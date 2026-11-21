import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import base64  # noqa: E402
import copy  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import unittest  # noqa: E402
import urllib.parse  # noqa: E402
from contextlib import redirect_stderr, redirect_stdout  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from unittest import mock  # noqa: E402

import import_archive  # noqa: E402
from importers import wordpress_api  # noqa: E402
from importers.common import ImporterError, ImportReport, normalize  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "wordpress_api"
SITE_KEY = "example-ledger-com"  # from the index's "home", not the test server's address
TYPE_BY_REST_BASE = {"posts": "post", "pages": "page", "story": "story", "media": "attachment"}
APP_USER, APP_PASSWORD = "editor", "abcdEFGHijklMNOPqrstUVWX"
PUBLISHED = ["101", "102", "103", "107", "111", "112", "113"]  # published posts normalize() keeps
_REMOVE = object()


def wp_id(n):
    return f"wp-{SITE_KEY}-{n}"


class FakeWordPress:
    """
    A small stand-in for a WordPress site's REST API, serving the fixtures the way
    WordPress does: page/per_page/X-WP-Total(Pages), status filtering, orderby,
    after/before on the local date, _embed=author, _fields, Basic auth with an
    Application Password, Co-Authors Plus's coauthors/v1 route, and WordPress's
    JSON error bodies.
    """

    def __init__(self, pretty=True, total_headers=True, ignore_page=False, ignore_status=False,
                 max_per_page=100, coauthors_plugin=True, coauthors_status=200, embed=True,
                 html_everywhere=False, index_overrides=None, hidden_users=(7,), hidden_after_count=(),
                 found_rows=True, yoast_authors=None, stale_yoast_lists=False, single_posts=True):
        # yoast_authors: {post id: name}, served as yoast_head_json.author the way Yoast SEO does.
        # stale_yoast_lists=True: in a list response every post carries the first post's name, as
        # seen on a live site; only /wp/v2/posts/<id> gives each post its own.
        # single_posts=False: the site answers 404 for /wp/v2/posts/<id>.
        self.yoast_authors, self.stale_yoast_lists = dict(yoast_authors or {}), stale_yoast_lists
        self.single_posts = single_posts
        # hidden_after_count: post ids a plugin drops from a page after WordPress has counted them
        # (e.g. a membership plugin's the_posts filter), so X-WP-Total includes them.
        # found_rows=False: a plugin forced no_found_rows, so WordPress reports 0 posts / 0 pages
        # (and, with a total of 0, answers pages past the end with [] instead of a 400).
        self.hidden_after_count, self.found_rows = set(hidden_after_count), found_rows
        self.pretty, self.total_headers, self.ignore_page = pretty, total_headers, ignore_page
        self.ignore_status, self.max_per_page = ignore_status, max_per_page
        self.coauthors_plugin, self.coauthors_status, self.embed = coauthors_plugin, coauthors_status, embed
        self.html_everywhere, self.hidden_users = html_everywhere, set(hidden_users)
        self.requests = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.index = self._load("index.json")
        if not coauthors_plugin:
            self.index["namespaces"].remove("coauthors/v1")
        for key, value in (index_overrides or {}).items():
            if value is _REMOVE:
                self.index.pop(key, None)
            else:
                self.index[key] = value
        self.posts = self._load("posts.json")
        self.users = self._load("users.json")
        self.coauthors = self._load("coauthors.json")
        self.types = self._load("types.json")
        self.home_html = (FIXTURES / "home.html").read_text(encoding="utf-8")
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    def _load(self, name):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8").replace("{{BASE}}", self.base))

    def _handler_class(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.handle(self)

            def log_message(self, *args):
                pass

        return Handler

    # --- helpers

    def requests_for(self, route):
        return [r for r in self.requests if r["route"] == route]

    def pages_requested(self, route="/wp/v2/posts"):
        return [int(r["query"].get("page", 1)) for r in self.requests_for(route)]

    def _send(self, req, status, body, ctype="application/json; charset=UTF-8", headers=()):
        data = body if isinstance(body, bytes) else (
            body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8"))
        req.send_response(status)
        req.send_header("Content-Type", ctype)
        req.send_header("Content-Length", str(len(data)))
        for name, value in headers:
            req.send_header(name, value)
        req.end_headers()
        req.wfile.write(data)

    def _error(self, req, status, code, message, **data):
        self._send(req, status, {"code": code, "message": message, "data": dict(status=status, **data)})

    def _api_url(self, route):
        return f"{self.base}/wp-json{route}" if self.pretty else f"{self.base}/?rest_route={route}"

    # --- routing

    def handle(self, req):
        parts = urllib.parse.urlsplit(req.path)
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        route = None
        if parts.path.startswith("/wp-json") and self.pretty:
            route = parts.path[len("/wp-json"):] or "/"
        elif "rest_route" in query:
            route = query["rest_route"]
        self.requests.append({"path": parts.path, "route": route, "query": query,
                              "auth": req.headers.get("Authorization")})
        if self.html_everywhere:
            return self._send(req, 200, "<html><body><h1>Welcome to our store</h1></body></html>", "text/html")
        if route is None:
            if parts.path in ("", "/"):
                root = self._api_url("/")
                # WordPress sends several Link headers; the API one comes first.
                return self._send(req, 200, self.home_html.replace("{{API_ROOT}}", root), "text/html; charset=UTF-8",
                                  [("Link", f'<{root}>; rel="https://api.w.org/"'),
                                   ("Link", f'<{self.base}/?p=2>; rel=shortlink')])
            return self._send(req, 404, "<html><body>Page not found</body></html>", "text/html")
        authed = False
        header = req.headers.get("Authorization")
        if header:
            user, _, password = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8").partition(":")
            if user != APP_USER:
                return self._error(req, 401, "invalid_username", "Unknown username. Check again or try your email address.")
            if password != APP_PASSWORD:
                return self._error(req, 401, "incorrect_password", "The provided password is an invalid application password.")
            authed = True
        self.route(req, route, query, authed)

    def route(self, req, route, query, authed):
        if route == "/":
            return self._send(req, 200, self._fields(self.index, query))
        if route == "/wp/v2/types":
            return self._send(req, 200, self.types)
        if route == "/wp/v2/users/me":
            if not authed:
                return self._error(req, 401, "rest_not_logged_in", "You are not currently logged in.")
            return self._send(req, 200, self.users["me"])
        m = re.fullmatch(r"/wp/v2/users/(\d+)", route)
        if m:
            uid = int(m.group(1))
            if uid in self.hidden_users:
                return self._error(req, 401, "rest_user_cannot_view", "Sorry, you are not allowed to list users.")
            if str(uid) not in self.users:
                return self._error(req, 404, "rest_user_invalid_id", "Invalid user ID.")
            return self._send(req, 200, self.users[str(uid)])
        if route == "/coauthors/v1/coauthors" and self.coauthors_plugin:
            if self.coauthors_status != 200:
                return self._error(req, self.coauthors_status, "rest_forbidden", "Sorry, you are not allowed to do that.")
            post_id = query.get("post_id", "")
            if post_id not in self.coauthors:
                return self._error(req, 404, "rest_post_invalid_id", "Invalid post ID.")
            return self._send(req, 200, self.coauthors[post_id])
        m = re.fullmatch(r"/wp/v2/([a-z_-]+)", route)
        if m and m.group(1) in TYPE_BY_REST_BASE:
            return self.collection(req, TYPE_BY_REST_BASE[m.group(1)], route, query, authed)
        m = re.fullmatch(r"/wp/v2/([a-z_-]+)/(\d+)", route)
        if m and m.group(1) in TYPE_BY_REST_BASE and self.single_posts:
            return self.single(req, TYPE_BY_REST_BASE[m.group(1)], int(m.group(2)), query, authed)
        return self._error(req, 404, "rest_no_route", "No route was found matching the URL and request method.")

    def collection(self, req, post_type, route, query, authed):
        statuses = query.get("status", "publish").split(",")
        if any(s != "publish" for s in statuses) and not authed:
            return self._error(req, 401, "rest_forbidden_status", "Status is forbidden.")
        try:
            per_page = int(query.get("per_page", 10))
            page = int(query.get("page", 1))
        except ValueError:
            return self._error(req, 400, "rest_invalid_param", "Invalid parameter(s): per_page")
        if not 1 <= per_page <= self.max_per_page:
            return self._error(req, 400, "rest_invalid_param", "Invalid parameter(s): per_page",
                               params={"per_page": f"per_page must be between 1 (inclusive) and "
                                                   f"{self.max_per_page} (inclusive)"})
        items = [p for p in self.posts if p["type"] == post_type and (self.ignore_status or p["status"] in statuses)]
        if query.get("after"):
            items = [p for p in items if p["date"] > query["after"]]
        if query.get("before"):
            items = [p for p in items if p["date"] < query["before"]]
        if query.get("orderby") == "id":
            items.sort(key=lambda p: p["id"], reverse=query.get("order", "desc") == "desc")
        else:
            items.sort(key=lambda p: p["date"], reverse=True)
        total = len(items) if self.found_rows else 0
        max_pages = math.ceil(total / per_page)
        if self.ignore_page:
            page = 1
        if page > max_pages and total > 0:
            return self._error(req, 400, "rest_post_invalid_page_number",
                               "The page number requested is larger than the number of pages available.")
        chunk = items[(page - 1) * per_page: page * per_page]
        chunk = [p for p in chunk if p["id"] not in self.hidden_after_count]
        headers = []
        if self.total_headers:
            headers += [("X-WP-Total", str(total)), ("X-WP-TotalPages", str(max_pages))]
            if page < max_pages:
                headers.append(("Link", f'<{self._api_url(route)}&page={page + 1}>; rel="next"'))
        shaped = [self._shape(p, query) for p in chunk]
        if self.stale_yoast_lists and chunk:
            first = self.yoast_authors.get(chunk[0]["id"])
            for post in shaped:
                if "yoast_head_json" in post:
                    post["yoast_head_json"] = {"author": first}
        self._send(req, 200, shaped, headers=headers)

    def single(self, req, post_type, post_id, query, authed):
        post = next((p for p in self.posts if p["id"] == post_id and p["type"] == post_type), None)
        if post is None:
            return self._error(req, 404, "rest_post_invalid_id", "Invalid post ID.")
        if post["status"] != "publish" and not authed:
            return self._error(req, 401, "rest_forbidden", "Sorry, you are not allowed to view this post.")
        self._send(req, 200, self._shape(post, query))

    def _shape(self, stored, query):
        post = copy.deepcopy(stored)
        if not self.coauthors_plugin:
            post.pop("coauthors", None)
        author = post.get("author")
        post["_links"] = {"self": [{"href": self._api_url(f"/wp/v2/posts/{post['id']}")}]}
        if post["id"] in self.yoast_authors:
            post["yoast_head_json"] = {"author": self.yoast_authors[post["id"]]}
        if author:
            post["_links"]["author"] = [{"embeddable": True, "href": self._api_url(f"/wp/v2/users/{author}")}]
        fields = set(query["_fields"].split(",")) if query.get("_fields") else None
        if self.embed and "_embed" in query and author and (fields is None or "_embedded" in fields):
            if author in self.hidden_users:
                embedded = {"code": "rest_user_cannot_view", "message": "Sorry, you are not allowed to list users.",
                            "data": {"status": 401}}
            else:
                embedded = self.users[str(author)]
            post["_embedded"] = {"author": [embedded]}
        return self._fields(post, query)

    @staticmethod
    def _fields(obj, query):
        if not query.get("_fields"):
            return obj
        keep = set(query["_fields"].split(","))
        return {k: v for k, v in obj.items() if k in keep}


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in (wordpress_api.USER_ENV, wordpress_api.PASSWORD_ENV)}
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=True)


def build_parser():
    with mock.patch.object(import_archive, "IMPORTERS", ["wordpress_api"]):
        return import_archive.build_parser()


class WordPressApiTestCase(unittest.TestCase):
    def fake(self, **options):
        server = FakeWordPress(**options)
        self.addCleanup(server.close)
        return server

    def parse(self, *argv):
        parser, _ = build_parser()
        return parser.parse_args(["wordpress-api", *argv])

    def run_import(self, *argv, env=None):
        """iter_articles + normalize, as the CLI does. Returns (articles by id, report, stderr, raws)."""
        args = self.parse(*argv)
        err = io.StringIO()
        with clean_env(**(env or {})), redirect_stderr(err):
            raws = list(wordpress_api.iter_articles(args))
        report = ImportReport(source="wordpress-api")
        articles = {}
        for raw in raws:
            article = normalize(raw, report)
            if article:
                articles[article["id"]] = article
        return articles, report, err.getvalue(), raws

    def import_error(self, *argv, env=None):
        args = self.parse(*argv)
        with clean_env(**(env or {})), redirect_stderr(io.StringIO()), self.assertRaises(ImporterError) as ctx:
            list(wordpress_api.iter_articles(args))
        return str(ctx.exception)


class PaginationAndMappingTests(WordPressApiTestCase):
    def test_reads_every_page_oldest_first(self):
        wp = self.fake()
        articles, report, err, _ = self.run_import("--site", wp.base, "--per-page", "4")
        self.assertEqual(list(articles), [wp_id(n) for n in ["101", "102", "103", "107", "111", "112", "113"]])
        self.assertEqual(wp.pages_requested(), [1, 2, 3])
        first = wp.requests_for("/wp/v2/posts")[0]["query"]
        self.assertEqual(first["per_page"], "4")
        self.assertEqual(first["status"], "publish")
        self.assertEqual((first["orderby"], first["order"]), ("id", "asc"))
        self.assertEqual(first["_embed"], "author")
        for field in ("id", "date_gmt", "link", "title", "content", "status", "coauthors", "_links", "_embedded"):
            self.assertIn(field, first["_fields"].split(","))
        self.assertIn("10 published posts to read", err)
        self.assertIn("posts: page 3/3 (10 read)", err)

    def test_fields_html_entities_and_text(self):
        wp = self.fake()
        articles, *_ = self.run_import("--site", wp.base)
        a = articles[wp_id(101)]
        self.assertEqual(a["headline"], "Council’s budget vote: “We’re out of time” & 3 other takeaways")
        self.assertEqual(a["url"], "https://www.example-ledger.com/2024/03/12/council-budget-vote/")
        self.assertEqual(a["content"],
                         "The Example County Council voted 7–2 on Tuesday to approve a $1.2 billion budget.\n\n"
                         "Council members debate the budget.\n\n"
                         "“We’re out of time,” said Council President Ana Ruiz.")
        self.assertNotIn("adsQueue", a["content"])
        self.assertEqual(articles[wp_id(112)]["headline"], "Fireworks & food trucks: your July 4th guide")
        self.assertIn("- Main Street from 1st to 9th", articles[wp_id(103)]["content"])

    def test_skips_protected_and_malformed_with_reasons(self):
        wp = self.fake()
        articles, report, *_ = self.run_import("--site", wp.base)
        self.assertNotIn(wp_id(104), articles)
        self.assertEqual(dict(report.skipped), {"password-protected (its text isn't public)": 1,
                                                "no headline": 1, "no valid http(s) URL": 1})

    def test_ids_are_stable_and_namespaced_by_site(self):
        wp = self.fake()
        first, *_ = self.run_import("--site", wp.base)
        again, *_ = self.run_import("--site", wp.base + "/")
        self.assertEqual(list(first), list(again))
        self.assertTrue(all(i.startswith(f"wp-{SITE_KEY}-") for i in first))

    def test_without_total_headers_stops_on_short_page(self):
        wp = self.fake(total_headers=False)
        articles, _, err, _ = self.run_import("--site", wp.base, "--per-page", "4")
        self.assertEqual(len(articles), 7)
        self.assertEqual(wp.pages_requested(), [1, 2, 3])
        self.assertIn("page 3/?", err)

    def test_without_total_headers_stops_on_invalid_page_error(self):
        wp = self.fake(total_headers=False)
        articles, *_ = self.run_import("--site", wp.base, "--per-page", "5")
        self.assertEqual(len(articles), 7)
        self.assertEqual(wp.pages_requested(), [1, 2, 3])  # page 3 answered rest_post_invalid_page_number

    def test_cache_that_ignores_page_number_is_reported(self):
        wp = self.fake(ignore_page=True, total_headers=False)
        message = self.import_error("--site", wp.base, "--per-page", "4")
        self.assertIn("ignoring the page number", message)

    def test_per_page_rejected_falls_back_to_wordpress_default(self):
        wp = self.fake(max_per_page=50)
        articles, _, err, _ = self.run_import("--site", wp.base)
        self.assertEqual(len(articles), 7)
        self.assertEqual([r["query"]["per_page"] for r in wp.requests_for("/wp/v2/posts")], ["100", "10"])
        self.assertIn("using 10", err)

    def test_custom_post_type(self):
        wp = self.fake()
        articles, *_ = self.run_import("--site", wp.base, "--post-type", "story")
        self.assertEqual(list(articles), [wp_id(201)])
        self.assertEqual(articles[wp_id(201)]["authors"], ["Derek Chu"])

    def test_unknown_post_type_lists_available_types(self):
        wp = self.fake()
        message = self.import_error("--site", wp.base, "--post-type", "stories")
        self.assertIn("'stories'", message)
        self.assertIn("media, pages, posts, story", message)

    def test_date_window_is_sent_as_site_local_bounds(self):
        wp = self.fake()
        articles, *_ = self.run_import("--site", wp.base, "--after", "2023-01-01", "--before", "2024-01-01")
        query = wp.requests_for("/wp/v2/posts")[0]["query"]
        self.assertEqual(query["after"], "2022-12-31T23:59:59")
        self.assertEqual(query["before"], "2024-01-01T00:00:00")
        self.assertEqual(sorted(articles), sorted(wp_id(n) for n in ["103", "107", "112", "113"]))

    def test_after_must_precede_before(self):
        message = self.import_error("--site", "https://news.example.org", "--after", "2024-01-01",
                                    "--before", "2023-01-01")
        self.assertIn("--after", message)


class StatusTests(WordPressApiTestCase):
    def test_non_published_items_are_skipped_with_reasons(self):
        # A misbehaving plugin or cache returning every status: the importer still keeps only published posts.
        wp = self.fake(ignore_status=True)
        articles, report, *_ = self.run_import("--site", wp.base)
        self.assertEqual(sorted(articles), sorted(wp_id(n) for n in PUBLISHED))
        self.assertEqual(report.skipped["draft or pending review (not published)"], 1)
        self.assertEqual(report.skipped["scheduled, not yet published"], 1)
        self.assertEqual(report.skipped["private post (use --include-private with an Application Password to import)"], 1)

    def test_status_reason_is_sanitized(self):
        self.assertEqual(wordpress_api._status_reason("<b>pitch</b>"),
                         "status 'pitch' (only published posts are imported)")
        self.assertEqual(wordpress_api._status_reason("trash"), "in the trash")


class TimezoneTests(WordPressApiTestCase):
    def test_date_gmt_is_used_when_present(self):
        wp = self.fake()
        articles, *_ = self.run_import("--site", wp.base)
        self.assertEqual(articles[wp_id(101)]["publish_date"], "2024-03-12T22:05:00Z")
        self.assertEqual(articles[wp_id(112)]["publish_date"], "2023-07-04T13:30:00Z")

    def test_missing_date_gmt_uses_site_time_zone_with_dst(self):
        try:
            wordpress_api.ZoneInfo("America/New_York")
        except Exception:  # pragma: no cover - no tz database on this machine
            self.skipTest("no IANA time zone database")
        wp = self.fake()
        articles, *_ = self.run_import("--site", wp.base)
        self.assertEqual(articles[wp_id(107)]["publish_date"], "2023-01-15T13:00:00Z")  # EST, UTC-5
        self.assertEqual(articles[wp_id(113)]["publish_date"], "2023-06-16T00:15:00Z")  # EDT, UTC-4

    def test_missing_date_gmt_uses_fixed_offset_when_no_zone_name(self):
        wp = self.fake(index_overrides={"timezone_string": "", "gmt_offset": "-6"})
        articles, *_ = self.run_import("--site", wp.base)
        self.assertEqual(articles[wp_id(107)]["publish_date"], "2023-01-15T14:00:00Z")
        self.assertEqual(articles[wp_id(113)]["publish_date"], "2023-06-16T02:15:00Z")

    def test_missing_date_gmt_and_unknown_zone_is_skipped_not_guessed(self):
        wp = self.fake(index_overrides={"timezone_string": _REMOVE, "gmt_offset": _REMOVE})
        articles, report, *_ = self.run_import("--site", wp.base)
        self.assertNotIn(wp_id(107), articles)
        self.assertNotIn(wp_id(113), articles)
        self.assertEqual(report.skipped["no UTC publish date (date_gmt) and the site's time zone is unknown"], 2)

    def test_date_with_explicit_offset(self):
        post = {"date_gmt": None, "date": "2023-03-01T09:00:00-08:00"}
        when, reason = wordpress_api._publish_date(post, None)
        self.assertIsNone(reason)
        self.assertEqual(when.isoformat(), "2023-03-01T17:00:00+00:00")


class BylineTests(WordPressApiTestCase):
    def test_coauthors_plus_bylines_in_order(self):
        wp = self.fake()
        articles, report, err, _ = self.run_import("--site", wp.base)
        self.assertEqual(articles[wp_id(102)]["authors"], ["María González", "Sam O'Neil"])
        self.assertEqual(articles[wp_id(111)]["authors"], ["Priya Natarajan"])  # guest author, no WP account
        self.assertEqual(articles[wp_id(107)]["authors"], ["Jane Q. Reporter"])
        self.assertEqual(articles[wp_id(101)]["authors"], ["Jane Q. Reporter"])  # coauthors: [] -> post author
        self.assertEqual(articles[wp_id(103)]["authors"], [])  # no byline at all
        asked = sorted(r["query"]["post_id"] for r in wp.requests_for("/coauthors/v1/coauthors"))
        self.assertEqual(asked, ["102", "107", "111"])  # not for skipped or coauthors: [] posts
        self.assertIn("Co-Authors Plus found", err)
        self.assertNotIn("Note:", err)

    def test_wordpress_post_author_without_coauthors_plus(self):
        wp = self.fake(coauthors_plugin=False)
        articles, _, err, _ = self.run_import("--site", wp.base)
        self.assertEqual(articles[wp_id(102)]["authors"], ["Jane Q. Reporter"])
        self.assertEqual(articles[wp_id(112)]["authors"], ["Derek Chu"])
        self.assertEqual(articles[wp_id(111)]["authors"], [])  # site hides this user: no placeholder
        self.assertEqual(wp.requests_for("/coauthors/v1/coauthors"), [])
        self.assertEqual(wp.requests_for("/wp/v2/users/7"), [])  # embed already said no
        self.assertIn("1 post(s) have no authors because the site hides author details", err)

    def test_blocked_coauthors_api_leaves_bylines_empty_rather_than_guessing(self):
        wp = self.fake(coauthors_status=403)
        articles, _, err, _ = self.run_import("--site", wp.base)
        for n in ("102", "107", "111"):
            self.assertEqual(articles[wp_id(n)]["authors"], [], n)
        self.assertEqual(articles[wp_id(101)]["authors"], ["Jane Q. Reporter"])
        self.assertIn("3 post(s) have no authors because their Co-Authors Plus bylines couldn't be read "
                      "(HTTP 403 rest_forbidden)", err)

    def test_coauthors_lookups_stop_after_repeated_failures(self):
        class Client:
            calls = 0

            def get(self, url, params=None, retries=4):
                Client.calls += 1
                raise wordpress_api._WPError("HTTP 403 from x", 403, "rest_forbidden")

        bylines = wordpress_api._Bylines(Client(), wordpress_api._Api("https://example.com/wp-json/"), True, None)
        with redirect_stderr(io.StringIO()):
            results = [bylines.resolve({"id": n, "coauthors": [5], "author": 3}) for n in range(1, 7)]
        self.assertEqual(results, [[]] * 6)
        self.assertEqual(Client.calls, 3)
        self.assertEqual(bylines.cap_missing, 6)

    def test_author_names_looked_up_when_embedding_is_unavailable(self):
        wp = self.fake(coauthors_plugin=False, embed=False)
        articles, _, err, _ = self.run_import("--site", wp.base)
        self.assertEqual(articles[wp_id(101)]["authors"], ["Jane Q. Reporter"])
        self.assertEqual(articles[wp_id(102)]["authors"], ["Jane Q. Reporter"])
        self.assertEqual(articles[wp_id(111)]["authors"], [])
        self.assertEqual(len(wp.requests_for("/wp/v2/users/3")), 1)  # cached per author
        self.assertIn("hides author details", err)

    def test_byline_field(self):
        wp = self.fake()
        articles, *_ = self.run_import("--site", wp.base, "--byline-field", "acf.byline")
        self.assertEqual(articles[wp_id(101)]["authors"], ["Lee Ann Park"])
        self.assertEqual(articles[wp_id(102)]["authors"], ["María González", "Sam O'Neil"])  # empty -> CAP
        self.assertIn("acf", wp.requests_for("/wp/v2/posts")[0]["query"]["_fields"].split(","))

    def test_names_from_custom_field_shapes(self):
        f = wordpress_api._names_from_value
        self.assertEqual(f("By Lee Ann Park"), ["Lee Ann Park"])
        self.assertEqual(f(["Ana Ruiz", "Tom Becker", "Ana Ruiz"]), ["Ana Ruiz", "Tom Becker"])
        self.assertEqual(f([{"display_name": "Ana Ruiz"}, {"name": "Tom&nbsp;Becker"}]), ["Ana Ruiz", "Tom Becker"])
        self.assertEqual(f({"title": {"rendered": "Derek Chu"}}), ["Derek Chu"])
        self.assertEqual(f([13, 15]), [])
        self.assertEqual(f(None), [])


YOAST = "yoast_head_json.author"
OWN_NAMES = {101: "Lee Ann Park", 102: "Ana Ruiz", 103: "Lee Ann Park", 107: "Tom Becker",
             111: "Ana Ruiz", 112: "Lee Ann Park", 113: "Tom Becker"}


class BylineFieldPerPostTests(WordPressApiTestCase):
    """--byline-field must give each article its own post's value (issue #4)."""

    def single_post_requests(self, wp):
        return [r["route"] for r in wp.requests if re.fullmatch(r"/wp/v2/posts/\d+", r["route"] or "")]

    def test_a_list_that_repeats_one_byline_is_reread_post_by_post(self):
        # Seen on a live site: yoast_head_json.author in a list response is the first post's
        # author on every post, and right only on /wp/v2/posts/<id>.
        wp = self.fake(yoast_authors=OWN_NAMES, stale_yoast_lists=True)
        articles, _, err, _ = self.run_import("--site", wp.base, "--byline-field", YOAST)
        self.assertEqual({n: articles[wp_id(n)]["authors"] for n in OWN_NAMES},
                         {n: [name] for n, name in OWN_NAMES.items()})
        self.assertIn("gives --byline-field yoast_head_json.author another post's value", err)
        self.assertIn("one extra request per post", err)
        self.assertNotIn("gave the same byline", err)
        asked = wp.requests_for("/wp/v2/posts/102")[0]["query"]["_fields"].split(",")
        self.assertIn("yoast_head_json", asked)

    def test_the_stale_value_changing_from_page_to_page_does_not_hide_it(self):
        wp = self.fake(yoast_authors=OWN_NAMES, stale_yoast_lists=True)
        articles, *_ = self.run_import("--site", wp.base, "--byline-field", YOAST, "--per-page", "3")
        self.assertEqual({n: articles[wp_id(n)]["authors"] for n in OWN_NAMES},
                         {n: [name] for n, name in OWN_NAMES.items()})

    def test_a_post_whose_own_response_has_no_value_falls_back_instead_of_keeping_the_stale_one(self):
        names = dict(OWN_NAMES)
        del names[112]  # no Yoast author of its own: the WordPress post author is used
        wp = self.fake(yoast_authors=names, stale_yoast_lists=True, coauthors_plugin=False)
        for post in wp.posts:  # the plugin still puts the (stale) field on every post in a list
            names.setdefault(post["id"], None)
        wp.yoast_authors = names
        articles, *_ = self.run_import("--site", wp.base, "--byline-field", YOAST)
        self.assertEqual(articles[wp_id(112)]["authors"], ["Derek Chu"])
        self.assertEqual(articles[wp_id(113)]["authors"], ["Tom Becker"])

    def test_a_list_with_each_posts_own_value_is_spot_checked_not_reread(self):
        wp = self.fake(yoast_authors=OWN_NAMES)
        articles, _, err, _ = self.run_import("--site", wp.base, "--byline-field", YOAST)
        self.assertEqual(articles[wp_id(107)]["authors"], ["Tom Becker"])
        self.assertEqual(self.single_post_requests(wp),
                         [f"/wp/v2/posts/{n}" for n in (101, 102, 103, 107, 111)])
        self.assertNotIn("another post's value", err)
        self.assertNotIn("Note:", err)

    def test_a_site_one_person_wrote_still_imports_and_says_what_it_checked(self):
        wp = self.fake(yoast_authors={n: "Ana Ruiz" for n in OWN_NAMES})
        articles, _, err, _ = self.run_import("--site", wp.base, "--byline-field", YOAST)
        self.assertEqual({tuple(a["authors"]) for a in articles.values()}, {("Ana Ruiz",)})
        self.assertEqual(len(self.single_post_requests(wp)), 7)  # every repeat was checked
        self.assertIn("Note: --byline-field yoast_head_json.author gave the same byline, Ana Ruiz, "
                      "for all 7 posts that used it", err)
        self.assertIn("own API response says the same", err)

    def test_the_same_two_names_on_every_post_are_flagged(self):
        wp = self.fake(yoast_authors={n: "By Ana Ruiz and Tom Becker" for n in OWN_NAMES})
        articles, _, err, _ = self.run_import("--site", wp.base, "--byline-field", YOAST)
        self.assertEqual(articles[wp_id(101)]["authors"], ["Ana Ruiz", "Tom Becker"])
        self.assertIn("gave the same byline, Ana Ruiz and Tom Becker, for all 7 posts", err)

    def test_posts_that_fell_back_to_another_source_do_not_hide_it(self):
        # Three posts use the field, the rest fall back to Co-Authors Plus or the post author.
        wp = self.fake(yoast_authors={101: "Ana Ruiz", 103: "Ana Ruiz", 113: "Ana Ruiz"})
        articles, _, err, _ = self.run_import("--site", wp.base, "--byline-field", YOAST)
        self.assertEqual(articles[wp_id(102)]["authors"], ["María González", "Sam O'Neil"])
        self.assertIn("gave the same byline, Ana Ruiz, for all 3 posts that used it", err)

    def test_a_check_that_cannot_be_made_is_said_so(self):
        wp = self.fake(yoast_authors={n: "Ana Ruiz" for n in OWN_NAMES}, single_posts=False)
        articles, _, err, _ = self.run_import("--site", wp.base, "--byline-field", YOAST)
        self.assertEqual(articles[wp_id(113)]["authors"], ["Ana Ruiz"])
        self.assertEqual(len(self.single_post_requests(wp)), 3)  # gives up, like the other lookups
        self.assertIn("gave the same byline, Ana Ruiz, for all 7 posts", err)
        self.assertIn("couldn't check that against each post's own API response (HTTP 404", err)
        self.assertNotIn("says the same", err)

    def test_private_posts_are_reread_with_the_login(self):
        names = dict(OWN_NAMES)
        names[106] = "Derek Chu"
        wp = self.fake(yoast_authors=names, stale_yoast_lists=True)
        articles, *_ = self.run_import("--site", wp.base, "--byline-field", YOAST, "--include-private",
                                       env={wordpress_api.USER_ENV: APP_USER,
                                            wordpress_api.PASSWORD_ENV: APP_PASSWORD})
        self.assertEqual(articles[wp_id(106)]["authors"], ["Derek Chu"])


class DiscoveryTests(WordPressApiTestCase):
    def test_finds_api_without_pretty_permalinks(self):
        wp = self.fake(pretty=False)
        articles, _, err, _ = self.run_import("--site", wp.base, "--per-page", "4")
        self.assertEqual(len(articles), 7)
        self.assertIn(f"{wp.base}/?rest_route=/", err)
        self.assertTrue(all(r["path"] == "/" for r in wp.requests_for("/wp/v2/posts")))

    def test_explicit_api_root(self):
        wp = self.fake(pretty=False)
        articles, *_ = self.run_import("--site", "https://www.example-ledger.com",
                                       "--api-root", f"{wp.base}/?rest_route=/")
        self.assertEqual(len(articles), 7)
        self.assertEqual(wp.requests[0]["route"], "/")  # no detour through the home page

    def test_site_given_as_api_url(self):
        wp = self.fake()
        articles, *_ = self.run_import("--site", f"{wp.base}/wp-json/wp/v2/posts")
        self.assertEqual(len(articles), 7)

    def test_not_a_wordpress_site(self):
        wp = self.fake(html_everywhere=True)
        message = self.import_error("--site", wp.base)
        self.assertIn("Couldn't find a WordPress REST API", message)
        self.assertIn("wordpress-export", message)

    def test_bad_site_value(self):
        self.assertIn("--site", self.import_error("--site", "ftp://files.example.org"))

    def test_site_keys_and_default_prefix(self):
        self.assertEqual(wordpress_api._site_key("https://www.Example-Ledger.com/"), "example-ledger-com")
        self.assertEqual(wordpress_api._site_key("https://example.com/news/wp-json/"), "example-com-news")
        args = self.parse("--site", "www.example-ledger.com", "--post-type", "story",
                          "--after", "2023-01-01", "--before", "2024-01-01")
        self.assertEqual(wordpress_api.default_prefix(args),
                         "wordpress-example-ledger-com-story-after-20230101-before-20240101")
        self.assertEqual(wordpress_api.default_prefix(self.parse("--site", "https://www.example-ledger.com")),
                         "wordpress-example-ledger-com")


class AuthTests(WordPressApiTestCase):
    def test_include_private_with_application_password_from_env(self):
        wp = self.fake()
        spaced = " ".join(APP_PASSWORD[i:i + 4] for i in range(0, len(APP_PASSWORD), 4))
        articles, _, err, _ = self.run_import("--site", wp.base, "--include-private",
                                              env={"WORDPRESS_USER": APP_USER, "WORDPRESS_APP_PASSWORD": spaced})
        self.assertEqual(articles[wp_id(106)]["headline"], "Newsroom style memo")  # WordPress's prefix dropped
        self.assertEqual(articles[wp_id(106)]["authors"], ["Tom Becker"])
        self.assertEqual(wp.requests_for("/wp/v2/posts")[0]["query"]["status"], "publish,private")
        self.assertTrue(all(r["auth"] for r in wp.requests if r["route"] and r["route"] != "/"))
        self.assertIn("Signed in to WordPress as Nina Editor", err)
        self.assertNotIn(APP_PASSWORD, err)
        self.assertNotIn(APP_PASSWORD[:4], err)

    def test_flags_work_too(self):
        wp = self.fake()
        articles, *_ = self.run_import("--site", wp.base, "--include-private",
                                       "--user", APP_USER, "--app-password", APP_PASSWORD)
        self.assertIn(wp_id(106), articles)

    def test_include_private_requires_credentials(self):
        wp = self.fake()
        message = self.import_error("--site", wp.base, "--include-private")
        self.assertIn("WORDPRESS_APP_PASSWORD", message)
        self.assertEqual(wp.requests, [])

    def test_half_a_login_is_an_error(self):
        message = self.import_error("--site", "https://news.example.org", env={"WORDPRESS_USER": APP_USER})
        self.assertIn("Application Password", message)

    def test_wrong_password_is_explained_without_echoing_it(self):
        wp = self.fake()
        message = self.import_error("--site", wp.base, env={"WORDPRESS_USER": APP_USER,
                                                             "WORDPRESS_APP_PASSWORD": "wrongwrongwrong"})
        self.assertIn("rejected the Application Password", message)
        self.assertNotIn("wrongwrongwrong", message)

    def test_wrong_username(self):
        wp = self.fake()
        message = self.import_error("--site", wp.base, env={"WORDPRESS_USER": "nobody",
                                                             "WORDPRESS_APP_PASSWORD": APP_PASSWORD})
        self.assertIn("doesn't know the username 'nobody'", message)

    def test_refuses_to_send_password_over_plain_http(self):
        message = self.import_error("--site", "http://news.example.org",
                                    env={"WORDPRESS_USER": APP_USER, "WORDPRESS_APP_PASSWORD": APP_PASSWORD})
        self.assertIn("plain http", message)
        self.assertNotIn(APP_PASSWORD, message)

    def test_private_posts_without_permission(self):
        wp = self.fake()
        with mock.patch.object(FakeWordPress, "collection",
                               lambda self, req, *a: self._error(req, 403, "rest_forbidden_status",
                                                                 "Status is forbidden.")):
            message = self.import_error("--site", wp.base, "--include-private",
                                        env={"WORDPRESS_USER": APP_USER, "WORDPRESS_APP_PASSWORD": APP_PASSWORD})
        self.assertIn("can't list private posts", message)

    def test_authorization_header_only_goes_to_the_named_site(self):
        named, other = self.fake(), self.fake()
        header = "Basic " + base64.b64encode(f"{APP_USER}:{APP_PASSWORD}".encode()).decode()
        client = wordpress_api._Client(header, wordpress_api._origin(named.base), APP_USER)
        client.get(named.base + "/wp-json/")
        client.get(other.base + "/wp-json/")
        self.assertEqual(named.requests[0]["auth"], header)
        self.assertIsNone(other.requests[0]["auth"])

    def test_api_is_found_without_sending_credentials(self):
        wp = self.fake()
        self.run_import("--site", wp.base, env={"WORDPRESS_USER": APP_USER, "WORDPRESS_APP_PASSWORD": APP_PASSWORD})
        self.assertEqual(wp.requests[0]["route"], "/")
        self.assertIsNone(wp.requests[0]["auth"])
        self.assertTrue(all(r["auth"] for r in wp.requests[1:]))


class ReviewRegressionTests(WordPressApiTestCase):
    """Cases where the importer used to finish "successfully" with a wrong or incomplete archive."""

    WITHHELD = "counted by WordPress but not returned by its API (often a membership or paywall plugin)"

    # --- pagination

    def test_empty_page_inside_the_reported_range_does_not_end_the_import(self):
        # per_page 2 over posts 101..113: page 3 holds 107 and 108, which a plugin drops after counting.
        wp = self.fake(hidden_after_count={107, 108})
        articles, report, err, _ = self.run_import("--site", wp.base, "--per-page", "2")
        self.assertEqual(wp.pages_requested(), [1, 2, 3, 4, 5])
        self.assertEqual(sorted(articles), sorted(wp_id(n) for n in ["101", "102", "103", "111", "112", "113"]))
        self.assertEqual(report.skipped[self.WITHHELD], 2)
        self.assertIn("2 of the 10", err)

    def test_posts_withheld_from_a_short_page_are_counted(self):
        wp = self.fake(hidden_after_count={107})
        articles, report, *_ = self.run_import("--site", wp.base, "--per-page", "4")
        self.assertEqual(wp.pages_requested(), [1, 2, 3])
        self.assertIn(wp_id(113), articles)
        self.assertEqual(report.skipped[self.WITHHELD], 1)

    def test_withheld_posts_appear_in_the_cli_summary(self):
        wp = self.fake(hidden_after_count={107, 108})
        with tempfile.TemporaryDirectory() as tmp, clean_env(), mock.patch.object(import_archive, "IMPORTERS",
                                                                                  ["wordpress_api"]):
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = import_archive.main(["wordpress-api", "--site", wp.base, "--per-page", "2", "--out-dir", tmp])
            written = json.loads(next(pathlib.Path(tmp).glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(len(written), 6)
        self.assertIn(f"2 skipped: {self.WITHHELD}", out.getvalue())

    def test_page_count_of_zero_is_not_trusted_when_posts_come_back(self):
        # A plugin forcing no_found_rows makes WordPress send X-WP-Total: 0 and X-WP-TotalPages: 0.
        wp = self.fake(found_rows=False)
        articles, _, err, _ = self.run_import("--site", wp.base, "--per-page", "4")
        self.assertEqual(sorted(articles), sorted(wp_id(n) for n in PUBLISHED))
        self.assertEqual(wp.pages_requested(), [1, 2, 3])
        self.assertNotIn("lists no published posts", err)

    def test_page_count_of_zero_with_a_full_last_page_stops_on_the_empty_page(self):
        wp = self.fake(found_rows=False)
        articles, *_ = self.run_import("--site", wp.base, "--per-page", "5")
        self.assertEqual(sorted(articles), sorted(wp_id(n) for n in PUBLISHED))
        self.assertEqual(wp.pages_requested(), [1, 2, 3])  # page 3 is an empty 200: the end

    # --- an empty listing must not replace an earlier import

    def test_empty_listing_keeps_the_previous_import(self):
        wp = self.fake()
        with tempfile.TemporaryDirectory() as tmp, clean_env(), mock.patch.object(import_archive, "IMPORTERS",
                                                                                  ["wordpress_api"]):
            argv = ["wordpress-api", "--site", wp.base, "--out-dir", tmp, "--prefix", "ledger"]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(import_archive.main(argv), 0)
            wp.posts = []  # e.g. a cache or security plugin in front of the site now answers []
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = import_archive.main(argv)
            kept = json.loads((pathlib.Path(tmp) / "ledger-0001.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(len(kept), 7)
        self.assertIn("listed no published posts", err.getvalue())

    def test_everything_withheld_is_explained(self):
        every_id = {p["id"] for p in json.loads((FIXTURES / "posts.json").read_text(encoding="utf-8"))}
        wp = self.fake(hidden_after_count=every_id)
        message = self.import_error("--site", wp.base)
        self.assertIn("WordPress counted 10", message)

    # --- credentials in addresses

    def test_credentials_in_the_site_address_are_refused_without_echoing_them(self):
        for site in ("https://stage:s3cretpw@news.example.org", "stage:s3cretpw@news.example.org/wp-json/",
                     "ftp://stage:s3cretpw@news.example.org"):
            message = self.import_error("--site", site)
            self.assertNotIn("s3cretpw", message, site)
            self.assertIn("--site", message, site)
        message = self.import_error("--site", "https://news.example.org",
                                    "--api-root", "https://stage:s3cretpw@news.example.org/wp-json/")
        self.assertNotIn("s3cretpw", message)
        self.assertIn("username or password", message)

    # --- bylines

    def test_text_bylines_are_split_into_names(self):
        f = wordpress_api._names_from_value
        self.assertEqual(f("By Jane Doe and Sam Lee"), ["Jane Doe", "Sam Lee"])
        self.assertEqual(f("By: Jane Doe"), ["Jane Doe"])
        self.assertEqual(f("Written by Jane Doe, Sam Lee and Ana Ruiz"), ["Jane Doe", "Sam Lee", "Ana Ruiz"])
        self.assertEqual(f("Jane Doe, Staff Writer"), ["Jane Doe, Staff Writer"])  # a comma alone doesn't split
        self.assertEqual(f(["Jane Doe & Sam Lee", "Ana Ruiz"]), ["Jane Doe", "Sam Lee", "Ana Ruiz"])
        self.assertEqual(f({"display_name": "Smith & Wesson"}), ["Smith & Wesson"])  # one account: not split

    def test_text_bylines_match_the_wordpress_export_importer(self):
        # Both importers write the same ids, so re-importing one way must not change the authors.
        try:
            from importers import wordpress_export
        except ImportError:  # pragma: no cover
            self.skipTest("wordpress_export not available")
        for text in ("By Jane Doe and Sam Lee", "By: Jane Doe", "story by Ana Ruiz; Tom Becker",
                     "Jane Doe, Sam Lee & Ana Ruiz", "Jane Doe, Staff Writer", "BY LEE ANN PARK"):
            self.assertEqual(wordpress_api._names_from_value(text), wordpress_export._split_byline(text), text)

    def test_byline_field_with_two_names_end_to_end(self):
        wp = self.fake()
        next(p for p in wp.posts if p["id"] == 101)["acf"]["byline"] = "By: Lee Ann Park and Tom Becker"
        articles, *_ = self.run_import("--site", wp.base, "--byline-field", "acf.byline")
        self.assertEqual(articles[wp_id(101)]["authors"], ["Lee Ann Park", "Tom Becker"])

    # --- dates

    def test_non_text_dates_are_not_read_as_unix_timestamps(self):
        # WordPress's mysql_to_rfc3339('') returns false, which reaches the JSON as "date_gmt": false.
        utc = wordpress_api.timezone.utc
        when, reason = wordpress_api._publish_date({"date_gmt": False, "date": "2024-03-12T18:05:00"}, utc)
        self.assertEqual((when.isoformat(), reason), ("2024-03-12T18:05:00+00:00", None))
        self.assertEqual(wordpress_api._publish_date({"date_gmt": False, "date": False}, utc), (None, None))
        self.assertEqual(wordpress_api._publish_date({"date_gmt": 0, "date": 0}, utc), (None, None))

    def test_post_with_false_dates_is_skipped_not_dated_1970(self):
        wp = self.fake()
        post = next(p for p in wp.posts if p["id"] == 112)
        post["date_gmt"], post["date"] = False, False
        articles, report, *_ = self.run_import("--site", wp.base)
        self.assertNotIn(wp_id(112), articles)
        self.assertEqual(report.skipped["no parseable publish date"], 1)
        self.assertFalse(any(a["publish_date"].startswith("1970") for a in articles.values()))


class HelpTests(unittest.TestCase):
    def test_help_says_where_to_find_each_value(self):
        parser, _ = build_parser()
        sub = parser._subparsers._group_actions[0].choices["wordpress-api"]
        text = sub.format_help()
        for needle in ("/wp-json/", "rest_base", "Users > Profile > Application Passwords",
                       "WORDPRESS_USER", "WORDPRESS_APP_PASSWORD", "--include-private", "shell history"):
            self.assertIn(needle, text)
        self.assertNotIn("\n", wordpress_api.HELP)


class EndToEndTests(WordPressApiTestCase):
    def test_cli_writes_articles_and_returns_zero(self):
        wp = self.fake()
        with tempfile.TemporaryDirectory() as tmp, clean_env(), mock.patch.object(import_archive, "IMPORTERS",
                                                                                  ["wordpress_api"]):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = import_archive.main(["wordpress-api", "--site", wp.base, "--per-page", "4", "--out-dir", tmp])
            self.assertEqual(code, 0, err.getvalue())
            files = sorted(pathlib.Path(tmp).iterdir())
            port = wp.base.rsplit(":", 1)[1]
            self.assertEqual([f.name for f in files], [f"wordpress-127-0-0-1-{port}-0001.json"])
            written = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(len(written), 7)
        by_id = {a["id"]: a for a in written}
        self.assertEqual(by_id[wp_id(102)], {
            "id": wp_id(102),
            "headline": "Inside the port’s $40M dredging plan",
            "content": "The port authority wants to deepen the main channel to 45 feet.\n\nWho pays\n\n"
                       "State and federal grants would cover most of the cost.",
            "url": "https://www.example-ledger.com/2024/02/01/port-dredging-plan/",
            "authors": ["María González", "Sam O'Neil"],
            "publish_date": "2024-02-01T12:30:00Z",
        })
        self.assertTrue(all(set(a) == {"id", "headline", "content", "url", "authors", "publish_date"} for a in written))
        summary = out.getvalue()
        self.assertIn("Imported 7 article(s) from wordpress-api.", summary)
        self.assertIn("1 have no byline", summary)
        self.assertIn("1 skipped: password-protected (its text isn't public)", summary)

    def test_cli_limit_stops_early(self):
        wp = self.fake()
        with tempfile.TemporaryDirectory() as tmp, clean_env(), mock.patch.object(import_archive, "IMPORTERS",
                                                                                  ["wordpress_api"]):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = import_archive.main(["wordpress-api", "--site", wp.base, "--limit", "2", "--out-dir", tmp,
                                            "--prefix", "trial"])
            written = json.loads((pathlib.Path(tmp) / "trial-0001.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual([a["id"] for a in written], [wp_id(101), wp_id(102)])
        self.assertEqual(wp.pages_requested(), [1])
        self.assertEqual(wp.requests_for("/wp/v2/posts")[0]["query"]["per_page"], "2")

    def test_cli_failure_returns_one_and_writes_nothing(self):
        wp = self.fake(html_everywhere=True)
        with tempfile.TemporaryDirectory() as tmp, clean_env(), mock.patch.object(import_archive, "IMPORTERS",
                                                                                  ["wordpress_api"]):
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = import_archive.main(["wordpress-api", "--site", wp.base, "--out-dir", tmp])
            self.assertEqual(list(pathlib.Path(tmp).iterdir()), [])
        self.assertEqual(code, 1)
        self.assertIn("Import failed", err.getvalue())


if __name__ == "__main__":
    unittest.main()

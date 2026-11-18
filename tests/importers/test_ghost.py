import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import argparse  # noqa: E402
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
from datetime import datetime, timezone  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from unittest import mock  # noqa: E402

import import_archive  # noqa: E402
from importers import ghost  # noqa: E402
from importers.common import ImporterError, ImportReport, html_to_text, normalize  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "ghost"
KEY = "8f1c2a3b4d5e6f7a8b9c0d1e2f"  # 26 hex characters, like a real Content API key
ADMIN_KEY = "64f0a1b2c3d4e5f6a7b8c9d0:" + "ab" * 32
SITE = "https://news.harborledger.example/"
# Public, published posts normalize() keeps by default, oldest first.
PUBLIC = ["a00001", "a00002", "a00003", "a00004", "a00008", "a00009", "a00010", "a00013", "a00019"]
EXPECTED_SKIPS = {
    "members-only (members): only a preview is public (--include-member-previews imports it)": 1,
    "members-only (paid): only a preview is public (--include-member-previews imports it)": 1,
    "members-only (tiers): only a preview is public (--include-member-previews imports it)": 1,
    "no public URL (Ghost reports /404/: no route in the theme's routes.yaml serves it)": 1,
    "no parseable publish date": 2,
    "no headline": 1,
    "no id": 1,
    "no article text": 1,
    "not published (status: draft)": 1,
}


def gid(suffix):
    return f"ghost-65a0c1f2e4b0a10001{suffix}"


def _parse_ghost_time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class FakeGhost:
    """
    A small stand-in for a Ghost site's Content API, serving the fixtures the way
    Ghost does: ?key= auth with Ghost's JSON error bodies, limit/page with
    meta.pagination (limit capped at 100, default 15), order, include=authors/tags,
    a subset of NQL filters, and the unversioned or a legacy versioned API path.
    Gating is already applied in the fixtures (as Ghost does for a request without a member).
    """

    def __init__(self, api_paths=("/ghost/api/content/",), settings_overrides=None, no_meta=False,
                 ignore_page=False, reject_filters=False, ignore_filters=False, max_limit=100,
                 html_everywhere=False, fail_first=0, posts=None, remove_after_page=None, cached_pages=(),
                 echo_query_status=None, publish_after_page=None):
        self.api_paths = api_paths
        self.no_meta, self.ignore_page, self.max_limit = no_meta, ignore_page, max_limit
        self.reject_filters, self.ignore_filters = reject_filters, ignore_filters
        self.html_everywhere, self.fail_first = html_everywhere, fail_first
        # {posts page number: [post ids]}: those posts are unpublished right after that page is served,
        # as when an editor unpublishes or deletes a post while a long import runs.
        self.remove_after_page = dict(remove_after_page or {})
        # {posts page number: [post dicts]}: published right after that page is served.
        self.publish_after_page = dict(publish_after_page or {})
        # Posts pages a CDN keeps serving as first seen, whatever has changed since.
        self.cached_pages, self.cache = set(cached_pages), {}
        # Answer every request with this status and a plain-text body that echoes the request line,
        # as some proxies and app servers do.
        self.echo_query_status = echo_query_status
        self.requests = []
        self.lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.settings = self._load("settings.json")
        self.settings["settings"].update(settings_overrides or {})
        self.collections = {"posts": posts if posts is not None else self._load("posts.json"),
                            "pages": self._load("pages.json")}
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    @staticmethod
    def _load(name):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def _handler_class(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.handle(self)

            def log_message(self, *args):
                pass

        return Handler

    # --- helpers

    def requests_for(self, resource):
        return [r for r in self.requests if r["resource"] == resource]

    def pages_requested(self, resource="posts"):
        return [int(r["query"].get("page", 1)) for r in self.requests_for(resource)]

    def _send(self, req, status, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
        req.send_response(status)
        req.send_header("Content-Type", ctype)
        req.send_header("Content-Length", str(len(data)))
        req.send_header("Content-Version", "v6.4")
        req.end_headers()
        req.wfile.write(data)

    def _error(self, req, status, etype, message, context=None):
        self._send(req, status, {"errors": [{"message": message, "context": context, "type": etype,
                                             "details": None, "property": None, "help": None, "code": None,
                                             "id": "0e1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"}]})

    # --- routing

    def handle(self, req):
        parts = urllib.parse.urlsplit(req.path)
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        resource = None
        for prefix in self.api_paths:
            if parts.path.startswith(prefix):
                resource = parts.path[len(prefix):].strip("/")
        with self.lock:
            self.requests.append({"path": parts.path, "resource": resource, "query": query,
                                  "headers": dict(req.headers.items())})
            failing = self.fail_first > 0
            if failing:
                self.fail_first -= 1
        if self.html_everywhere:
            return self._send(req, 200, "<html><body><h1>Welcome to our shop</h1></body></html>", "text/html")
        if self.echo_query_status:
            return self._send(req, self.echo_query_status, f"Bad request: GET {req.path} rejected by policy",
                              "text/plain")
        if failing:
            return self._send(req, 503, "<html><body>Service Unavailable</body></html>", "text/html")
        if resource is None:
            return self._send(req, 404, "<html><head><title>404 — Page not found</title></head></html>",
                              "text/html; charset=utf-8")
        if "key" not in query:
            return self._error(req, 403, "NoPermissionError", "Authorization failed",
                               "Unable to determine the authenticated member or integration. Check the "
                               "supplied Content API Key and ensure cookies are being passed through if "
                               "member auth is failing.")
        if query["key"] != KEY:
            return self._error(req, 401, "UnauthorizedError", "Unknown Content API Key")
        if resource == "settings":
            return self._send(req, 200, self.settings)
        if resource not in self.collections:
            return self._error(req, 404, "NotFoundError", "Resource not found")
        return self._browse(req, resource, query)

    def _browse(self, req, resource, query):
        page_no = int(query.get("page") or 1)
        if resource == "posts" and page_no in self.cached_pages and page_no in self.cache:
            return self._send(req, 200, self.cache[page_no])
        status, body = self._browse_body(resource, query)
        if resource == "posts" and status == 200:
            if page_no in self.cached_pages:
                self.cache.setdefault(page_no, body)
            gone = set(self.remove_after_page.pop(page_no, []))
            if gone:
                self.collections["posts"] = [p for p in self.collections["posts"] if p.get("id") not in gone]
            self.collections["posts"] = self.collections["posts"] + self.publish_after_page.pop(page_no, [])
        if status != 200:
            return self._error(req, status, *body)
        return self._send(req, 200, body)

    def _browse_body(self, resource, query):
        """(200, body) or (status, (error type, message, context))."""
        items = copy.deepcopy(self.collections[resource])
        nql = query.get("filter")
        if nql and self.reject_filters:
            return 400, ("BadRequestError", "Error parsing filter", "Query Error: unexpected character")
        if nql and not self.ignore_filters:
            try:
                items = [i for i in items if self._matches(i, nql)]
            except ValueError as e:
                return 400, ("BadRequestError", "Error parsing filter", str(e))
        items = self._order(items, query.get("order"))
        limit = query.get("limit") or "15"
        limit = self.max_limit if limit == "all" else min(int(limit), self.max_limit)
        page = 1 if self.ignore_page else max(1, int(query.get("page") or 1))
        total = len(items)
        pages = max(1, math.ceil(total / limit))
        chunk = items[(page - 1) * limit: page * limit]
        include = set(filter(None, query.get("include", "").split(",")))
        for item in chunk:
            if "authors" not in include:
                item.pop("authors", None)
                item.pop("primary_author", None)
            if "tags" not in include:
                item.pop("tags", None)
                item.pop("primary_tag", None)
            if "tiers" not in include:
                item.pop("tiers", None)
        body = {resource: chunk}
        if not self.no_meta:
            body["meta"] = {"pagination": {"page": page, "limit": limit, "pages": pages, "total": total,
                                           "next": page + 1 if page < pages else None,
                                           "prev": page - 1 if page > 1 else None}}
        return 200, body

    @staticmethod
    def _order(items, order):
        if not order:  # Ghost's default for the Content API: newest first
            return sorted(items, key=lambda i: (_parse_ghost_time(i.get("published_at"))
                                                or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        for clause in reversed([c.strip() for c in order.split(",") if c.strip()]):
            field, _, direction = clause.partition(" ")
            if field == "published_at":
                key = lambda i: (_parse_ghost_time(i.get("published_at")) is not None,  # noqa: E731
                                 _parse_ghost_time(i.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc))
            elif field == "id":
                key = lambda i: str(i.get("id") or "")  # noqa: E731
            else:
                continue  # Ghost ignores fields it can't order by
            items = sorted(items, key=key, reverse=direction.strip().lower() == "desc")
        return items

    @staticmethod
    def _split(nql, sep):
        parts, depth, quote, cur = [], 0, None, ""
        for ch in nql:
            if quote:
                quote = None if ch == quote else quote
            elif ch in "'\"":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == sep and depth == 0:
                parts.append(cur)
                cur = ""
                continue
            cur += ch
        parts.append(cur)
        return parts

    def _matches(self, item, nql):
        nql = nql.strip()
        and_parts = self._split(nql, "+")
        if len(and_parts) > 1:
            return all(self._matches(item, p) for p in and_parts)
        or_parts = self._split(nql, ",")
        if len(or_parts) > 1:
            return any(self._matches(item, p) for p in or_parts)
        if nql.startswith("(") and nql.endswith(")"):
            return self._matches(item, nql[1:-1])
        m = re.fullmatch(r"published_at:(>=|<=|>|<)'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)'", nql)
        if m:
            when = _parse_ghost_time(item.get("published_at"))
            bound = datetime.strptime(m.group(2), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if when is None:
                return False
            return {">=": when >= bound, "<=": when <= bound, ">": when > bound, "<": when < bound}[m.group(1)]
        m = re.fullmatch(r"(tag|tags\.slug|author|authors\.slug):([a-z0-9-]+)", nql)
        if m:
            field = "tags" if m.group(1).startswith("tag") else "authors"
            return any(t.get("slug") == m.group(2) for t in item.get(field) or [])
        raise ValueError(f"unsupported filter {nql!r}")


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k != ghost.KEY_ENV}
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=True)


def build_parser():
    with mock.patch.object(import_archive, "IMPORTERS", ["ghost"]):
        return import_archive.build_parser()


class GhostTestCase(unittest.TestCase):
    def fake(self, **options):
        server = FakeGhost(**options)
        self.addCleanup(server.close)
        return server

    def parse(self, *argv):
        parser, _ = build_parser()
        return parser.parse_args(["ghost", *argv])

    def run_import(self, *argv, env=None):
        """iter_articles + normalize, as the CLI does. Returns (articles by id, report, stderr, raws)."""
        args = self.parse(*argv)
        err = io.StringIO()
        with clean_env(**({ghost.KEY_ENV: KEY} if env is None else env)), redirect_stderr(err):
            raws = list(ghost.iter_articles(args))
        report = ImportReport(source="ghost")
        articles = {}
        for raw in raws:
            article = normalize(raw, report)
            if article:
                articles[article["id"]] = article
        return articles, report, err.getvalue(), raws

    def import_error(self, *argv, env=None):
        args = self.parse(*argv)
        err = io.StringIO()
        with clean_env(**({ghost.KEY_ENV: KEY} if env is None else env)), redirect_stderr(err), \
                self.assertRaises(ImporterError) as ctx:
            list(ghost.iter_articles(args))
        return str(ctx.exception), err.getvalue()


class PaginationAndMappingTests(GhostTestCase):
    def test_reads_every_page_oldest_first(self):
        g = self.fake()
        articles, report, err, raws = self.run_import("--url", g.base, "--per-page", "4")
        self.assertEqual(list(articles), [gid(s) for s in PUBLIC])
        self.assertEqual(len(raws), 19)
        self.assertEqual(g.pages_requested(), [1, 2, 3, 4, 5])
        first = g.requests_for("posts")[0]["query"]
        self.assertEqual(first["limit"], "4")
        self.assertEqual(first["include"], "authors")
        self.assertEqual(first["order"], "published_at asc,id asc")
        self.assertEqual(first["key"], KEY)
        self.assertNotIn("filter", first)
        self.assertEqual(g.requests[0]["resource"], "settings")
        self.assertIn("Connected to The Harbor Ledger (https://news.harborledger.example/), Ghost 6.4.", err)
        self.assertIn("posts: 19 published to read", err)
        self.assertIn("posts: page 5/5 (19 read)", err)
        self.assertNotIn("pages", [r["resource"] for r in g.requests])

    def test_fields_entities_and_text(self):
        g = self.fake()
        articles, _, _, _ = self.run_import("--url", g.base)
        budget = articles[gid("a00003")]
        self.assertEqual(budget["headline"], "Council’s budget vote & what it means for schools")
        self.assertEqual(budget["content"], "The council approved a $1.2 billion budget on Thursday.\n\nSchools\n\n"
                                            "Districts get 4% more than last year.")
        self.assertEqual(budget["url"], "https://news.harborledger.example/council-budget-vote-schools/")
        self.assertEqual(budget["authors"], ["María González"])
        self.assertEqual(articles[gid("a00004")]["authors"], ["Sam O'Neil", "Priya Natarajan", "Dana Whitfield"])
        self.assertEqual(articles[gid("a00004")]["content"],
                         "The port authority wants to deepen the main channel to 45 feet.\n\n"
                         "A dredge working the outer channel\n\nState and federal grants would cover most of the cost.")
        phones = articles[gid("a00019")]["content"]
        self.assertEqual(phones, "AT&T and Verizon crews restored service by noon.\n\n"
                                 "Outages peaked at 12 000 homes < a record.")

    def test_missing_byline_is_left_empty(self):
        g = self.fake()
        articles, _, _, raws = self.run_import("--url", g.base)
        self.assertEqual(articles[gid("a00008")]["authors"], [])
        self.assertFalse([r for r in raws if r.get("authors") and any("staff" in a.lower() for a in r["authors"])])

    def test_primary_author_used_when_authors_list_is_absent(self):
        post = json.loads((FIXTURES / "posts.json").read_text(encoding="utf-8"))[2]
        post.pop("authors")
        raw = ghost._convert(post, "posts", SITE, False, (None, None))
        self.assertEqual(raw["authors"], ["María González"])

    def test_dates_are_converted_to_utc(self):
        g = self.fake()
        articles, _, _, _ = self.run_import("--url", g.base)
        self.assertEqual(articles[gid("a00003")]["publish_date"], "2024-01-08T14:05:00Z")  # +00:00 form
        self.assertEqual(articles[gid("a00004")]["publish_date"], "2024-02-01T17:30:00Z")  # Z form
        self.assertEqual(articles[gid("a00009")]["publish_date"], "2024-03-10T06:30:00Z")  # -05:00 offset

    def test_skips_non_public_and_malformed_with_reasons(self):
        g = self.fake()
        _, report, _, _ = self.run_import("--url", g.base)
        self.assertEqual(dict(report.skipped), EXPECTED_SKIPS)

    def test_ids_are_stable_and_namespaced(self):
        g = self.fake()
        first, _, _, _ = self.run_import("--url", g.base, "--per-page", "3")
        second, _, _, _ = self.run_import("--url", g.base)
        self.assertEqual(list(first), list(second))
        self.assertTrue(all(re.fullmatch(r"ghost-[0-9a-f]{24}", i) for i in first))

    def test_relative_url_is_resolved_against_the_site_address(self):
        g = self.fake()
        articles, _, _, _ = self.run_import("--url", g.base)
        self.assertEqual(articles[gid("a00013")]["url"], "https://news.harborledger.example/ferry-schedule-changes/")

    def test_card_interface_text_is_left_out(self):
        g = self.fake()
        articles, _, _, _ = self.run_import("--url", g.base)
        text = articles[gid("a00010")]["content"]
        for kept in ("Crews pulled 40 tons of debris", "Harbor cleanup crews at dawn",
                     "Episode 12: Cleaning up the harbor", "Read the full audit",
                     "State auditor's report, March 2024", "The federal cleanup plan",
                     "The city expects the work to finish in 2026.",
                     "Residents can comment at the next commission meeting."):
            self.assertIn(kept, text)
        for dropped in ("0:00", "2:00", "31:04", "1×", "2 MB", "harbor-audit.pdf", "Superfund site profile",
                        "Cleanup activities", "Subscribe", "Email sent", "Sign up for", "No spam",
                        "Support local news", "download-circle", "kg-card"):
            self.assertNotIn(dropped, text)

    def test_server_page_cap_is_followed(self):
        g = self.fake(max_limit=5)
        articles, _, _, _ = self.run_import("--url", g.base, "--per-page", "100")
        self.assertEqual(list(articles), [gid(s) for s in PUBLIC])
        self.assertEqual(g.pages_requested(), [1, 2, 3, 4])

    def test_without_pagination_meta_stops_on_short_page(self):
        g = self.fake(no_meta=True)
        articles, _, _, raws = self.run_import("--url", g.base, "--per-page", "4")
        self.assertEqual(len(raws), 19)
        self.assertEqual(list(articles), [gid(s) for s in PUBLIC])
        self.assertEqual(g.pages_requested(), [1, 2, 3, 4, 5])

    def test_cache_that_ignores_page_number_is_reported(self):
        g = self.fake(ignore_page=True)
        message, _ = self.import_error("--url", g.base, "--per-page", "4")
        self.assertIn("same posts for page 2", message)
        self.assertEqual(g.pages_requested(), [1, 2])

    def test_empty_site(self):
        g = self.fake(posts=[])
        articles, report, err, raws = self.run_import("--url", g.base)
        self.assertEqual((articles, raws), ({}, []))
        self.assertIn("posts: 0 published to read", err)


class ChangingSiteAndOddPagingTests(GhostTestCase):
    """Offset pages shift when posts disappear mid-import; servers without meta may cap pages."""

    A00001 = "65a0c1f2e4b0a10001a00001"

    def test_post_unpublished_mid_import_does_not_push_another_off_its_page(self):
        # Page 1 is a00011, a00018, a00001, a00002. Once a00001 goes, a00003 moves up onto page 1,
        # so page 2 starts at a00004 and a00003 would never be read.
        g = self.fake(remove_after_page={1: [self.A00001]})
        articles, report, err, raws = self.run_import("--url", g.base, "--per-page", "4")
        self.assertEqual(sorted(articles), sorted(gid(s) for s in PUBLIC))
        ids = [r["id"] for r in raws if r.get("id")]
        self.assertEqual(len(ids), len(set(ids)), "a re-read page passed posts on twice")
        self.assertIn("posts: 1 removed from the site during the import; reading again from page 1", err)
        self.assertEqual(g.pages_requested(), [1, 2, 1, 2, 3, 4, 5])
        self.assertEqual(dict(report.skipped), EXPECTED_SKIPS)

    def test_several_removals_go_back_far_enough(self):
        # Five posts on pages 1-2 disappear after page 2 (per page 2): page 3 has moved 5 places, so the
        # posts that slid back onto pages 1-2 must be read again from page 1.
        gone = ["65a0c1f2e4b0a10001a000" + s for s in ("11", "18", "01", "02", "03")]
        g = self.fake(remove_after_page={2: gone})
        articles, _, err, raws = self.run_import("--url", g.base, "--per-page", "2")
        # a00001 and a00002 were read before they went; a00003 went before it was read.
        self.assertEqual(sorted(articles), sorted(gid(s) for s in PUBLIC if s != "a00003"))
        ids = [r["id"] for r in raws if r.get("id")]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("posts: 5 removed from the site during the import; reading again from page 1", err)

    def test_removal_after_a_new_post_is_published_is_still_noticed(self):
        # On a busy site the total rises (a new post, at the end) before it falls (an old post goes):
        # the fall is what matters, even though the total never goes below where it started.
        new = copy.deepcopy(json.loads((FIXTURES / "posts.json").read_text(encoding="utf-8"))[0])
        new.update(id="65a0c1f2e4b0a10001a00020", title="Storm cleanup begins", slug="storm-cleanup-begins",
                   uuid="2f0c1e2d-3b4a-4c5d-8e6f-7a8b9c0d1e20", url=SITE + "storm-cleanup-begins/",
                   published_at="2024-06-01T12:00:00.000+00:00")
        g = self.fake(publish_after_page={1: [new]}, remove_after_page={2: [self.A00001]})
        articles, report, err, _ = self.run_import("--url", g.base, "--per-page", "4")
        self.assertEqual(sorted(articles), sorted([gid(s) for s in PUBLIC] + [gid("a00020")]))
        self.assertEqual(dict(report.skipped), EXPECTED_SKIPS)  # a00007 (tiers) moved up and is still counted
        self.assertEqual(g.pages_requested(), [1, 2, 3, 2, 3, 4, 5])
        self.assertIn("posts: 1 removed from the site during the import; reading again from page 2", err)

    def test_backdated_posts_added_mid_import_are_not_taken_for_a_looping_cache(self):
        # Pages of 2: a00011, a00018 | a00001, a00002 | ... A migration then adds two 2015 posts, which
        # sort after the two undated ones, so page 3 is exactly the old page 2. Ghost's total went up:
        # that's new posts, not a cache ignoring `page`. (The two old posts landed behind the import;
        # a later re-run picks them up.)
        old = []
        for n in range(2):
            post = copy.deepcopy(json.loads((FIXTURES / "posts.json").read_text(encoding="utf-8"))[0])
            post.update(id=f"65a0c1f2e4b0a10001c0000{n}", slug=f"from-the-archive-{n}", url=SITE + f"archive-{n}/",
                        published_at=f"2015-0{n + 1}-01T12:00:00.000+00:00")
            old.append(post)
        g = self.fake(publish_after_page={2: old})
        articles, report, _, raws = self.run_import("--url", g.base, "--per-page", "2")
        self.assertEqual(sorted(articles), sorted(gid(s) for s in PUBLIC))
        self.assertEqual(dict(report.skipped), EXPECTED_SKIPS)
        ids = [r["id"] for r in raws if r.get("id")]
        self.assertEqual(len(ids), len(set(ids)))

    def test_a_cached_page_cannot_make_the_re_read_loop(self):
        # A CDN keeps serving the old page 1 (old total): the importer must still finish.
        g = self.fake(remove_after_page={1: [self.A00001]}, cached_pages={1})
        articles, _, _, _ = self.run_import("--url", g.base, "--per-page", "4")
        self.assertEqual(g.pages_requested(), [1, 2, 1, 2, 3, 4, 5])
        self.assertTrue({gid(s) for s in PUBLIC if s not in ("a00001", "a00002", "a00003")} <= set(articles))

    def test_server_page_cap_without_pagination_meta(self):
        # No meta and a server that caps pages at 5: a page of 5 when 100 were asked for isn't the end.
        g = self.fake(no_meta=True, max_limit=5)
        articles, _, _, raws = self.run_import("--url", g.base, "--per-page", "100")
        self.assertEqual(len(raws), 19)
        self.assertEqual(list(articles), [gid(s) for s in PUBLIC])
        self.assertEqual(g.pages_requested(), [1, 2, 3, 4])

    def test_single_short_page_without_meta_from_a_server_that_ignores_page(self):
        # The extra request that confirms the end must not be mistaken for a looping cache.
        posts = json.loads((FIXTURES / "posts.json").read_text(encoding="utf-8"))[:4]
        g = self.fake(no_meta=True, ignore_page=True, posts=posts)
        articles, _, _, raws = self.run_import("--url", g.base)
        self.assertEqual(list(articles), [gid(s) for s in ("a00001", "a00002", "a00003", "a00004")])
        self.assertEqual(len(raws), 4)
        self.assertEqual(g.pages_requested(), [1, 2])


class KeyExposureTests(GhostTestCase):
    def test_url_with_a_space_or_control_character_is_refused_without_showing_the_key(self):
        g = self.fake()
        for bad in (g.base + "/news room", g.base + "/blog\x07", g.base + "/blog\u00a0x", "https://exa mple.com/"):
            message, err = self.import_error("--url", bad)
            self.assertIn("--url", message, repr(bad))
            self.assertNotIn(KEY, message + err, repr(bad))
        self.assertEqual(g.requests, [])

    def test_non_ascii_subfolder_is_percent_encoded(self):
        self.assertEqual(ghost._parse_url("https://example.com/blög/"), ("https://example.com/bl%C3%B6g", None))
        self.assertEqual(ghost._parse_url("https://example.com/a%20b/"), ("https://example.com/a%20b", None))
        g = self.fake()
        message, err = self.import_error("--url", g.base + "/blög")
        self.assertIn("No Ghost Content API found", message)
        self.assertEqual(g.requests[0]["path"], "/bl%C3%B6g/ghost/api/content/settings/")

    def test_error_body_that_echoes_the_request_does_not_show_the_key(self):
        g = self.fake(echo_query_status=400)
        message, err = self.import_error("--url", g.base)
        self.assertIn("HTTP 400", message)
        self.assertIn("rejected by policy", message)
        self.assertNotIn(KEY, message + err)

    def test_low_level_request_errors_become_importer_errors_without_the_key(self):
        with clean_env(), self.assertRaises(ImporterError) as ctx:
            ghost._get("http://127.0.0.1:9/a b/settings/", {"key": KEY})
        self.assertNotIn(KEY, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)


class MembersAndPagesTests(GhostTestCase):
    def test_member_previews_are_imported_only_with_the_flag(self):
        g = self.fake()
        articles, report, _, _ = self.run_import("--url", g.base, "--include-member-previews")
        self.assertEqual(articles[gid("a00005")]["content"],
                         "For three months, developers met privately with zoning officials.")
        self.assertEqual(articles[gid("a00007")]["content"],
                         "Our data team mapped every flood claim filed since 2010.")
        self.assertNotIn(gid("a00006"), articles)
        self.assertEqual(report.skipped["members-only (paid) with no public preview"], 1)
        self.assertFalse([r for r in report.skipped if "only a preview is public" in r])
        self.assertEqual(len(articles), len(PUBLIC) + 2)

    def test_access_false_is_gated_even_without_visibility(self):
        post = {"id": "65a0c1f2e4b0a10001a00099", "title": "T", "html": "<p>Preview</p>", "access": False,
                "url": SITE + "t/", "published_at": "2024-01-01T00:00:00.000Z"}
        raw = ghost._convert(post, "posts", SITE, False, (None, None))
        self.assertEqual(raw, {"_skip": "members-only (access: false): only a preview is public "
                                        "(--include-member-previews imports it)"})

    def test_unexpected_visibility_value_is_sanitized_and_gated(self):
        post = {"id": "x1", "title": "T", "html": "<p>P</p>", "visibility": "<b>vip</b>\n" + "z" * 80,
                "url": SITE + "t/", "published_at": "2024-01-01T00:00:00.000Z"}
        reason = ghost._convert(post, "posts", SITE, False, (None, None))["_skip"]
        self.assertTrue(reason.startswith("members-only (bvipb"), reason)
        self.assertLess(len(reason), 140)

    def test_pages_only_with_flag(self):
        g = self.fake()
        articles, report, _, _ = self.run_import("--url", g.base, "--include-pages")
        about = articles[gid("b00001")]
        self.assertEqual(about["headline"], "About The Harbor Ledger")
        self.assertEqual(about["url"], "https://news.harborledger.example/about/")
        self.assertEqual(report.skipped["members-only (members): only a preview is public "
                                        "(--include-member-previews imports it)"], 2)
        self.assertEqual(g.requests_for("pages")[0]["query"]["include"], "authors")


class ConnectionTests(GhostTestCase):
    def test_legacy_versioned_api_path(self):
        g = self.fake(api_paths=("/ghost/api/v3/content/",))
        articles, _, _, _ = self.run_import("--url", g.base)
        self.assertEqual(list(articles), [gid(s) for s in PUBLIC])
        self.assertEqual([r["path"] for r in g.requests[:3]],
                         ["/ghost/api/content/settings/", "/ghost/api/v4/content/settings/",
                          "/ghost/api/v3/content/settings/"])
        self.assertTrue(all(r["path"].startswith("/ghost/api/v3/") for r in g.requests[3:]))

    def test_url_forms_are_accepted(self):
        cases = {
            "example.com": ("https://example.com", None),
            "https://www.Example.com/": ("https://www.example.com", None),
            "https://example.com/ghost/#/settings/integrations": ("https://example.com", None),
            "https://example.com/blog/ghost/api/content/posts/?key=abc": ("https://example.com/blog", "abc"),
            "http://user:secret@127.0.0.1:2368/": ("http://127.0.0.1:2368", None),
            "https://example.com/ghost-news/": ("https://example.com/ghost-news", None),
        }
        for raw, expected in cases.items():
            self.assertEqual(ghost._parse_url(raw), expected, raw)
        for bad in ("", "ftp://example.com", "https://", "https://example.com:99999/"):
            with self.assertRaises(ImporterError, msg=bad):
                ghost._parse_url(bad)

    def test_url_pointing_at_the_admin_or_api_works(self):
        g = self.fake()
        articles, _, _, _ = self.run_import("--url", g.base + "/ghost/api/content/")
        self.assertEqual(len(articles), len(PUBLIC))

    def test_not_a_ghost_site(self):
        g = self.fake(html_everywhere=True)
        message, _ = self.import_error("--url", g.base)
        self.assertIn("No Ghost Content API found", message)
        self.assertIn("Integrations", message)

    def test_nothing_at_the_api_path(self):
        g = self.fake(api_paths=())
        message, _ = self.import_error("--url", g.base)
        self.assertIn(f"No Ghost Content API found at {g.base}/ghost/api/content/", message)
        self.assertEqual(len(g.requests), 4)

    def test_wrong_key_is_explained_without_echoing_it(self):
        g = self.fake()
        wrong = "0123456789abcdef0123456789"
        message, err = self.import_error("--url", g.base, env={ghost.KEY_ENV: wrong})
        self.assertIn("Unknown Content API Key", message)
        self.assertIn("Integrations", message)
        self.assertNotIn(wrong, message + err)
        self.assertEqual(len(g.requests), 1)

    def test_missing_key(self):
        g = self.fake()
        message, _ = self.import_error("--url", g.base, env={})
        self.assertIn(ghost.KEY_ENV, message)
        self.assertIn("Add custom integration", message)
        self.assertEqual(g.requests, [])

    def test_admin_api_key_is_refused_without_echoing_it(self):
        g = self.fake()
        message, err = self.import_error("--url", g.base, env={ghost.KEY_ENV: ADMIN_KEY})
        self.assertIn("Admin API key", message)
        self.assertIn("Content API key", message)
        self.assertNotIn(ADMIN_KEY.split(":")[1], message + err)
        self.assertEqual(g.requests, [])

    def test_key_flag_and_key_in_url(self):
        g = self.fake()
        articles, _, err, _ = self.run_import("--url", g.base, "--key", KEY, env={})
        self.assertEqual(len(articles), len(PUBLIC))
        self.assertNotIn(KEY, err)
        articles, _, err, _ = self.run_import("--url", f"{g.base}/ghost/api/content/posts/?key={KEY}", env={})
        self.assertEqual(len(articles), len(PUBLIC))
        self.assertIn(ghost.KEY_ENV, err)
        self.assertNotIn(KEY, err)

    def test_flag_wins_over_environment(self):
        g = self.fake()
        articles, _, _, _ = self.run_import("--url", g.base, "--content-api-key", KEY,
                                            env={ghost.KEY_ENV: "ffffffffffffffffffffffffff"})
        self.assertEqual(len(articles), len(PUBLIC))

    def test_odd_looking_key_is_tried_with_a_note(self):
        g = self.fake()
        message, err = self.import_error("--url", g.base, env={ghost.KEY_ENV: "not-a-real-key"})
        self.assertIn("doesn't look like a Ghost Content API key", err)
        self.assertIn("Unknown Content API Key", message)
        self.assertNotIn("not-a-real-key", message + err)

    def test_retry_messages_never_show_the_key(self):
        g = self.fake(fail_first=1)
        with mock.patch("importers.common.time.sleep"):
            articles, _, err, _ = self.run_import("--url", g.base)
        self.assertEqual(len(articles), len(PUBLIC))
        self.assertIn("HTTP 503", err)
        self.assertIn("key=***", err)  # redact_url leaves the mask readable
        self.assertNotIn(KEY, err)

    def test_requests_use_the_generic_user_agent(self):
        g = self.fake()
        self.run_import("--url", g.base)
        agents = {r["headers"].get("User-Agent") for r in g.requests}
        self.assertEqual(len(agents), 1)
        self.assertTrue(agents.pop().startswith("Dewey-archive-importer/"))


class DateWindowAndFilterTests(GhostTestCase):
    WINDOW = ["--after", "2023-12-21", "--before", "2024-02-01"]

    def test_window_is_sent_as_site_local_bounds(self):
        g = self.fake()
        articles, report, err, _ = self.run_import("--url", g.base, *self.WINDOW)
        # New York midnight is 05:00 UTC in winter: the 03:00 UTC post on Dec 21 is still Dec 20 there.
        self.assertEqual(g.requests_for("posts")[0]["query"]["filter"],
                         "published_at:>='2023-12-21 05:00:00'+published_at:<'2024-02-01 05:00:00'")
        self.assertEqual(list(articles), [gid("a00003")])
        self.assertEqual(dict(report.skipped), {})
        self.assertIn("America/New_York", err)

    def test_summer_bounds_follow_daylight_saving_time(self):
        start, end = ghost._window(argparse.Namespace(after=datetime(2024, 7, 1).date(),
                                                      before=datetime(2024, 12, 1).date()),
                                   ghost._site_zone({"timezone": "America/New_York"})[0])
        self.assertEqual((start.hour, end.hour), (4, 5))

    def test_unknown_time_zone_falls_back_to_utc_with_a_note(self):
        g = self.fake(settings_overrides={"timezone": "Mars/Olympus_Mons"})
        articles, _, err, _ = self.run_import("--url", g.base, *self.WINDOW)
        self.assertEqual(g.requests_for("posts")[0]["query"]["filter"],
                         "published_at:>='2023-12-21 00:00:00'+published_at:<'2024-02-01 00:00:00'")
        self.assertEqual(list(articles), [gid("a00002"), gid("a00003")])
        self.assertIn("couldn't load the site's time zone", err)

    def test_rejected_date_filter_falls_back_to_checking_dates_here(self):
        g = self.fake(reject_filters=True)
        articles, report, err, _ = self.run_import("--url", g.base, *self.WINDOW)
        self.assertEqual(list(articles), [gid("a00003")])
        self.assertIn("didn't accept the filter (Error parsing filter); retrying without the date part", err)
        self.assertNotIn("filter", g.requests_for("posts")[-1]["query"])
        self.assertGreater(report.skipped["published outside the --after/--before dates"], 0)

    def test_ignored_date_filter_is_still_respected(self):
        g = self.fake(ignore_filters=True)
        articles, report, _, _ = self.run_import("--url", g.base, *self.WINDOW)
        self.assertEqual(list(articles), [gid("a00003")])
        self.assertEqual(report.skipped["published outside the --after/--before dates"], 11)

    def test_after_must_precede_before(self):
        g = self.fake()
        message, _ = self.import_error("--url", g.base, "--after", "2024-02-01", "--before", "2024-02-01")
        self.assertIn("must be earlier than --before", message)
        self.assertEqual(g.requests, [])

    def test_user_filter_is_combined_with_dates(self):
        g = self.fake()
        articles, _, _, _ = self.run_import("--url", g.base, "--filter", "tag:city-hall", "--before", "2024-01-01")
        self.assertEqual(g.requests_for("posts")[0]["query"]["filter"],
                         "published_at:<'2024-01-01 05:00:00'+(tag:city-hall)")
        self.assertEqual(list(articles), [gid("a00001"), gid("a00002")])

    def test_user_filter_with_or_keeps_its_meaning(self):
        g = self.fake()
        articles, _, _, _ = self.run_import("--url", g.base, "--filter", "tag:education,tag:port",
                                            "--after", "2024-01-01")
        self.assertEqual(sorted(articles), [gid("a00003"), gid("a00004"), gid("a00010")])

    def test_rejected_user_filter_is_an_error(self):
        g = self.fake()
        message, _ = self.import_error("--url", g.base, "--filter", "featured:maybe")
        self.assertIn("Ghost rejected the filter (Error parsing filter)", message)

    def test_rejected_user_filter_with_dates_retries_without_dates_then_fails(self):
        g = self.fake()
        message, err = self.import_error("--url", g.base, "--filter", "featured:maybe", "--after", "2024-01-01")
        self.assertIn("retrying without the date part", err)
        self.assertIn("Ghost rejected the filter (Error parsing filter)", message)
        self.assertEqual([r["query"]["filter"] for r in g.requests_for("posts")],
                         ["published_at:>='2024-01-01 05:00:00'+(featured:maybe)", "(featured:maybe)"])

    def test_default_prefix(self):
        cases = [
            (["--url", "https://www.example.com/"], "ghost-example-com"),
            (["--url", "https://example.ghost.io/ghost/"], "ghost-example-ghost-io"),
            (["--url", "http://127.0.0.1:2368/blog"], "ghost-127-0-0-1-2368-blog"),
            (["--url", "https://example.com", "--after", "2023-01-01", "--before", "2024-01-01"],
             "ghost-example-com-after-20230101-before-20240101"),
            (["--url", "ftp://nope"], "ghost-site"),
        ]
        for argv, expected in cases:
            self.assertEqual(ghost.default_prefix(self.parse(*argv)), expected, argv)
        with_filter = ghost.default_prefix(self.parse("--url", "https://example.com", "--filter", "tag:news"))
        self.assertRegex(with_filter, r"^ghost-example-com-filter-[0-9a-f]{8}$")
        self.assertEqual(with_filter, ghost.default_prefix(self.parse("--url", "https://example.com",
                                                                      "--filter", "tag:news")))
        self.assertNotIn(KEY, ghost.default_prefix(self.parse("--url", f"https://example.com/?key={KEY}")))

    def test_argument_validation(self):
        parser, _ = build_parser()
        for argv in (["--per-page", "0"], ["--per-page", "101"], ["--after", "01/02/2024"], ["--filter", " "]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(["ghost", "--url", "https://example.com", *argv])


class HtmlCleanupTests(unittest.TestCase):
    def test_unclosed_chrome_element_keeps_everything(self):
        markup = '<p>Lead.</p><div class="kg-audio-player"><span>0:00</span><p>Rest of the story.</p>'
        self.assertEqual(ghost._strip_chrome(markup), markup)

    def test_nested_elements_inside_a_dropped_card(self):
        markup = ('<p>Before.</p><div class="kg-video-player-container"><div><div>0:00</div></div>'
                  '<div>1×</div></div><p>After.</p>')
        self.assertEqual(html_to_text(ghost._strip_chrome(markup)), "Before.\n\nAfter.")

    def test_entities_and_literal_brackets_survive(self):
        markup = "<p>Fish &amp; chips &lt;b&gt;not bold&lt;/b&gt; &#8217;s</p><!-- note -->"
        self.assertEqual(html_to_text(ghost._strip_chrome(markup)), "Fish & chips <b>not bold</b> ’s")

    def test_scripts_and_void_chrome(self):
        markup = ('<p>Text</p><img class="kg-audio-thumbnail" src="x.jpg"><input type="email">'
                  '<script>var a = "<p>";</script><p>More</p>')
        self.assertEqual(html_to_text(ghost._strip_chrome(markup)), "Text\n\nMore")

    def test_plain_text_passes_through(self):
        self.assertEqual(ghost._strip_chrome("No markup & fine"), "No markup & fine")
        self.assertEqual(ghost._strip_chrome(""), "")


class HelpTests(unittest.TestCase):
    def test_help_says_where_to_find_each_value(self):
        parser, _ = build_parser()
        sub = parser._subparsers._group_actions[0].choices["ghost"]
        text = sub.format_help()
        for needle in ("Integrations", "Add custom integration", "API URL", "Content API key", "Admin API key",
                       ghost.KEY_ENV, "shell history", "--include-member-previews", "--include-pages",
                       "paywall", "Drafts", "site's time zone"):
            self.assertIn(needle, text)
        self.assertNotIn("\n", ghost.HELP)


class EndToEndTests(GhostTestCase):
    def run_cli(self, *argv, env=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out, err = io.StringIO(), io.StringIO()
        with clean_env(**({ghost.KEY_ENV: KEY} if env is None else env)), \
                mock.patch.object(import_archive, "IMPORTERS", ["ghost"]), redirect_stdout(out), redirect_stderr(err):
            code = import_archive.main(["ghost", *argv, "--out-dir", tmp.name])
        return code, pathlib.Path(tmp.name), out.getvalue(), err.getvalue()

    def test_cli_writes_articles_and_returns_zero(self):
        g = self.fake()
        code, out_dir, summary, err = self.run_cli("--url", g.base, "--per-page", "4")
        self.assertEqual(code, 0, err)
        port = g.base.rsplit(":", 1)[1]
        files = sorted(out_dir.iterdir())
        self.assertEqual([f.name for f in files], [f"ghost-127-0-0-1-{port}-0001.json"])
        written = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual([a["id"] for a in written], [gid(s) for s in PUBLIC])
        by_id = {a["id"]: a for a in written}
        self.assertEqual(by_id[gid("a00004")], {
            "id": gid("a00004"),
            "headline": "Port authority’s $40M dredging plan, explained",
            "content": "The port authority wants to deepen the main channel to 45 feet.\n\n"
                       "A dredge working the outer channel\n\nState and federal grants would cover most of the cost.",
            "url": "https://news.harborledger.example/port-dredging-plan/",
            "authors": ["Sam O'Neil", "Priya Natarajan", "Dana Whitfield"],
            "publish_date": "2024-02-01T17:30:00Z",
        })
        self.assertTrue(all(set(a) == {"id", "headline", "content", "url", "authors", "publish_date"}
                            for a in written))
        self.assertIn("Imported 9 article(s) from ghost.", summary)
        self.assertIn("Publish dates: 2023-12-20 to 2024-05-20", summary)
        self.assertIn("1 have no byline", summary)
        self.assertIn("1 skipped: members-only (paid): only a preview is public", summary)
        self.assertIn("2 skipped: no parseable publish date", summary)
        self.assertNotIn(KEY, summary + err)

    def test_cli_rerun_replaces_rather_than_duplicates(self):
        g = self.fake()
        code, out_dir, _, _ = self.run_cli("--url", g.base, "--prefix", "harbor")
        self.assertEqual(code, 0)
        first = json.loads((out_dir / "harbor-0001.json").read_text(encoding="utf-8"))
        with clean_env(**{ghost.KEY_ENV: KEY}), mock.patch.object(import_archive, "IMPORTERS", ["ghost"]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = import_archive.main(["ghost", "--url", g.base, "--prefix", "harbor", "--out-dir", str(out_dir)])
        self.assertEqual(code, 0)
        self.assertEqual([f.name for f in out_dir.iterdir()], ["harbor-0001.json"])
        self.assertEqual(json.loads((out_dir / "harbor-0001.json").read_text(encoding="utf-8")), first)

    def test_cli_limit_stops_early(self):
        g = self.fake()
        code, out_dir, _, _ = self.run_cli("--url", g.base, "--limit", "2", "--prefix", "trial")
        self.assertEqual(code, 0)
        written = json.loads((out_dir / "trial-0001.json").read_text(encoding="utf-8"))
        self.assertEqual([a["id"] for a in written], [gid("a00001"), gid("a00002")])
        # Page 1 holds the two posts without a usable date (sorted first, as MySQL sorts NULLs); the
        # import stops once two articles are written instead of reading all 10 pages.
        self.assertEqual(g.pages_requested(), [1, 2])
        self.assertEqual(g.requests_for("posts")[0]["query"]["limit"], "2")

    def test_cli_failure_returns_one_and_writes_nothing(self):
        g = self.fake()
        wrong = "0123456789abcdef0123456789"
        code, out_dir, _, err = self.run_cli("--url", g.base, env={ghost.KEY_ENV: wrong})
        self.assertEqual(code, 1)
        self.assertEqual(list(out_dir.iterdir()), [])
        self.assertIn("Import failed", err)
        self.assertNotIn(wrong, err)


if __name__ == "__main__":
    unittest.main()

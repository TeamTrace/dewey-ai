import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import argparse  # noqa: E402
import base64  # noqa: E402
import copy  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import unittest  # noqa: E402
import urllib.parse  # noqa: E402
from contextlib import redirect_stderr, redirect_stdout  # noqa: E402
from datetime import datetime  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from unittest import mock  # noqa: E402

import import_archive  # noqa: E402
from importers import drupal  # noqa: E402
from importers.common import ImporterError, ImportReport, normalize  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "drupal"
USERNAME, PASSWORD = "dewey-reader", "s3cret pass:word"
USER_UUID = "9b1f6c2e-0000-4000-8000-000000000042"
JSONAPI_HEADER = {"version": "1.0", "meta": {"links": {"self": {"href": "http://jsonapi.org/format/1.0/"}}}}
HIDDEN = "not visible to anonymous visitors (members-only or restricted content)"
VISIBLE_PUBLISHED = [101, 102, 108, 109, 110]  # what an anonymous import keeps, in nid order


def iso_ts(value):
    return int(datetime.fromisoformat(value).timestamp())


class FakeDrupal:
    """
    A stand-in for a Drupal 10 site with core JSON:API, serving the fixtures the way
    core does: the entry point's links, page[offset]/page[limit] (capped at 50) with a
    `next` link only when an N+1 query finds more rows, filter[status] and condition
    filters, sort, include, sparse fieldsets, label-only users for visitors without
    "access user profiles", meta.omitted for rows the visitor can't view, Basic Auth
    that falls back to anonymous on a wrong password, and JSON:API error documents.
    """

    def __init__(self, block_anonymous=False, jsonapi_enabled=True, html_entry=False, ignore_status_filter=False,
                 ignore_offset=False, no_links=False, next_host=None, include_count=False, restricted=(104,),
                 reject_sort=False, collection_status=None):
        self.block_anonymous, self.jsonapi_enabled, self.html_entry = block_anonymous, jsonapi_enabled, html_entry
        self.ignore_status_filter, self.ignore_offset, self.no_links = ignore_status_filter, ignore_offset, no_links
        self.next_host, self.include_count, self.restricted = next_host, include_count, set(restricted)
        self.reject_sort, self.collection_status = reject_sort, collection_status
        self.requests = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.key = f"127-0-0-1-{self.httpd.server_address[1]}"
        self.raw = {name: (FIXTURES / name).read_text(encoding="utf-8")
                    for name in ("entrypoint.json", "articles.json", "users.json", "terms.json", "paragraphs.json")}
        self.not_found = (FIXTURES / "not_found.html").read_text(encoding="utf-8")
        self.front_page = (FIXTURES / "front_page.html").read_text(encoding="utf-8")
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    def _load(self, name, base):
        return json.loads(self.raw[name].replace("{{BASE}}", base))

    def _handler_class(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.handle(self)

            def log_message(self, *args):
                pass

        return Handler

    # --- helpers for tests

    def collection_requests(self):
        return [r for r in self.requests if r["path"].endswith("/node/article")]

    def page_requests(self):
        return [r for r in self.collection_requests() if r["query"].get("page[limit]") != "5"]  # not the probe

    # --- responses

    def _send(self, req, status, body, ctype="application/vnd.api+json", headers=()):
        data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body, separators=(",", ":")).encode("utf-8")
        req.send_response(status)
        req.send_header("Content-Type", ctype)
        req.send_header("Content-Length", str(len(data)))
        for name, value in headers:
            req.send_header(name, value)
        req.end_headers()
        req.wfile.write(data)

    def _error(self, req, status, title, detail, via, headers=()):
        # Same key order as core's JsonApiDocumentTopLevelNormalizer: "jsonapi" first, then "errors".
        self._send(req, status, {"jsonapi": JSONAPI_HEADER, "errors": [{
            "title": title, "status": str(status), "detail": detail,
            "links": {"via": {"href": via}, "info": {"href": "http://www.w3.org/Protocols/rfc2616/rfc2616-sec10.html"}}}]},
            headers=headers)

    def _user_for(self, header):
        if not header or not header.startswith("Basic "):
            return None
        try:
            user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except ValueError:
            return None
        return user if (user, password) == (USERNAME, PASSWORD) else None  # wrong login: treated as anonymous

    # --- routing

    def handle(self, req):
        parts = urllib.parse.urlsplit(req.path)
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        user = self._user_for(req.headers.get("Authorization"))
        self.requests.append({"path": parts.path, "query": query, "user": user, "accept": req.headers.get("Accept"),
                              "sent_auth": bool(req.headers.get("Authorization"))})
        url = self.base + req.path
        m = re.fullmatch(r"(?P<prefix>(?:/[^/]+)*?)/jsonapi(?P<rest>/.*)?", parts.path)
        if not m or not self.jsonapi_enabled:
            return self._send(req, 404, self.not_found, "text/html; charset=UTF-8")
        if self.block_anonymous and not user:
            # basic_auth turns an anonymous 403 into a 401 challenge.
            return self._error(req, 401, "Unauthorized", "No authentication credentials provided.", url,
                               headers=[("WWW-Authenticate", 'Basic realm="Example Ledger"')])
        api_base = self.base + m.group("prefix")
        rest = (m.group("rest") or "").rstrip("/")
        if rest == "":
            if self.html_entry:
                return self._send(req, 200, self.front_page, "text/html; charset=UTF-8")
            doc = self._load("entrypoint.json", api_base)
            if user:
                doc["meta"] = {"links": {"me": {"meta": {"id": USER_UUID},
                                                "href": f"{api_base}/jsonapi/user/user/{USER_UUID}"}}}
            return self._send(req, 200, doc)
        if rest == "/node/article":
            if self.collection_status:
                return self._error(req, self.collection_status, "Service Unavailable", "Try again later.", url)
            return self._collection(req, api_base, parts.path, query, user)
        if rest in ("/node/page", "/node/story"):
            return self._send(req, 200, {"jsonapi": JSONAPI_HEADER, "data": [], "links": {"self": {"href": url}}})
        return self._error(req, 404, "Not Found", f"The requested page \"{parts.path}\" could not be found.", url)

    def _collection(self, req, api_base, path, query, user):
        url = self.base + req.path
        nodes = self._load("articles.json", api_base)
        try:
            limit = min(int(query.get("page[limit]", 50)), 50)
            offset = 0 if self.ignore_offset else int(query.get("page[offset]", 0))
        except ValueError:
            return self._error(req, 400, "Bad Request", "The page size needs to be a positive integer.", url)
        if limit <= 0:
            return self._error(req, 400, "Bad Request", "The page size needs to be a positive integer.", url)

        conditions, groups = [], {}
        for key, value in query.items():
            m = re.fullmatch(r"filter\[([^\]]+)\](.*)", key)
            if not m:
                continue
            if m.group(2) == "":
                conditions.append((m.group(1), "=", value))
            else:
                part = re.fullmatch(r"\[condition\]\[(path|operator|value)\]", m.group(2))
                if part:
                    groups.setdefault(m.group(1), {})[part.group(1)] = value
        conditions += [(g["path"], g.get("operator", "="), g.get("value")) for g in groups.values()]
        for field, op, value in conditions:
            if field not in nodes[0]["attributes"]:
                return self._error(req, 400, "Bad Request", f"Invalid nested filtering. The field `{field}`, given "
                                   f"in the path `{field}`, does not exist.", url)
            if field == "status":
                if not self.ignore_status_filter:
                    nodes = [n for n in nodes if n["attributes"]["status"] == (value in ("1", "true"))]
                continue
            ops = {">=": lambda a, b: a >= b, "<": lambda a, b: a < b, ">": lambda a, b: a > b,
                   "<=": lambda a, b: a <= b, "=": lambda a, b: a == b}
            nodes = [n for n in nodes if n["attributes"][field] is not None
                     and ops[op](iso_ts(n["attributes"][field]), int(value))]

        sort = query.get("sort")
        if sort and (self.reject_sort or sort.lstrip("-") != "drupal_internal__nid"):
            return self._error(req, 400, "Bad Request", f"Invalid nested filtering. The field `{sort.lstrip('-')}`, "
                               f"given in the path `{sort.lstrip('-')}`, does not exist.", url)
        nodes.sort(key=lambda n: n["attributes"]["drupal_internal__nid"], reverse=bool(sort and sort.startswith("-")))

        includes = [i for i in query.get("include", "").split(",") if i]
        relationship_names = set(self._load("articles.json", api_base)[0]["relationships"])
        for name in includes:
            if name not in relationship_names:
                return self._error(req, 400, "Bad Request", f"`{name}` is not a valid relationship field name.", url)

        window = nodes[offset:offset + limit + 1]  # N+1, as core does, to know whether a next page exists
        has_next = len(window) > limit
        window = window[:limit]
        data, omitted = [], []
        for n in window:
            a = n["attributes"]
            if not user and (a["drupal_internal__nid"] in self.restricted or not a["status"]):
                omitted.append(f"{api_base}/jsonapi/node/article/{n['id']}")
            else:
                data.append(n)

        included, seen = [], set()
        entities = {(e["type"], e["id"]): e for name in ("users.json", "terms.json", "paragraphs.json")
                    for e in self._load(name, api_base)}
        for name in includes:
            for n in data:
                rel = n["relationships"][name]["data"]
                for ident in (rel if isinstance(rel, list) else [rel] if rel else []):
                    key = (ident["type"], ident["id"])
                    if key in seen or key not in entities:
                        continue
                    seen.add(key)
                    entity = copy.deepcopy(entities[key])
                    if not user and entity["attributes"].get("status") is False and entity["type"] != "user--user":
                        omitted.append(entity["links"]["self"]["href"].split("?")[0])
                        continue
                    if entity["type"] == "user--user" and not user:
                        # "view label" access only: the label-only object carries display_name and nothing else.
                        entity = {"type": entity["type"], "id": entity["id"], "links": entity["links"],
                                  "attributes": {"display_name": entity["attributes"]["display_name"]}}
                    included.append(entity)

        def sparse(obj, fields):
            if not fields:
                return obj
            wanted = set(fields.split(","))
            obj = copy.deepcopy(obj)
            obj["attributes"] = {k: v for k, v in obj.get("attributes", {}).items() if k in wanted}
            if "relationships" in obj:
                obj["relationships"] = {k: v for k, v in obj["relationships"].items() if k in wanted}
            return obj

        doc = {"jsonapi": JSONAPI_HEADER,
               "data": [sparse(n, query.get("fields[node--article]")) for n in data]}
        if included:
            doc["included"] = [sparse(e, query.get(f"fields[{e['type']}]")) for e in included]
        meta = {}
        if self.include_count:
            meta["count"] = len(nodes)
        if omitted:
            meta["omitted"] = {"detail": "Some resources have been omitted because of insufficient authorization.",
                               "links": {"help": {"href": "https://www.drupal.org/docs/8/modules/json-api/filtering#filters-access-control"}}}
            for i, href in enumerate(omitted):
                meta["omitted"]["links"][f"item--{i:06x}aBc"] = {"href": href, "meta": {
                    "rel": "item", "detail": "The current user is not allowed to GET the selected resource."}}
        if meta:
            doc["meta"] = meta
        if not self.no_links:
            doc["links"] = {"self": {"href": url}}
            if has_next:
                next_query = dict(query)
                next_query["page[offset]"] = str(offset + limit)
                next_query["page[limit]"] = str(limit)
                host = self.next_host or self.base
                doc["links"]["next"] = {"href": f"{host}{path}?{urllib.parse.urlencode(next_query)}"}
        return self._send(req, 200, doc)


class RedirectingDrupal(FakeDrupal):
    """
    A FakeDrupal that answers /old/... with a 301 to the same path without /old, on its own address or on
    `elsewhere` (e.g. a www. host, a CDN or an http:// address Drupal's proxy config produced).
    """

    def __init__(self, elsewhere=None, **kw):
        self.elsewhere = elsewhere
        self.redirected = []
        super().__init__(**kw)

    def handle(self, req):
        if req.path.startswith("/old/"):
            self.redirected.append({"path": req.path, "sent_auth": bool(req.headers.get("Authorization"))})
            req.send_response(301)
            req.send_header("Location", (self.elsewhere or self.base) + req.path[len("/old"):])
            req.send_header("Content-Length", "0")
            req.end_headers()
            return
        return super().handle(req)


def clean_env(**env):
    return mock.patch.dict(os.environ, env, clear=True)


def only_drupal():
    return mock.patch.object(import_archive, "IMPORTERS", ["drupal"])


class DrupalTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("importers.common.time.sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def server(self, **kw):
        fake = FakeDrupal(**kw)
        self.addCleanup(fake.close)
        return fake

    def args(self, *argv):
        parser = argparse.ArgumentParser()
        drupal.add_arguments(parser)
        parser.add_argument("--limit", type=int)
        return parser.parse_args(list(argv))

    def run_import(self, *argv, env=None):
        args = self.args(*argv)
        report = ImportReport(source="drupal")
        err = io.StringIO()
        with clean_env(**(env or {})), redirect_stderr(err):
            articles = [a for a in (normalize(raw, report) for raw in drupal.iter_articles(args)) if a]
        return articles, report, err.getvalue()

    def import_error(self, *argv, env=None):
        args = self.args(*argv)
        err = io.StringIO()
        with clean_env(**(env or {})), redirect_stderr(err), self.assertRaises(ImporterError) as ctx:
            list(drupal.iter_articles(args))
        return str(ctx.exception), err.getvalue()

    @staticmethod
    def by_nid(articles):
        return {int(a["id"].rsplit("-", 1)[1]): a for a in articles}


class PaginationAndMappingTests(DrupalTestCase):
    def test_reads_every_page_in_node_id_order(self):
        fake = self.server()
        articles, report, err = self.run_import("--site", fake.base, "--page-size", "2")
        self.assertEqual([int(a["id"].rsplit("-", 1)[1]) for a in articles], VISIBLE_PUBLISHED)
        pages = fake.page_requests()
        self.assertEqual([r["query"]["page[offset]"] for r in pages], ["0", "2", "4", "6", "8"])
        first = pages[0]["query"]
        self.assertEqual(first["filter[status]"], "1")
        self.assertEqual(first["sort"], "drupal_internal__nid")
        self.assertEqual(first["page[limit]"], "2")
        self.assertEqual(first["include"], "uid")
        self.assertEqual(first["fields[user--user]"], "display_name,drupal_internal__uid")
        self.assertEqual(set(first["fields[node--article]"].split(",")),
                         {"drupal_internal__nid", "status", "title", "path", "created", "body", "uid"})
        self.assertTrue(all(r["accept"] == "application/vnd.api+json" for r in fake.requests))
        self.assertIn("node--article: page 5 (8 read)", err)  # 9 listed; one is hidden (meta.omitted)

    def test_headline_text_url_and_html_entities(self):
        fake = self.server()
        articles, _, _ = self.run_import("--site", fake.base + "/")
        a = self.by_nid(articles)
        self.assertEqual(a[101]["headline"], "Council approves $2M budget & new parks plan")
        self.assertEqual(a[101]["content"], "The City Council voted 9-2 on Thursday to approve a $2 million budget."
                         "\n\nThe plan adds three parks in “underserved” neighborhoods.")
        self.assertEqual(a[101]["url"], fake.base + "/news/2024/03/council-approves-budget")
        self.assertEqual(a[102]["headline"], "Café owners rally against new parking rules")
        self.assertEqual(a[102]["url"], fake.base + "/node/102")  # no URL alias
        self.assertEqual(a[108]["url"], fake.base + "/noticias/abre-caf%C3%A9%20fishtown")
        self.assertEqual(a[108]["content"], "El café abrió sus puertas el sábado.")

    def test_ids_are_stable_and_namespaced_by_site(self):
        fake = self.server()
        first, _, _ = self.run_import("--site", fake.base)
        second, _, _ = self.run_import("--site", fake.base, "--page-size", "3")
        self.assertEqual([a["id"] for a in first], [a["id"] for a in second])
        self.assertEqual(first[0]["id"], f"drupal-{fake.key}-101")

    def test_hidden_and_malformed_nodes_are_skipped_with_reasons(self):
        fake = self.server()
        articles, report, _ = self.run_import("--site", fake.base, "--page-size", "2")
        self.assertEqual(len(articles), 5)
        self.assertEqual(dict(report.skipped), {HIDDEN: 1, "no headline": 1, "no article text": 1,
                                                "no parseable publish date": 1})

    def test_malformed_entries_in_data_are_skipped(self):
        ctx = drupal._Context("example-com", "https://example.com", drupal._Plan("node--article", "body", "uid",
                                                                                  "created"),
                              False, None, None, "https://example.com/jsonapi/node/article")
        self.assertEqual(drupal._to_raw("not a node", ctx, {}), {"_skip": "malformed entry in the API response "
                                                                           "(not a node)"})
        self.assertEqual(drupal._to_raw({"type": "node--article", "id": "x"}, ctx, {})["_skip"],
                         "malformed entry in the API response (not a node)")
        raw = drupal._to_raw({"type": "node--article", "id": "abc-123", "attributes": {
            "title": "T", "created": "2024-01-01T00:00:00+00:00", "body": {"value": "<p>x</p>", "format": "plain"}}},
            ctx, {})
        self.assertEqual(raw["id"], "drupal-example-com-abc-123")  # no nid exposed: the UUID is used
        self.assertEqual(raw["url"], "")  # no alias and no nid: normalize() reports "no valid http(s) URL"

    def test_limit_shrinks_the_page(self):
        fake = self.server()
        self.run_import("--site", fake.base, "--limit", "3")
        self.assertEqual(fake.page_requests()[0]["query"]["page[limit]"], "3")

    def test_page_size_must_be_within_drupals_maximum(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.args("--site", "https://example.com", "--page-size", "51")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.args("--site", "https://example.com", "--page-size", "0")

    def test_cache_that_ignores_the_offset_is_reported(self):
        fake = self.server(ignore_offset=True)
        message, _ = self.import_error("--site", fake.base, "--page-size", "2")
        self.assertIn("page[offset]", message)
        self.assertIn("cache", message)

    def test_cache_that_ignores_the_offset_is_reported_when_every_row_is_hidden(self):
        # Pages of nothing but meta.omitted links have no node ids to compare. With no next links to stop on
        # (not core's behaviour, but some proxies strip them), the importer would request offsets forever.
        fake = self.server(ignore_offset=True, no_links=True,
                           restricted=(101, 102, 104, 105, 106, 107, 108, 109, 110))
        handle = fake.handle

        def capped(req):
            if len(fake.requests) >= 40:
                return fake._error(req, 400, "Bad Request", "test request cap reached", fake.base)
            return handle(req)

        fake.handle = capped
        message, _ = self.import_error("--site", fake.base, "--page-size", "2")
        self.assertIn("ignoring the page[offset]", message)
        self.assertLess(len(fake.requests), 10)

    def test_without_links_stops_on_a_short_page(self):
        fake = self.server(no_links=True)
        articles, _, _ = self.run_import("--site", fake.base, "--page-size", "2")
        self.assertEqual(len(articles), 5)
        self.assertEqual([r["query"]["page[offset]"] for r in fake.page_requests()], ["0", "2", "4", "6", "8"])

    def test_next_link_on_another_host_is_not_followed(self):
        internal = self.server()  # stands in for the backend address Drupal put in its links
        fake = self.server(next_host=internal.base)
        articles, _, _ = self.run_import("--site", fake.base, "--page-size", "2")
        self.assertEqual(len(articles), 5)
        self.assertEqual(internal.requests, [])

    def test_fields_are_found_when_the_first_nodes_are_all_hidden(self):
        # The oldest nodes are often members-only or restricted. Field types must still be read from a node the
        # visitor can see, or Paragraphs bodies and reference bylines are silently lost for every article.
        hidden_first = (101, 102, 104, 105, 106)
        fake = self.server(restricted=hidden_first)
        articles, report, _ = self.run_import("--site", fake.base, "--body-field", "field_paragraphs")
        a = self.by_nid(articles)
        self.assertEqual(sorted(a), [110])
        self.assertEqual(a[110]["content"], "Phase one starts in September.")
        self.assertEqual(report.skipped[HIDDEN], 5)
        planned = [r["query"] for r in fake.collection_requests() if "include" in r["query"]]
        self.assertIn("field_paragraphs", planned[0]["include"])
        self.assertEqual([q["page[offset]"] for q in planned], ["5"])  # the hidden rows are not read again

        fake = self.server(restricted=hidden_first)
        a = self.by_nid(self.run_import("--site", fake.base, "--byline-field", "field_authors")[0])
        self.assertEqual(a[108]["authors"], ["Jae Kim"])
        self.assertEqual(a[110]["authors"], ["Jae Kim"])

    def test_hidden_rows_are_read_a_full_page_at_a_time_even_with_a_small_limit(self):
        fake = self.server(restricted=(101, 102, 104, 105, 106, 107))
        articles, report, err = self.run_import("--site", fake.base, "--limit", "1")
        self.assertEqual([a["id"] for a in articles][:1], [f"drupal-{fake.key}-108"])
        probe = [r["query"] for r in fake.collection_requests() if "include" not in r["query"]]
        self.assertEqual([(q["page[offset]"], q["page[limit]"]) for q in probe], [("0", "5"), ("5", "50")])
        self.assertIn("the first 5 node(s) are hidden from this visitor", err)

    def test_collection_with_nothing_visible_is_read_once(self):
        fake = self.server(restricted=(101, 102, 104, 105, 106, 107, 108, 109, 110))
        articles, report, _ = self.run_import("--site", fake.base, "--body-field", "field_paragraphs")
        self.assertEqual(articles, [])
        self.assertEqual(dict(report.skipped), {HIDDEN: 9})
        self.assertEqual([r["query"]["page[offset]"] for r in fake.collection_requests()], ["0", "5"])

    def test_total_is_shown_when_the_site_reports_a_count(self):
        fake = self.server(include_count=True)
        _, _, err = self.run_import("--site", fake.base, "--page-size", "5")
        self.assertIn("(4 of 9 read)", err)


class StatusTests(DrupalTestCase):
    def test_unpublished_nodes_are_never_requested(self):
        fake = self.server()
        articles, report, _ = self.run_import("--site", fake.base)
        self.assertNotIn(103, self.by_nid(articles))
        self.assertTrue(all(r["query"].get("filter[status]") == "1" for r in fake.collection_requests()))
        self.assertNotIn("unpublished (draft, archived or not yet published)", report.skipped)

    def test_unpublished_nodes_returned_anyway_are_skipped_with_a_reason(self):
        fake = self.server(ignore_status_filter=True)
        articles, report, _ = self.run_import("--site", fake.base, env={"DRUPAL_USER": USERNAME,
                                                                         "DRUPAL_PASSWORD": PASSWORD})
        self.assertEqual(report.skipped["unpublished (draft, archived or not yet published)"], 1)
        self.assertNotIn(103, self.by_nid(articles))

    def test_unpublished_paragraphs_and_terms_are_left_out_when_signed_in(self):
        # An editor's account can see unpublished paragraphs (draft text) and unpublished author terms. Readers
        # can't, so neither belongs in the archive.
        login = {"DRUPAL_USER": USERNAME, "DRUPAL_PASSWORD": PASSWORD}
        fake = self.server()
        articles, _, err = self.run_import("--site", fake.base, "--body-field", "field_paragraphs", env=login)
        a = self.by_nid(articles)
        self.assertEqual(a[110]["content"], "Phase one starts in September.")
        self.assertNotIn("Unpublished paragraph text", json.dumps(articles))
        self.assertIn("1 unpublished paragraph(s)", err)

        fake = self.server()
        a = self.by_nid(self.run_import("--site", fake.base, "--byline-field", "field_authors", env=login)[0])
        self.assertEqual(a[110]["authors"], ["Jae Kim"])  # not the unpublished "Priya Shah" term
        self.assertEqual(a[101]["authors"], ["Maria Lopez", "Sam O'Neil"])

    def test_restricted_nodes_are_counted_not_imported(self):
        fake = self.server()
        articles, report, _ = self.run_import("--site", fake.base)
        self.assertNotIn(104, self.by_nid(articles))
        self.assertEqual(report.skipped[HIDDEN], 1)


class TimezoneTests(DrupalTestCase):
    def test_offsets_are_converted_to_utc(self):
        fake = self.server()
        a = self.by_nid(self.run_import("--site", fake.base)[0])
        self.assertEqual(a[101]["publish_date"], "2024-03-10T06:30:00Z")
        self.assertEqual(a[102]["publish_date"], "2024-03-11T14:05:00Z")
        self.assertEqual(a[108]["publish_date"], "2023-11-05T05:30:00Z")  # the night US daylight time ended
        self.assertEqual(a[109]["publish_date"], "2024-04-02T06:30:00Z")  # next day in UTC

    def test_custom_date_field(self):
        fake = self.server()
        articles, report, _ = self.run_import("--site", fake.base, "--date-field", "field_display_date")
        a = self.by_nid(articles)
        self.assertEqual(sorted(a), [101, 110])
        self.assertEqual(a[101]["publish_date"], "2024-03-10T02:15:00Z")
        self.assertEqual(a[110]["publish_date"], "2024-01-02T14:00:00Z")
        self.assertEqual(report.skipped["no parseable publish date"], 4)  # never invented from `created`

    def test_date_window_is_sent_as_utc_timestamps(self):
        fake = self.server()
        articles, _, _ = self.run_import("--site", fake.base, "--after", "2024-03-11", "--before", "2024-04-03")
        query = fake.page_requests()[0]["query"]
        self.assertEqual(query["filter[dewey_after][condition][path]"], "created")
        self.assertEqual(query["filter[dewey_after][condition][operator]"], ">=")
        self.assertEqual(query["filter[dewey_after][condition][value]"], "1710115200")
        self.assertEqual(query["filter[dewey_before][condition][operator]"], "<")
        self.assertEqual(query["filter[dewey_before][condition][value]"], "1712102400")
        self.assertEqual(sorted(self.by_nid(articles)), [102, 109])

    def test_date_window_on_a_custom_field_is_checked_while_reading(self):
        fake = self.server()
        articles, report, err = self.run_import("--site", fake.base, "--date-field", "field_display_date",
                                                "--after", "2024-03-01")
        self.assertNotIn("filter[dewey_after][condition][value]", fake.page_requests()[0]["query"])
        self.assertEqual(sorted(self.by_nid(articles)), [101])
        self.assertEqual(report.skipped["published outside the --after/--before dates"], 1)
        self.assertIn("every node of the type is downloaded", err)

    def test_after_must_precede_before(self):
        message, _ = self.import_error("--site", "https://example.com", "--after", "2024-02-01",
                                       "--before", "2024-01-01")
        self.assertIn("--after must be an earlier date", message)


class BylineTests(DrupalTestCase):
    def test_author_display_name_and_no_placeholder_for_anonymous_or_deleted_users(self):
        fake = self.server()
        articles, report, err = self.run_import("--site", fake.base)
        a = self.by_nid(articles)
        self.assertEqual(a[101]["authors"], ["Maria Lopez"])  # display_name, not the login name mlopez
        self.assertEqual(a[108]["authors"], ["Jae Kim"])
        self.assertEqual(a[102]["authors"], [])  # Drupal's anonymous user, not "Anonymous"
        self.assertEqual(a[109]["authors"], [])  # the author account was deleted
        self.assertIn("1 article(s) are authored by Drupal's anonymous user", err)
        self.assertIn("--byline-field", err)

    def test_anonymous_user_recognised_without_relationship_meta(self):
        ctx = drupal._Context("k", "https://example.com", drupal._Plan("node--article", "body", "uid", "created"),
                              True, None, None, "https://example.com/jsonapi/node/article")
        node = {"type": "node--article", "id": "n", "attributes": {"title": "T"},
                "relationships": {"uid": {"data": {"type": "user--user", "id": "u0"}}}}
        anonymous = {("user--user", "u0"): {"type": "user--user", "id": "u0",
                                            "attributes": {"display_name": "Anonymous", "drupal_internal__uid": 0}}}
        self.assertEqual(drupal._authors(node, ctx, anonymous), [])
        self.assertEqual(ctx.anonymous_author, 1)

    def test_anonymous_label_only_user_without_relationship_meta(self):
        # Older Drupal releases don't put drupal_internal__target_id on relationships, and a visitor only gets the
        # anonymous user's label, so "Anonymous" must not become a byline.
        ctx = drupal._Context("k", "https://example.com", drupal._Plan("node--article", "body", "uid", "created"),
                              False, None, None, "https://example.com/jsonapi/node/article")
        node = {"type": "node--article", "id": "n", "attributes": {"title": "T"},
                "relationships": {"uid": {"data": {"type": "user--user", "id": "u0"}}}}
        label_only = {("user--user", "u0"): {"type": "user--user", "id": "u0",
                                             "attributes": {"display_name": "Anonymous"}}}
        self.assertEqual(drupal._authors(node, ctx, label_only), [])
        self.assertEqual(ctx.anonymous_author, 1)
        # With relationship meta naming a real account, the name is that account's, even if it reads "Anonymous".
        node["relationships"]["uid"]["data"]["meta"] = {"drupal_internal__target_id": 7}
        self.assertEqual(drupal._authors(node, ctx, label_only), ["Anonymous"])

    def test_reference_byline_field_with_several_authors(self):
        fake = self.server()
        articles, _, err = self.run_import("--site", fake.base, "--byline-field", "field_authors")
        a = self.by_nid(articles)
        self.assertEqual(a[101]["authors"], ["Maria Lopez", "Sam O'Neil"])
        self.assertEqual(a[108]["authors"], ["Jae Kim"])
        self.assertEqual(a[102]["authors"], [])
        self.assertEqual(a[110]["authors"], ["Jae Kim"])  # the unpublished "Priya Shah" term is hidden
        self.assertIn("field_authors", fake.page_requests()[0]["query"]["include"])
        self.assertIn("1 byline(s) couldn't be read", err)

    def test_text_byline_field(self):
        fake = self.server()
        a = self.by_nid(self.run_import("--site", fake.base, "--byline-field", "field_byline")[0])
        self.assertEqual(a[101]["authors"], ["Maria Lopez"])  # "By " removed
        self.assertEqual(a[108]["authors"], ["Jae Kim"])
        self.assertEqual(a[110]["authors"], [])
        self.assertNotIn("include", fake.page_requests()[0]["query"])

    def test_no_bylines(self):
        fake = self.server()
        articles, _, _ = self.run_import("--site", fake.base, "--byline-field", "none")
        self.assertTrue(all(a["authors"] == [] for a in articles))
        self.assertNotIn("include", fake.page_requests()[0]["query"])

    def test_unknown_byline_field_lists_the_fields(self):
        fake = self.server()
        message, _ = self.import_error("--site", fake.base, "--byline-field", "field_writer")
        self.assertIn("no field 'field_writer'", message)
        self.assertIn("field_authors", message)
        self.assertIn("Manage fields", message)

    def test_names_from_text_values(self):
        self.assertEqual(drupal._names_from_value("by  Jane&nbsp;Doe"), ["Jane Doe"])
        self.assertEqual(drupal._names_from_value(["Ana Ruiz", "Ana Ruiz", " ", None, "Li Wei"]),
                         ["Ana Ruiz", "Li Wei"])
        self.assertEqual(drupal._names_from_value({"value": "<p>By <strong>Ana Ruiz</strong></p>",
                                                   "format": "basic_html", "processed": None}), ["Ana Ruiz"])
        self.assertEqual(drupal._names_from_value(42), [])


class BodyTests(DrupalTestCase):
    def test_paragraphs_body_field(self):
        fake = self.server()
        articles, report, err = self.run_import("--site", fake.base, "--body-field", "field_paragraphs")
        a = self.by_nid(articles)
        self.assertEqual(sorted(a), [101, 110])
        self.assertEqual(a[101]["content"], "Paragraph one: the council’s vote followed a three-hour hearing."
                                            "\n\nWe heard every neighborhood.")
        self.assertEqual(a[110]["content"], "Phase one starts in September.")  # unpublished paragraph hidden
        self.assertEqual(set(fake.page_requests()[0]["query"]["include"].split(",")), {"uid", "field_paragraphs"})
        self.assertIn("1 paragraph(s) of article text were hidden", err)

    def test_unknown_body_field(self):
        fake = self.server()
        message, _ = self.import_error("--site", fake.base, "--body-field", "field_story")
        self.assertIn("no field 'field_story' (--body-field)", message)

    def test_date_field_must_not_be_a_reference(self):
        fake = self.server()
        message, _ = self.import_error("--site", fake.base, "--date-field", "uid")
        self.assertIn("reference field, not a date", message)

    def test_body_hidden_by_field_access_is_reported_as_such(self):
        # A field the visitor may not view is left out of the resource (a field-level paywall), unlike an empty
        # field, which is null. Those articles aren't empty: say why they were skipped.
        ctx = drupal._Context("k", "https://example.com", drupal._Plan("node--article", "body", None, "created"),
                              False, None, None, "https://example.com/jsonapi/node/article")
        node = {"type": "node--article", "id": "u", "attributes": {
            "drupal_internal__nid": 7, "status": True, "title": "T", "created": "2024-01-01T00:00:00+00:00",
            "path": {"alias": "/t"}}}
        self.assertEqual(drupal._to_raw(node, ctx, {}),
                         {"_skip": "article text hidden from this visitor (field permissions, e.g. a paywall)"})
        node["attributes"]["body"] = None
        self.assertEqual(drupal._to_raw(node, ctx, {})["content_html"], "")  # empty: normalize() says "no article text"

    def test_body_field_found_when_the_first_visible_node_hides_it(self):
        sample_hidden = {"type": "node--article", "id": "a", "attributes": {"title": "A", "created": "x"},
                         "relationships": {"uid": {"data": None}}}
        sample_open = {"type": "node--article", "id": "b", "attributes": {"title": "B", "created": "x", "body": None},
                       "relationships": {"uid": {"data": None}}}
        plan = drupal._Plan("node--article", "body", "uid", "created")
        plan.classify([sample_hidden, sample_open])
        self.assertFalse(plan.body_is_rel)
        with self.assertRaises(ImporterError) as ctx:
            drupal._Plan("node--article", "body", "uid", "created").classify([sample_hidden])
        self.assertIn("hidden from this visitor", str(ctx.exception))

    def test_plain_long_text_body(self):
        ctx = drupal._Context("k", "https://example.com", drupal._Plan("node--article", "field_text", None,
                                                                        "created"),
                              False, None, None, "https://example.com/jsonapi/node/article")
        raw = drupal._to_raw({"type": "node--article", "id": "u", "attributes": {
            "drupal_internal__nid": 7, "title": "T", "created": "2024-01-01T00:00:00+00:00",
            "field_text": "Line one.\nLine two with a < sign."}}, ctx, {})
        self.assertEqual(raw["content"], "Line one.\nLine two with a < sign.")
        self.assertNotIn("content_html", raw)


class DiscoveryTests(DrupalTestCase):
    def test_site_given_as_the_jsonapi_address(self):
        fake = self.server()
        articles, _, _ = self.run_import("--site", fake.base + "/jsonapi/")
        self.assertEqual(articles[0]["url"], fake.base + "/news/2024/03/council-approves-budget")

    def test_language_prefix_is_kept_in_api_and_article_urls(self):
        fake = self.server()
        articles, _, _ = self.run_import("--site", fake.base + "/es")
        self.assertTrue(all(r["path"].startswith("/es/jsonapi") for r in fake.requests))
        self.assertEqual(articles[0]["url"], fake.base + "/es/news/2024/03/council-approves-budget")
        self.assertEqual(articles[0]["id"], f"drupal-{fake.key}-es-101")

    def test_headless_site_with_separate_api_address(self):
        fake = self.server()
        articles, _, _ = self.run_import("--site", "https://www.example-ledger.org",
                                         "--api-url", fake.base + "/jsonapi")
        self.assertEqual(articles[0]["url"], "https://www.example-ledger.org/news/2024/03/council-approves-budget")
        self.assertEqual(articles[0]["id"], "drupal-example-ledger-org-101")

    def test_jsonapi_module_off(self):
        fake = self.server(jsonapi_enabled=False)
        message, _ = self.import_error("--site", fake.base)
        self.assertIn("No JSON:API at", message)
        self.assertIn("Manage > Extend", message)

    def test_entry_point_is_a_web_page(self):
        fake = self.server(html_entry=True)
        message, _ = self.import_error("--site", fake.base)
        self.assertIn("returned a web page, not JSON:API data", message)

    def test_unreachable_site(self):
        message, _ = self.import_error("--site", "http://127.0.0.1:9")
        self.assertIn("Couldn't reach http://127.0.0.1:9/jsonapi", message)

    def test_unknown_type_lists_the_content_types(self):
        fake = self.server()
        message, _ = self.import_error("--site", fake.base, "--type", "blog_post")
        self.assertIn("no content type 'blog_post'", message)
        self.assertIn("article, page, story", message)

    def test_type_by_resource_name_and_empty_type(self):
        fake = self.server()
        articles, _, err = self.run_import("--site", fake.base, "--type", "node--article", "--type", "page")
        self.assertEqual(len(articles), 5)
        self.assertIn("node--page: no published nodes", err)

    def test_bad_site_value(self):
        message, _ = self.import_error("--site", "ftp://example.com")
        self.assertIn("--site should be a web address", message)

    def test_rejected_request_is_explained(self):
        fake = self.server(reject_sort=True)
        message, _ = self.import_error("--site", fake.base)
        self.assertIn("Drupal rejected the request for node--article (HTTP 400: Invalid nested filtering.", message)
        self.assertIn("Manage fields", message)

    def test_server_errors_suggest_a_smaller_page(self):
        fake = self.server(collection_status=503)
        message, _ = self.import_error("--site", fake.base)
        self.assertIn("smaller --page-size", message)

    def test_error_detail_survives_truncation(self):
        body = json.dumps({"jsonapi": JSONAPI_HEADER, "errors": [{"title": "Bad Request", "status": "400",
                          "detail": "Invalid nested filtering. The field `status`, given in the path `status`, "
                                    "does not exist."}]}, separators=(",", ":"))
        message = f"HTTP 400 from https://example.com/jsonapi/node/article. Server said: {body[:200]}"
        detail = drupal._error_detail(message)
        self.assertTrue(detail.startswith("Invalid nested filtering."))
        self.assertTrue(detail.endswith("..."))
        self.assertEqual(drupal._error_detail('HTTP 400 ... Server said: {"detail":"Short \\"one\\""}'),
                         'Short "one"')

    def test_site_keys_and_default_prefix(self):
        self.assertEqual(drupal._site_key("https://www.example.com/es/"), "example-com-es")
        self.assertEqual(drupal._site_key("https://example.com/jsonapi"), "example-com")
        self.assertEqual(drupal.default_prefix(self.args("--site", "https://www.example.com")),
                         "drupal-example-com")
        self.assertEqual(drupal.default_prefix(self.args("--site", "www.example.com/es", "--type", "story",
                                                         "--type", "node--article", "--after", "2023-01-01",
                                                         "--before", "2024-01-01")),
                         "drupal-example-com-es-story-article-after-20230101-before-20240101")


class AuthTests(DrupalTestCase):
    LOGIN = {"DRUPAL_USER": USERNAME, "DRUPAL_PASSWORD": PASSWORD}

    def test_site_closed_to_anonymous_visitors(self):
        fake = self.server(block_anonymous=True)
        message, _ = self.import_error("--site", fake.base)
        self.assertIn("refused", message)
        self.assertIn("--user and --password", message)
        self.assertFalse(any(r["sent_auth"] for r in fake.requests))

    def test_login_from_environment(self):
        fake = self.server(block_anonymous=True)
        articles, report, err = self.run_import("--site", fake.base, env=self.LOGIN)
        self.assertIn(104, self.by_nid(articles))  # an account can see restricted content: the help says so
        self.assertNotIn(HIDDEN, report.skipped)
        self.assertTrue(all(r["user"] == USERNAME for r in fake.requests))
        self.assertIn("Signed in to Drupal", err)
        self.assertNotIn(PASSWORD, err)

    def test_login_flags(self):
        fake = self.server(block_anonymous=True)
        articles, _, _ = self.run_import("--site", fake.base, "--user", USERNAME, "--password", PASSWORD)
        self.assertEqual(len(articles), 6)

    def test_wrong_password_is_explained_without_echoing_it(self):
        for blocked in (False, True):
            with self.subTest(block_anonymous=blocked):
                fake = self.server(block_anonymous=blocked)
                message, err = self.import_error("--site", fake.base, env={"DRUPAL_USER": USERNAME,
                                                                          "DRUPAL_PASSWORD": "wrong-Pa55"})
                self.assertIn("didn't accept the login", message)
                self.assertNotIn("wrong-Pa55", message + err)

    def test_half_a_login_is_an_error(self):
        message, _ = self.import_error("--site", "https://example.com", env={"DRUPAL_USER": USERNAME})
        self.assertIn("password (DRUPAL_PASSWORD or --password) is missing", message)

    def test_refuses_to_send_a_password_over_plain_http(self):
        message, _ = self.import_error("--site", "http://cms.example.org", env=self.LOGIN)
        self.assertIn("plain http", message)
        self.assertNotIn(PASSWORD, message)

    def test_credentials_go_only_to_the_api_address(self):
        internal = self.server()
        fake = self.server(next_host=internal.base)
        articles, _, _ = self.run_import("--site", "https://www.example-ledger.org", "--api-url",
                                         fake.base + "/jsonapi", "--page-size", "2", env=self.LOGIN)
        self.assertEqual(len(articles), 6)
        self.assertEqual(internal.requests, [])
        self.assertTrue(all(r["user"] == USERNAME for r in fake.requests))

    def test_password_is_not_carried_across_a_redirect_to_another_address(self):
        # urllib copies the Authorization header onto every redirect, to any host and to http://.
        elsewhere = self.server(block_anonymous=True)
        fake = RedirectingDrupal(elsewhere=elsewhere.base.replace("127.0.0.1", "localhost"))
        self.addCleanup(fake.close)
        message, err = self.import_error("--site", fake.base + "/old", env=self.LOGIN)
        self.assertEqual(elsewhere.requests, [])
        self.assertIn("redirected", message)
        self.assertIn(elsewhere.base.replace("127.0.0.1", "localhost"), message)
        self.assertIn("--site", message)
        self.assertNotIn(PASSWORD, message + err)

    def test_same_address_redirects_keep_the_login(self):
        fake = RedirectingDrupal(block_anonymous=True)
        self.addCleanup(fake.close)
        articles, _, _ = self.run_import("--site", fake.base + "/old", env=self.LOGIN)
        self.assertEqual(len(articles), 6)
        self.assertTrue(fake.redirected and all(r["sent_auth"] for r in fake.redirected))

    def test_anonymous_imports_follow_redirects_to_another_address(self):
        elsewhere = self.server()
        fake = RedirectingDrupal(elsewhere=elsewhere.base)
        self.addCleanup(fake.close)
        articles, _, _ = self.run_import("--site", fake.base + "/old")
        self.assertEqual(len(articles), 5)

    def test_address_with_a_password_in_it_is_refused_without_echoing_it(self):
        for option in ("--site", "--api-url"):
            with self.subTest(option=option):
                argv = ["--site", "https://www.example.com", option, "http://dewey:hunter2@127.0.0.1:9"]
                message, err = self.import_error(*argv[(2 if option == "--site" else 0):])
                self.assertIn("--user", message)
                self.assertIn(drupal.PASSWORD_ENV, message)
                self.assertNotIn("hunter2", message + err)

    def test_client_never_sends_credentials_to_another_origin(self):
        fake, other = self.server(), self.server()
        token = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        client = drupal._Client(token, drupal._origin(fake.base))
        client.get(fake.base + "/jsonapi")
        client.get(other.base + "/jsonapi")
        self.assertTrue(fake.requests[0]["sent_auth"])
        self.assertFalse(other.requests[0]["sent_auth"])


class HelpTests(unittest.TestCase):
    def test_help_says_where_to_find_each_value(self):
        parser = argparse.ArgumentParser(prog="import_archive.py drupal")
        drupal.add_arguments(parser)
        text = parser.format_help()
        for phrase in ("<site>/jsonapi", "Manage > Extend", "Structure > Content types", "Manage fields",
                       "People > Add user", "HTTP Basic Authentication", "DRUPAL_USER", "DRUPAL_PASSWORD",
                       "shell history", "--limit 20"):
            self.assertIn(phrase, text)
        self.assertNotIn("\n", drupal.HELP)


class EndToEndTests(DrupalTestCase):
    def test_cli_writes_articles_and_returns_zero(self):
        fake = self.server()
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, clean_env(), only_drupal():
            with redirect_stdout(out), redirect_stderr(err):
                code = import_archive.main(["drupal", "--site", fake.base, "--page-size", "2", "--out-dir", tmp])
            self.assertEqual(code, 0, err.getvalue())
            files = sorted(pathlib.Path(tmp).glob("*.json"))
            self.assertEqual([f.name for f in files], [f"drupal-{fake.key}-0001.json"])
            written = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(written[0], {
            "id": f"drupal-{fake.key}-101",
            "headline": "Council approves $2M budget & new parks plan",
            "content": "The City Council voted 9-2 on Thursday to approve a $2 million budget.\n\n"
                       "The plan adds three parks in “underserved” neighborhoods.",
            "url": fake.base + "/news/2024/03/council-approves-budget",
            "authors": ["Maria Lopez"],
            "publish_date": "2024-03-10T06:30:00Z",
        })
        self.assertEqual([w["id"].rsplit("-", 1)[1] for w in written], [str(n) for n in VISIBLE_PUBLISHED])
        summary = out.getvalue()
        self.assertIn("Imported 5 article(s) from drupal.", summary)
        self.assertIn("2 have no byline", summary)
        self.assertIn(f"1 skipped: {HIDDEN}", summary)
        self.assertIn("Publish dates: 2023-11-05 to 2024-04-15", summary)

    def test_cli_limit_stops_early(self):
        fake = self.server()
        with tempfile.TemporaryDirectory() as tmp, clean_env(), only_drupal():
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = import_archive.main(["drupal", "--site", fake.base, "--limit", "2", "--out-dir", tmp])
            self.assertEqual(code, 0)
            written = json.loads(next(pathlib.Path(tmp).glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(len(written), 2)
        self.assertEqual(len(fake.page_requests()), 1)

    def test_cli_failure_returns_one_and_writes_nothing(self):
        fake = self.server(block_anonymous=True)
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, clean_env(), only_drupal():
            (pathlib.Path(tmp) / f"drupal-{fake.key}-0001.json").write_text("[]", encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = import_archive.main(["drupal", "--site", fake.base, "--out-dir", tmp])
            self.assertEqual(code, 1)
            self.assertEqual([f.name for f in pathlib.Path(tmp).iterdir()], [f"drupal-{fake.key}-0001.json"])
        self.assertIn("Import failed:", err.getvalue())
        self.assertIn("Nothing was changed", err.getvalue())


if __name__ == "__main__":
    unittest.main()

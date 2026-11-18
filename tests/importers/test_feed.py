import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import argparse
import contextlib
import gzip
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
from typing import Callable, ClassVar, Dict, List, Optional, Tuple
from unittest import mock

import import_archive
from importers import feed
from importers.common import ImporterError, ImportReport, normalize

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "feed"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run_feed(argv):
    """Parse argv with the importer's own options and return (raw items, stderr text)."""
    parser = argparse.ArgumentParser(prog="feed")
    feed.add_arguments(parser)
    args = parser.parse_args(argv)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        raws = list(feed.iter_articles(args))
    return raws, err.getvalue()


def normalized(raws):
    report = ImportReport("feed")
    articles = [a for a in (normalize(r, report) for r in raws) if a]
    return articles, report


def by_headline(articles):
    return {a["headline"]: a for a in articles}


# ---------------------------------------------------------------- local HTTP server

class _Handler(BaseHTTPRequestHandler):
    routes: ClassVar[Dict[str, Tuple[int, str, bytes]]] = {}
    requests: ClassVar[List[str]] = []
    dynamic: ClassVar[Optional[Callable[[str], Optional[Tuple[int, str, bytes]]]]] = None

    def do_GET(self):
        type(self).requests.append(self.path)
        route = self.routes.get(self.path)
        if route is None and type(self).dynamic is not None:
            route = type(self).dynamic(self.path)
        if route is None and self.path.startswith("/repeat/feed/"):
            route = self.routes["/repeat/feed/"]  # a site that ignores ?paged=
        status, ctype, body = route or (404, "text/html; charset=UTF-8", b"<html><body>Not found</body></html>")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


RSS = "application/rss+xml; charset=UTF-8"
ATOM = "application/atom+xml"


def setUpModule():
    global _saved_no_proxy
    # Never send 127.0.0.1 requests through a proxy configured in the environment.
    _saved_no_proxy = {k: os.environ.get(k) for k in ("no_proxy", "NO_PROXY")}
    for k in _saved_no_proxy:
        os.environ[k] = "127.0.0.1,localhost" + ("," + _saved_no_proxy[k] if _saved_no_proxy[k] else "")


def tearDownModule():
    for k, v in _saved_no_proxy.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _Handler.routes = {
            "/": (200, "text/html; charset=UTF-8", fixture("homepage.html")),
            "/nofeed.html": (200, "text/html", b"<!DOCTYPE html><html><head><title>Hi</title></head></html>"),
            "/feed/": (200, RSS, fixture("wp_page1.xml")),
            "/feed/?paged=2": (200, RSS, fixture("wp_page2.xml")),
            "/comments/feed/": (200, RSS, fixture("comments_feed.xml")),
            "/disguised/": (200, RSS, fixture("comments_feed.xml")),
            "/repeat/feed/": (200, RSS, fixture("wp_page1.xml")),
            "/empty/feed/": (200, RSS, fixture("wp_empty.xml")),
            "/atom.xml": (200, ATOM, fixture("atom_page1.xml")),
            "/atom.xml?page=2": (200, ATOM, fixture("atom_page2.xml")),
            "/archive/2023.xml": (200, ATOM, fixture("atom_archive_2023.xml")),
            "/gazette/rss.xml": (200, "text/xml", fixture("newsroom_rss.xml")),
            "/feed.json": (200, "application/feed+json", json.dumps({
                "version": "https://jsonfeed.org/version/1.1", "title": "JSON", "items": []}).encode()),
        }
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.requests = []

    def url(self, path):
        return self.base + path


# ---------------------------------------------------------------- feeds over HTTP

class WordPressFeedTests(ServerTestCase):
    def test_pages_with_paged_query_until_404(self):
        raws, err = run_feed(["--url", self.url("/feed/"), "--delay", "0"])
        self.assertEqual(_Handler.requests, ["/feed/", "/feed/?paged=2", "/feed/?paged=3"])
        self.assertIn("WordPress feed without paging links", err)
        self.assertIn("page 3: not found", err)

        articles, report = normalized(raws)
        heads = by_headline(articles)
        council = heads["Council’s budget vote: “We’re not done”"]
        self.assertEqual(council["authors"], ["Jane Doe"])
        self.assertEqual(council["publish_date"], "2024-03-05T14:30:00Z")
        self.assertEqual(council["url"], "https://dailyexample.test/2024/03/05/council-budget-vote/")
        # Full text from content:encoded; script and Yoast "appeared first on" footer removed.
        self.assertIn("“We’re not done,” said Council President Maria Lopez.", council["content"])
        self.assertNotIn("trackView", council["content"])
        self.assertNotIn("appeared first on", council["content"])
        self.assertNotIn("Continue reading", council["content"])

        # Double-encoded entities and markup in the title; two dc:creator, "By " dropped; EST offset.
        qa = heads["Q&A: The new schools chief on reading scores"]
        self.assertEqual(qa["authors"], ["Jane Doe", "John Roe"])
        self.assertEqual(qa["publish_date"], "2024-03-04T14:00:00Z")
        self.assertIn("- Reading scores rose 4 points.", qa["content"])

        # Empty dc:creator -> no byline (never a placeholder); -0500 offset.
        roads = heads["Weekend road closures"]
        self.assertEqual(roads["authors"], [])
        self.assertEqual(roads["publish_date"], "2024-03-03T23:00:00Z")
        # CET is not known to email.utils; it must not silently become UTC.
        self.assertEqual(heads["Festival returns to the waterfront"]["publish_date"], "2024-03-01T09:15:00Z")

        self.assertNotIn("Protected: Budget memo", heads)
        self.assertEqual(report.skipped["password-protected post (the feed carries only a password prompt)"], 1)
        self.assertEqual(report.skipped["unparseable date in <pubDate>"], 1)
        self.assertEqual(report.skipped["no headline"], 1)
        self.assertIn("had no full-text field", err)
        self.assertIn("1 end like an excerpt", err)

    def test_ids_are_stable_and_namespaced(self):
        first, _ = run_feed(["--url", self.url("/feed/"), "--delay", "0", "--paging", "none"])
        second, _ = run_feed(["--url", self.url("/feed/"), "--delay", "0", "--paging", "none"])
        ids = [r["id"] for r in first if "id" in r]
        self.assertEqual(ids, [r["id"] for r in second if "id" in r])
        self.assertTrue(all(i.startswith("feed-") for i in ids))
        self.assertEqual(len(ids), len(set(ids)))
        # The id is the guid's, not the feed URL's: the same post in another copy of the feed keeps it.
        self.assertEqual(ids[0], feed._make_id("https://dailyexample.test/?p=101", "", ""))

    def test_paging_none_reads_one_page_and_says_so(self):
        raws, err = run_feed(["--url", self.url("/feed/"), "--delay", "0", "--paging", "none"])
        self.assertEqual(_Handler.requests, ["/feed/"])
        self.assertEqual(len(raws), 4)
        self.assertIn("may not be the whole archive", err)

    def test_query_paging_stops_when_site_ignores_parameter(self):
        raws, err = run_feed(["--url", self.url("/repeat/feed/"), "--delay", "0"])
        self.assertEqual(_Handler.requests, ["/repeat/feed/", "/repeat/feed/?paged=2"])
        self.assertIn("only items already seen", err)
        self.assertEqual(len(raws), 8)  # page 2 repeated page 1; the writer drops the duplicates

    def test_empty_wordpress_feed_is_not_paged(self):
        raws, err = run_feed(["--url", self.url("/empty/feed/"), "--delay", "0"])
        self.assertEqual(raws, [])
        self.assertEqual(_Handler.requests, ["/empty/feed/"])
        self.assertIn("it has no items", err)

    def test_custom_page_param_and_max_pages(self):
        _, err = run_feed(["--url", self.url("/repeat/feed/"), "--delay", "0", "--paging", "query",
                           "--page-param", "page", "--max-pages", "1"])
        self.assertEqual(_Handler.requests, ["/repeat/feed/"])
        self.assertIn("--max-pages", err)

    def test_require_full_text_skips_excerpt_items(self):
        raws, _ = run_feed(["--url", self.url("/feed/"), "--delay", "0", "--paging", "none",
                            "--require-full-text"])
        articles, report = normalized(raws)
        self.assertNotIn("Weekend road closures", by_headline(articles))
        self.assertEqual(report.skipped[
            "only a summary/description in the feed, no full text (--require-full-text)"], 1)

    def test_homepage_autodiscovery_skips_comments_feed(self):
        raws, err = run_feed(["--url", self.url("/"), "--delay", "0", "--paging", "none"])
        self.assertEqual(_Handler.requests, ["/", "/feed/"])
        self.assertIn("using the feed it advertises", err)
        self.assertEqual(len(raws), 4)

    def test_web_page_without_feed_is_explained(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(ImporterError) as ctx:
            run_feed(["--url", self.url("/nofeed.html"), "--delay", "0"])
        self.assertIn("doesn't advertise one", str(ctx.exception))

    def test_comments_feed_refused_by_url_without_fetching(self):
        with self.assertRaises(ImporterError) as ctx:
            run_feed(["--url", self.url("/comments/feed/"), "--delay", "0"])
        self.assertIn("comments feed", str(ctx.exception))
        self.assertEqual(_Handler.requests, [])

    def test_comments_feed_refused_by_title(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(ImporterError) as ctx:
            run_feed(["--url", self.url("/disguised/"), "--delay", "0"])
        self.assertIn("comments feed", str(ctx.exception))

    def test_json_feed_refused(self):
        with self.assertRaises(ImporterError) as ctx:
            run_feed(["--url", self.url("/feed.json"), "--delay", "0"])
        self.assertIn("JSON", str(ctx.exception))

    def test_url_with_login_refused_and_password_not_echoed(self):
        url = self.url("/feed/").replace("http://", "http://editor:s3cret-pw@")
        with self.assertRaises(ImporterError) as ctx:
            run_feed(["--url", url, "--delay", "0"])
        self.assertNotIn("s3cret-pw", str(ctx.exception))
        self.assertIn("public feeds only", str(ctx.exception))
        self.assertEqual(_Handler.requests, [])

    def test_url_without_scheme_gets_a_hint(self):
        with self.assertRaises(ImporterError) as ctx:
            run_feed(["--url", "dailyexample.test/feed/"])
        self.assertIn("https://dailyexample.test/feed/", str(ctx.exception))


class AtomPagingTests(ServerTestCase):
    def test_follows_rfc5005_next_and_prev_archive_once_each(self):
        raws, err = run_feed(["--url", self.url("/atom.xml"), "--delay", "0"])
        # page 2 links "previous" back to page 1 and the archive links "next-archive" to it: no refetch.
        self.assertEqual(_Handler.requests, ["/atom.xml", "/atom.xml?page=2", "/archive/2023.xml"])
        self.assertIn("not following https://elsewhere.invalid/archive/2022.xml", err)
        self.assertIn("3 page(s) in all", err)
        self.assertIn("1 deleted-entry marker(s)", err)

        articles, report = normalized(raws)
        heads = by_headline(articles)
        self.assertEqual(set(heads), {
            "Mayor’s first 100 days",
            "AT&T < Verizon? Readers weigh in on &amp; phone bills",
            "Audit report on city fleet",
            "School board picks new superintendent",
            "Year in review: 2023",
        })
        mayor = heads["Mayor’s first 100 days"]
        self.assertEqual(mayor["authors"], ["Priya Natarajan", "Tom Becker"])
        self.assertEqual(mayor["publish_date"], "2024-04-02T13:15:00Z")  # -04:00 offset
        self.assertEqual(mayor["url"], "https://atom.example.org/news/2024/04/mayor-100-days")  # xml:base
        self.assertEqual(mayor["content"], "Mayor Ellen Park marked 100 days in office on Tuesday.\n\n"
                                           "She said housing is “job one.”")

        phones = heads["AT&T < Verizon? Readers weigh in on &amp; phone bills"]
        self.assertIn("- Most pay over $80.", phones["content"])  # xhtml div unwrapped
        self.assertIn("phone bills & got 300 replies", phones["content"])

        fleet = heads["Audit report on city fleet"]
        self.assertEqual(fleet["authors"], [])  # feed-level author is not inherited by default
        self.assertEqual(fleet["content"], "The audit found 40 vehicles unused for a year.")  # src= content
        self.assertEqual(fleet["publish_date"], "2024-03-30T11:00:00Z")
        self.assertEqual(heads["School board picks new superintendent"]["publish_date"], "2024-03-20T15:00:00Z")
        self.assertEqual(heads["Year in review: 2023"]["content"],
                         "Plain text story. Use <angle brackets> literally & keep this.")

        self.assertEqual(report.skipped["draft (app:draft is yes)"], 1)
        self.assertEqual(report.skipped[
            "Atom entry has no <published> date, only <updated> (--use-updated-date accepts it)"], 1)
        self.assertEqual(report.skipped["no headline"], 1)

    def test_max_pages_stops_link_following(self):
        _, err = run_feed(["--url", self.url("/atom.xml"), "--delay", "0", "--max-pages", "2"])
        self.assertEqual(_Handler.requests, ["/atom.xml", "/atom.xml?page=2"])
        self.assertIn("stopped after 2 page(s)", err)

    def test_opt_in_flags_for_feed_author_and_updated_date(self):
        raws, _ = run_feed(["--url", self.url("/atom.xml"), "--delay", "0", "--paging", "none",
                            "--inherit-feed-author", "--use-updated-date"])
        heads = by_headline(normalized(raws)[0])
        self.assertEqual(heads["Audit report on city fleet"]["authors"], ["Example Newsroom"])
        self.assertEqual(heads["Corrections policy"]["publish_date"], "2024-03-31T08:00:00Z")
        self.assertNotIn("Draft: transit budget", heads)  # drafts stay out regardless

    def test_links_to_other_sites_are_not_followed(self):
        _, err = run_feed(["--url", self.url("/gazette/rss.xml"), "--delay", "0"])
        self.assertEqual(_Handler.requests, ["/gazette/rss.xml"])
        self.assertIn("not following https://news.example.org/rss.xml?page=1", err)


# ---------------------------------------------------------------- saved files

class FileTests(unittest.TestCase):
    def test_generic_rss_authors_links_and_dates(self):
        raws, err = run_feed(["--file", str(FIXTURES / "newsroom_rss.xml")])
        articles, report = normalized(raws)
        heads = by_headline(articles)
        self.assertEqual(heads["Port expansion approved"]["authors"], ["Jane Doe"])  # "email (Name)"
        self.assertEqual(heads["Weather service issues flood watch"]["authors"], [])  # email only
        self.assertIn("1 item(s) had an RSS <author> that was only an email address", err)
        bridge = heads["Opinion: Fix the bridge"]
        self.assertEqual(bridge["authors"], ["Lee Chen"])  # "Name <email>"
        self.assertEqual(bridge["url"], "https://news.example.org/opinion/2024/02/fix-the-bridge")  # guid
        self.assertEqual(heads["Weather service issues flood watch"]["publish_date"], "2024-02-11T15:00:00Z")
        museum = heads["Museum reopens"]
        self.assertEqual(museum["url"], "https://news.example.org/news/2024/02/museum-reopens")  # relative
        self.assertEqual(museum["publish_date"], "2024-02-10T08:30:00Z")  # no zone -> UTC by default
        self.assertIn("1 item date(s) had no time zone and were read as UTC", err)
        self.assertEqual(heads["Transit board meets"]["url"],
                         "https://news.example.org/news/2024/02/transit-board")  # feedburner:origLink
        self.assertNotIn("Concert series announced for 2098", heads)
        self.assertEqual(sum(n for reason, n in report.skipped.items() if "in the future" in reason), 1)
        self.assertIn("links in files aren't followed", err)

    def test_default_timezone_and_include_future(self):
        raws, err = run_feed(["--file", str(FIXTURES / "newsroom_rss.xml"),
                              "--default-timezone", "America/New_York", "--include-future"])
        heads = by_headline(normalized(raws)[0])
        self.assertEqual(heads["Museum reopens"]["publish_date"], "2024-02-10T13:30:00Z")
        self.assertIn("Concert series announced for 2098", heads)
        self.assertIn("read as America/New_York", err)

    def test_unknown_timezone_is_a_user_error(self):
        with self.assertRaises(ImporterError) as ctx:
            run_feed(["--file", str(FIXTURES / "newsroom_rss.xml"), "--default-timezone", "Mars/Olympus"])
        self.assertIn("--default-timezone", str(ctx.exception))

    def test_non_uri_guids_are_namespaced_by_host(self):
        a = feed._make_id("48207", "https://news.example.org/a", "")
        b = feed._make_id("48207", "https://other.example.com/a", "")
        self.assertNotEqual(a, b)
        self.assertEqual(feed._make_id("tag:x.org,2024:1", "https://a.example/", ""),
                         feed._make_id("tag:x.org,2024:1", "https://b.example/", ""))
        self.assertRegex(a, r"^feed-[0-9a-f]{20}$")

    def test_rss1_rdf(self):
        raws, _ = run_feed(["--file", str(FIXTURES / "rss1.rdf")])
        heads = by_headline(normalized(raws)[0])
        harbor = heads["Harbor cleanup & what comes next"]
        self.assertEqual(harbor["authors"], ["Aiko Tanaka"])
        self.assertEqual(harbor["publish_date"], "2024-01-15T01:00:00Z")  # +09:00
        self.assertIn("The city will add bins.", harbor["content"])  # content:encoded over description
        self.assertEqual(heads["Rail line delayed"]["url"], "https://rdf.example.jp/en/2024/01/14/rail.html")
        self.assertEqual(heads["Rail line delayed"]["authors"], [])

    def test_rss091_html_entities_and_missing_dates(self):
        raws, _ = run_feed(["--file", str(FIXTURES / "rss091.xml")])
        self.assertEqual(len(raws), 1)
        self.assertEqual(raws[0]["headline"], "Café owners\xa0rally downtown")
        articles, report = normalized(raws)
        self.assertEqual(articles, [])
        self.assertEqual(report.skipped["no parseable publish date"], 1)  # RSS 0.91 items carry no date

    def test_windows_1252_with_control_characters(self):
        raws, _ = run_feed(["--file", str(FIXTURES / "cp1252_control_chars.xml")])
        (article,), _ = normalized(raws)
        self.assertEqual(article["headline"], "“Not again” — residents on the boil-water notice")
        self.assertEqual(article["authors"], ["José Martínez"])
        self.assertEqual(article["publish_date"], "2024-03-07T14:05:00Z")  # PST
        self.assertIn("It’s the third notice", article["content"])

    def test_small_chunks_parse_the_same(self):
        expected, _ = run_feed(["--file", str(FIXTURES / "wp_page1.xml")])
        with mock.patch.object(feed, "CHUNK", 7):
            chunked, _ = run_feed(["--file", str(FIXTURES / "wp_page1.xml")])
        self.assertEqual(chunked, expected)

    def test_gzip_and_folder_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            (folder / "rss1.rdf.gz").write_bytes(gzip.compress(fixture("rss1.rdf")))
            shutil.copy(FIXTURES / "atom_archive_2023.xml", folder / "2023.atom")
            (folder / "notes.txt").write_text("not a feed")
            raws, err = run_feed(["--file", tmp])
        heads = by_headline(normalized(raws)[0])
        self.assertIn("Harbor cleanup & what comes next", heads)
        self.assertIn("Year in review: 2023", heads)
        self.assertNotIn("notes.txt", err)

    def test_malformed_xml_reports_line(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(ImporterError) as ctx:
            run_feed(["--file", str(FIXTURES / "malformed.xml")])
        message = str(ctx.exception)
        self.assertIn("line 13", message)
        self.assertIn("isn't well-formed XML", message)
        self.assertIn("after 1 item(s)", message)

    def test_not_a_feed(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(ImporterError) as ctx:
                run_feed(["--file", str(FIXTURES / "homepage.html")])
            self.assertIn("web page, not a feed", str(ctx.exception))
            with tempfile.TemporaryDirectory() as tmp:
                empty = pathlib.Path(tmp) / "empty.xml"
                empty.write_bytes(b"")
                with self.assertRaises(ImporterError) as ctx:
                    run_feed(["--file", str(empty)])
                self.assertIn("empty", str(ctx.exception))
                with self.assertRaises(ImporterError) as ctx:
                    run_feed(["--file", str(pathlib.Path(tmp) / "missing.xml")])
                self.assertIn("not found", str(ctx.exception))

    def test_comments_feed_file_refused(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(ImporterError):
            run_feed(["--file", str(FIXTURES / "comments_feed.xml")])

    def test_default_prefix(self):
        ns = argparse.Namespace
        self.assertEqual(feed.default_prefix(ns(url=["https://www.Example.com/feed/"], file=None)),
                         "feed-example.com")
        self.assertEqual(feed.default_prefix(ns(url=None, file=[pathlib.Path("/x/archive.xml.gz")])),
                         "feed-archive")

    def test_help_explains_where_to_find_the_feed(self):
        parser = argparse.ArgumentParser(prog="feed")
        feed.add_arguments(parser)
        text = parser.format_help()
        for needle in ("Where to find the feed URL", "https://SITE/feed/", "rss.xml", "no API key",
                       "Settings > Reading", "--require-full-text"):
            self.assertIn(needle, text)
        self.assertNotIn("\n", feed.HELP)


# ---------------------------------------------------------------- end to end through the CLI

class EndToEndTests(ServerTestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        # Only this importer: sibling importer modules may not exist yet.
        with mock.patch.object(import_archive, "IMPORTERS", ["feed"]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = import_archive.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_import_archive_main_writes_articles(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self.run_main(["feed", "--url", self.url("/feed/"), "--delay", "0",
                                            "--out-dir", tmp])
            self.assertEqual(code, 0, err)
            files = sorted(pathlib.Path(tmp).glob("*.json"))
            self.assertEqual([f.name for f in files], ["feed-127-0-0-1-0001.json"])
            articles = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(len(articles), 4)
            for article in articles:
                self.assertEqual(set(article), {"id", "headline", "content", "url", "authors", "publish_date"})
                self.assertRegex(article["publish_date"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
                self.assertIsInstance(article["authors"], list)
            self.assertIn("Imported 4 article(s) from feed.", out)
            self.assertIn("1 duplicate(s) skipped", out)
            self.assertIn("1 have no byline", out)
            self.assertIn("1 skipped: password-protected post", out)

            # Re-running replaces the same file with the same ids instead of adding duplicates.
            code, _, _ = self.run_main(["feed", "--url", self.url("/feed/"), "--delay", "0", "--out-dir", tmp])
            self.assertEqual(code, 0)
            again = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual([a["id"] for a in again], [a["id"] for a in articles])
            self.assertEqual(len(list(pathlib.Path(tmp).glob("*.json"))), 1)

    def test_limit_stops_fetching_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, _ = self.run_main(["feed", "--url", self.url("/feed/"), "--delay", "0", "--limit", "2",
                                          "--out-dir", tmp, "--prefix", "sample"])
            self.assertEqual(code, 0)
            self.assertEqual(_Handler.requests, ["/feed/"])
            self.assertEqual(len(json.loads((pathlib.Path(tmp) / "sample-0001.json").read_text())), 2)

    def test_failure_returns_1_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_main(["feed", "--file", str(FIXTURES / "malformed.xml"), "--out-dir", tmp])
            self.assertEqual(code, 1)
            self.assertIn("Import failed", err)
            self.assertEqual(list(pathlib.Path(tmp).glob("*.json")), [])


# ---------------------------------------------------------------- review: silent-data-loss regressions

def wp_rss(site: str, items, generator: bool = True) -> bytes:
    """A WordPress-shaped RSS 2.0 page. items: (post id, title, slug, RFC 822 date)."""
    out = ['<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"\n'
           '\txmlns:content="http://purl.org/rss/1.0/modules/content/"\n'
           '\txmlns:dc="http://purl.org/dc/elements/1.1/"\n'
           '\txmlns:atom="http://www.w3.org/2005/Atom"\n\t>\n<channel>',
           f"\t<title>The Daily Example</title>\n\t<link>{site}</link>\n\t<description>News</description>"]
    if generator:
        out.append("\t<generator>https://wordpress.org/?v=6.4.3</generator>")
    for pid, title, slug, date in items:
        out.append(f"""\t<item>
\t\t<title>{title}</title>
\t\t<link>{site}/{slug}/</link>
\t\t<dc:creator><![CDATA[Ana Lima]]></dc:creator>
\t\t<pubDate>{date}</pubDate>
\t\t<guid isPermaLink="false">{site}/?p={pid}</guid>
\t\t<description><![CDATA[{title}.]]></description>
\t\t<content:encoded><![CDATA[<p>{title}.</p>]]></content:encoded>
\t</item>""")
    out.append("</channel>\n</rss>\n")
    return "\n".join(out).encode("utf-8")


def loop_atom(path: str):
    """A CMS that ignores ?page= but always emits rel="next" to page N+1."""
    m = re.fullmatch(r"/loop/atom\.xml(?:\?page=(\d+))?", path)
    if not m:
        return None
    n = int(m.group(1) or 1)
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Loop CMS</title>
  <id>tag:loop.example.org,2024:feed</id>
  <updated>2024-04-02T14:00:00Z</updated>
  <link rel="self" href="atom.xml?page={n}"/>
  <link rel="next" href="atom.xml?page={n + 1}"/>
  <entry>
    <title>Only story</title>
    <link rel="alternate" href="https://loop.example.org/news/only-story"/>
    <id>tag:loop.example.org,2024:story-1</id>
    <published>2024-04-01T12:00:00Z</published>
    <updated>2024-04-01T12:00:00Z</updated>
    <author><name>Sam Park</name></author>
    <content type="html">&lt;p&gt;Same page every time.&lt;/p&gt;</content>
  </entry>
</feed>""".encode("utf-8")
    return 200, ATOM, body


SITE = "https://dailyexample.test"
BLOCK = [(204, "Senate race tightens", "senate-race", "Mon, 04 Mar 2024 12:00:00 +0000"),
         (203, "Ballot measure explained", "ballot-measure", "Sun, 03 Mar 2024 12:00:00 +0000")]


class ReviewServerTests(ServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _Handler.routes.update({
            "/nogen/feed/": (200, RSS, fixture("wp_nogenerator_page1.xml")),
            "/nogen/feed/?paged=2": (200, RSS, fixture("wp_nogenerator_page2.xml")),
            "/blank/feed/": (200, RSS, b"\n\n" + fixture("wp_page1.xml")),
            # Two category feeds of one site; politics page 2 holds only stories also filed in news.
            "/category/news/feed/": (200, RSS, wp_rss(SITE, [
                (210, "Transit fares rise", "transit-fares", "Tue, 05 Mar 2024 12:00:00 +0000"),
                (209, "Mayor signs budget", "mayor-budget", "Tue, 05 Mar 2024 09:00:00 +0000")])),
            "/category/news/feed/?paged=2": (200, RSS, wp_rss(SITE, BLOCK)),
            "/category/politics/feed/": (200, RSS, wp_rss(SITE, [
                (212, "Debate night recap", "debate-recap", "Wed, 06 Mar 2024 12:00:00 +0000"),
                (211, "Poll shows dead heat", "poll-dead-heat", "Wed, 06 Mar 2024 09:00:00 +0000")])),
            "/category/politics/feed/?paged=2": (200, RSS, wp_rss(SITE, BLOCK)),
            "/category/politics/feed/?paged=3": (200, RSS, wp_rss(SITE, [
                (150, "Primary results 2023", "primary-2023", "Wed, 17 May 2023 23:00:00 +0000")])),
        })
        _Handler.dynamic = staticmethod(loop_atom)

    @classmethod
    def tearDownClass(cls):
        _Handler.dynamic = None
        super().tearDownClass()

    def test_wordpress_without_generator_tag_is_still_paged(self):
        # Security plugins often strip <generator>; the ?p=N guids still say WordPress.
        raws, err = run_feed(["--url", self.url("/nogen/feed/"), "--delay", "0"])
        self.assertEqual(_Handler.requests, ["/nogen/feed/", "/nogen/feed/?paged=2", "/nogen/feed/?paged=3"])
        heads = by_headline(normalized(raws)[0])
        self.assertIn("School levy passes narrowly", heads)

    def test_overlapping_feeds_in_one_run_page_independently(self):
        raws, err = run_feed(["--url", self.url("/category/news/feed/"), self.url("/category/politics/feed/"),
                              "--delay", "0"])
        self.assertIn("/category/politics/feed/?paged=3", _Handler.requests)
        heads = by_headline(normalized(raws)[0])
        self.assertIn("Primary results 2023", heads)
        self.assertNotIn("seems to ignore", err)

    def test_link_paging_stops_when_pages_repeat(self):
        _, err = run_feed(["--url", self.url("/loop/atom.xml"), "--delay", "0", "--max-pages", "25"])
        self.assertEqual(_Handler.requests, ["/loop/atom.xml", "/loop/atom.xml?page=2"])
        self.assertIn("no items that weren't already read", err)

    def test_blank_line_before_xml_declaration_over_http(self):
        raws, _ = run_feed(["--url", self.url("/blank/feed/"), "--delay", "0", "--paging", "none"])
        self.assertEqual(len(raws), 4)


class ReviewFileTests(unittest.TestCase):
    def edge(self):
        raws, err = run_feed(["--file", str(FIXTURES / "edge_cases_rss.xml")])
        articles, report = normalized(raws)
        return by_headline(articles), articles, report, err

    def test_non_url_guid_is_never_turned_into_a_url(self):
        heads, articles, report, _ = self.edge()
        # <guid>48207</guid> with no <link>: resolving it against the feed would invent .../48207.
        self.assertNotIn("Water main breaks on Elm Street", heads)
        self.assertFalse([a["url"] for a in articles if "48207" in a["url"]])
        self.assertEqual(report.skipped["no valid http(s) URL"], 1)

    def test_colon_and_gmt_offsets_are_not_read_as_utc(self):
        heads, _, _, err = self.edge()
        self.assertEqual(heads["Mumbai desk: monsoon preparations begin early"]["publish_date"],
                         "2024-03-04T03:30:00Z")  # +05:30
        self.assertEqual(heads["Berlin desk: budget talks stall"]["publish_date"], "2024-03-04T08:00:00Z")  # GMT+1
        self.assertEqual(heads["Late edition: council adjourns"]["publish_date"],
                         "2024-03-04T22:15:00Z")  # RFC 3339 lowercase t/z
        self.assertNotIn("had no time zone", err)

    def test_combined_bylines_become_separate_authors(self):
        heads, _, _, _ = self.edge()
        # Co-Authors Plus and similar put every byline in one dc:creator; author filters match exactly.
        self.assertEqual(heads["Mumbai desk: monsoon preparations begin early"]["authors"],
                         ["Jane Doe", "John Roe", "Ann Lee"])
        self.assertEqual(heads["Berlin desk: budget talks stall"]["authors"], ["Max Weber", "Clara Zetkin"])
        self.assertEqual(heads["Late edition: council adjourns"]["authors"], ["Doe, Jane"])  # no conjunction

    def test_guid_equal_to_homepage_does_not_merge_items(self):
        heads, _, report, _ = self.edge()
        monsoon = heads["Mumbai desk: monsoon preparations begin early"]
        berlin = heads["Berlin desk: budget talks stall"]
        self.assertNotEqual(monsoon["id"], berlin["id"])
        self.assertEqual(berlin["id"], feed._make_id("https://edge.example.com/2024/03/berlin-budget", "", ""))

    def test_blank_line_or_bom_before_xml_declaration(self):
        body = fixture("wp_page1.xml")
        expected, _ = run_feed(["--file", str(FIXTURES / "wp_page1.xml")])
        with tempfile.TemporaryDirectory() as tmp:
            for name, prefix in (("blank.xml", b"\n\n"), ("bom.xml", b"\xef\xbb\xbf\r\n  ")):
                path = pathlib.Path(tmp) / name
                path.write_bytes(prefix + body)
                self.assertEqual(run_feed(["--file", str(path)])[0], expected)
                with mock.patch.object(feed, "CHUNK", 1):
                    self.assertEqual(run_feed(["--file", str(path)])[0], expected)

    def test_multibyte_legacy_encodings(self):
        raws, _ = run_feed(["--file", str(FIXTURES / "sjis_feed.xml")])
        heads = by_headline(normalized(raws)[0])
        harbor = heads["港の清掃活動に３００人が参加"]
        self.assertEqual(harbor["authors"], ["田中 愛子"])
        self.assertEqual(harbor["publish_date"], "2024-03-03T01:00:00Z")
        self.assertIn("市はごみ箱を増やす方針だ。", harbor["content"])
        self.assertEqual(heads["新駅の開業、一年遅れに"]["authors"], [])
        with mock.patch.object(feed, "CHUNK", 3):  # split inside multibyte characters
            self.assertEqual(run_feed(["--file", str(FIXTURES / "sjis_feed.xml")])[0], raws)
        with tempfile.TemporaryDirectory() as tmp:
            for enc, title in (("GB2312", "北京新闻"), ("Big5", "臺北新聞"), ("EUC-KR", "서울 뉴스"),
                               ("EUC-JP", "東京の天気")):
                path = pathlib.Path(tmp) / f"{enc}.xml"
                path.write_bytes((f'<?xml version="1.0" encoding="{enc}"?>\n<rss version="2.0"><channel>'
                                  f"<title>T</title><item><title>{title}</title>"
                                  "<link>https://cjk.example/a</link><description>本文</description>"
                                  "<pubDate>Mon, 04 Mar 2024 09:00:00 +0900</pubDate></item></channel></rss>"
                                  ).encode(enc))
                (raw,), _ = run_feed(["--file", str(path)])
                self.assertEqual(raw["headline"], title, enc)
            bogus = pathlib.Path(tmp) / "bogus.xml"
            bogus.write_bytes(b'<?xml version="1.0" encoding="x-no-such-charset"?><rss><channel/></rss>')
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(ImporterError):
                run_feed(["--file", str(bogus)])

    def test_latin1_label_with_windows_1252_quotes_and_one_broken_link(self):
        raws, _ = run_feed(["--file", str(FIXTURES / "legacy_latin1.xml")])
        articles, report = normalized(raws)
        heads = by_headline(articles)
        # Declared ISO-8859-1, typed in Word: bytes 0x93/0x94/0x97 are curly quotes and a dash, as browsers
        # read them, not C1 control characters.
        story = heads["“Not again” — café owners on the boil-water notice"]
        self.assertIn("It’s the third notice", story["content"])
        self.assertEqual(story["authors"], ["Renée Dubois"])
        self.assertFalse([a for a in articles if any("\x80" <= ch <= "\x9f" for ch in a["headline"] + a["content"])])
        # One malformed <link> skips that item only; it must not crash the whole import.
        self.assertNotIn("Harbor dredging resumes", heads)
        self.assertEqual(report.skipped["no valid http(s) URL"], 1)

    def test_corrupt_gzip_is_a_user_error(self):
        blob = bytearray(gzip.compress(fixture("wp_page1.xml") * 3))
        blob[30:60] = b"\xff" * 30
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "feed.xml.gz"
            path.write_bytes(bytes(blob))
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(ImporterError) as ctx:
                run_feed(["--file", str(path)])
        self.assertIn("feed.xml.gz", str(ctx.exception))

    def test_secrets_in_malformed_urls_are_not_echoed(self):
        for url, secret in (("editor:s3cret-pw@news.example.com/feed/", "s3cret-pw"),
                            ("ftp://editor:s3cret-pw@news.example.com/feed/", "s3cret-pw"),
                            ("//editor:s3cret-pw@news.example.com/feed/", "s3cret-pw"),
                            ("news.example.com/feed/?token=abc123secret", "abc123secret")):
            with self.assertRaises(ImporterError) as ctx:
                run_feed(["--url", url])
            self.assertNotIn(secret, str(ctx.exception), url)
        with self.assertRaises(ImporterError) as ctx:
            run_feed(["--url", "news.example.com/feed/?token=abc123secret"])
        self.assertIn("https://news.example.com/feed/", str(ctx.exception))

    def test_feed_discovery_keeps_sites_named_comment(self):
        page = (b'<html><head>'
                b'<link rel="alternate" type="application/rss+xml" title="Commentary Magazine &raquo; '
                b'Comments Feed" href="https://www.commentary.example/comments/feed/">'
                b'<link rel="alternate" type="application/rss+xml" title="Commentary Magazine &raquo; Feed" '
                b'href="https://www.commentary.example/feed/"></head></html>')
        self.assertEqual(feed._discover_feed("https://www.commentary.example/", page),
                         "https://www.commentary.example/feed/")
        story = (b'<link rel="alternate" type="application/rss+xml" title="Example &raquo; Budget vote '
                 b'Comments Feed" href="https://dailyexample.test/2024/03/05/budget-vote/feed/">')
        self.assertIsNone(feed._discover_feed("https://dailyexample.test/2024/03/05/budget-vote/", story))


if __name__ == "__main__":
    unittest.main()

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import argparse  # noqa: E402
import gzip  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
import zipfile  # noqa: E402
from contextlib import redirect_stderr, redirect_stdout  # noqa: E402
from unittest import mock  # noqa: E402

import import_archive  # noqa: E402
from importers import wordpress_api, wordpress_export  # noqa: E402
from importers.common import ArticleWriter, ImporterError, ImportReport, normalize  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "wordpress_export"
MAIN = FIXTURES / "ledger.WordPress.2024-05-01.xml"
LEGACY = FIXTURES / "legacy-wxr-1.0.xml"
WPCLI = FIXTURES / "wpcli"
SITE_KEY = "example-ledger-com"
DEFAULT_IMPORTED = ["101", "102", "103", "119", "121", "122", "123", "124"]
SECRETS = ["COMMENT TEXT", "reader-secret@example.net", "203.0.113.9", "NOT ARTICLE TEXT", "sam-secret@example.net",
           "s3cret-embargo", "jdoe@example-ledger.com", "Private memo text", "Unpublished notes",
           "Subscriber-only analysis", "Paid newsletter text"]


def wp_id(n):
    return f"wp-{SITE_KEY}-{n}"


def parse_args(argv):
    parser = argparse.ArgumentParser()
    wordpress_export.add_arguments(parser)
    return parser.parse_args([str(a) for a in argv])


def run(argv):
    """iter_articles + normalize, as import_archive does. Returns (articles by id, report, stderr text)."""
    args = parse_args(argv)
    report = ImportReport(source="wordpress-export")
    err = io.StringIO()
    articles = {}
    with redirect_stderr(err):
        for raw in wordpress_export.iter_articles(args):
            article = normalize(raw, report)
            if article:
                articles.setdefault(article["id"], article)
    return articles, report, err.getvalue()


def run_error(argv) -> str:
    with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
        try:
            list(wordpress_export.iter_articles(parse_args(argv)))
        except ImporterError as e:
            return str(e)
    raise AssertionError("expected ImporterError")


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name, data):
        path = self.tmp / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path


# ---------------------------------------------------------------- parsing and mapping

class ParsingTests(TempDirTestCase):
    def test_published_posts_only_with_all_fields(self):
        articles, report, _ = run(["--file", MAIN])
        self.assertEqual(sorted(articles), [wp_id(n) for n in DEFAULT_IMPORTED])
        self.assertEqual(articles[wp_id(101)], {
            "id": wp_id(101),
            "headline": "Council’s budget: Q&A",  # &#8217; and &amp; decoded once, as WordPress displays it
            "content": "The council approved a $1.2 billion budget on Tuesday.\n\nWhat changes\n\n"
                       "- More bus routes\n- Two new libraries",
            "url": "https://www.example-ledger.com/2024/03/05/council-budget-qa/",
            "authors": ["Jane Doe"],
            "publish_date": "2024-03-05T14:30:00Z",
        })
        self.assertEqual(len(articles), len(DEFAULT_IMPORTED))

    def test_classic_editor_text_shortcodes_cdata_split_and_cp1252_repair(self):
        articles, _, _ = run(["--file", MAIN])
        self.assertEqual(articles[wp_id(102)]["content"],
                         "The port authority wants to deepen the main channel to 45 feet.\n\n"
                         "Dredging barges at the main channel.\n\n"
                         "Engineers call it the “deeper channel” plan.\n"
                         "A broken sensor logged the string ]]> before going offline.\n\n"
                         "Who pays [sic]? State and federal grants would cover most of the cost.")

    def test_illegal_control_characters_are_dropped_not_fatal(self):
        articles, _, _ = run(["--file", MAIN])
        self.assertEqual(articles[wp_id(122)]["content"], "Branches will open on Sundays starting in June.")

    def test_comments_emails_passwords_and_other_fields_never_leak(self):
        articles, report, err = run(["--file", MAIN, "--include-private"])
        dumped = json.dumps(list(articles.values()), ensure_ascii=False) + err + report.summary()
        for secret in SECRETS[:-4]:
            self.assertNotIn(secret, dumped)
        articles, report, err = run(["--file", MAIN])
        dumped = json.dumps(list(articles.values()), ensure_ascii=False) + err + report.summary()
        for secret in SECRETS:
            self.assertNotIn(secret, dumped)

    def test_ids_are_stable_namespaced_and_match_the_wordpress_api_importer(self):
        first, _, _ = run(["--file", MAIN])
        second, _, _ = run(["--file", MAIN])
        self.assertEqual(list(first), list(second))
        for url in ("https://www.example-ledger.com", "http://valleygazette.example.org/news/",
                    "https://news.example.com:8443/", "https://EXAMPLE.com/wp-json/", "not a url"):
            self.assertEqual(wordpress_export._site_key(url), wordpress_api._site_key(url), url)

    def test_streams_across_tiny_chunks(self):
        expected, _, _ = run(["--file", MAIN])
        with mock.patch.object(wordpress_export, "CHUNK", 37):
            articles, _, _ = run(["--file", MAIN])
        self.assertEqual(articles, expected)

    def test_items_are_handed_over_one_at_a_time(self):
        target = wordpress_export._WxrTarget("t", want_content=True, meta_keys=set())
        source = wordpress_export._Source(str(MAIN), MAIN)
        seen = []
        with mock.patch.object(wordpress_export, "CHUNK", 512):
            for item in wordpress_export._parse_items(source, target):
                seen.append(item.get("wp:post_id"))
                self.assertLessEqual(len(target.ready), 3)
        self.assertEqual(len(seen), 26)
        self.assertEqual(target.authors[1]["wp:author_display_name"], "Jane Doe")
        self.assertNotIn("wp:author_email", target.authors[1])

    def test_gzip_and_zip_exports(self):
        expected, _, _ = run(["--file", MAIN])
        gz = self.write("ledger.WordPress.2024-05-01.xml.gz", gzip.compress(MAIN.read_bytes()))
        articles, _, _ = run(["--file", gz])
        self.assertEqual(articles, expected)
        zpath = self.tmp / "ledger-export.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("__MACOSX/._ledger.WordPress.2024-05-01.xml", b"\x00\x05\x16\x07junk")
            zf.writestr("ledger.WordPress.2024-05-01.xml", MAIN.read_bytes())
            zf.writestr("README.txt", "not an export")
        articles, _, err = run(["--file", zpath])
        self.assertEqual(articles, expected)
        self.assertIn("ledger-export.zip > ledger.WordPress.2024-05-01.xml", err)

    def test_wp_cli_split_files_resolve_guest_authors_across_files(self):
        articles, report, err = run(["--file", WPCLI])
        self.assertEqual(sorted(articles), [wp_id(201), wp_id(202), wp_id(203)])
        # The Dana Lee profile is in the second file; the post that credits her is in the first.
        self.assertEqual(articles[wp_id(201)]["authors"], ["Dana Lee", "Jane Doe"])
        self.assertEqual(articles[wp_id(202)]["authors"], ["María González"])
        self.assertIn("4 item(s) in 2 files.", err)
        self.assertNotIn("Co-Authors Plus bylines whose names", err)

    def test_overlapping_exports_in_one_run_are_not_duplicated(self):
        report = ImportReport(source="wordpress-export")
        with redirect_stderr(io.StringIO()), ArticleWriter(self.tmp, "t", report) as writer:
            writer.add_all(wordpress_export.iter_articles(parse_args(["--file", MAIN, MAIN])))
        self.assertEqual(report.written, len(DEFAULT_IMPORTED))
        self.assertEqual(report.duplicates, len(DEFAULT_IMPORTED))


# ---------------------------------------------------------------- what is imported

class StatusTests(unittest.TestCase):
    def test_non_published_and_non_article_items_are_skipped_with_reasons(self):
        _, report, _ = run(["--file", MAIN])
        expected = {
            "draft or pending review (not published)": 1,
            "private post (use --include-private to import)": 1,
            "password-protected (its text isn't public)": 1,
            "scheduled, not yet published": 1,
            "in the trash": 1,
            "page, not a post (add --post-type page to import pages)": 1,
            "media library item (attachment), not an article": 1,
            "menu item, not an article": 1,
            "saved revision of another post": 1,
            "Co-Authors Plus guest author profile (used for bylines), not an article": 1,
            "site setting or template (wp_navigation), not an article": 1,
            "post type 'story' not selected (add --post-type story to import it)": 1,
            "subscribers or paid members only (Jetpack); use --include-members-only to import": 1,
            "behind a Jetpack paywall block; use --include-members-only to import": 1,
            "no headline": 1,
            "no valid http(s) URL": 1,
            "no id": 1,
        }
        for reason, n in expected.items():
            self.assertEqual(report.skipped[reason], n, reason)
        self.assertEqual(sum(1 for r in report.skipped if r.startswith("no UTC publish date")), 1)
        self.assertEqual(sum(report.skipped.values()), 18)

    def test_include_private(self):
        articles, _, _ = run(["--file", MAIN, "--include-private"])
        self.assertEqual(articles[wp_id(105)]["headline"], "Newsroom staffing memo")
        self.assertEqual(articles[wp_id(105)]["authors"], ["Richard Roe"])

    def test_drafts_scheduled_trashed_and_protected_have_no_override(self):
        articles, _, _ = run(["--file", MAIN, "--include-private", "--include-members-only"])
        for n in (104, 106, 107, 108):
            self.assertNotIn(wp_id(n), articles)

    def test_post_type_selection(self):
        articles, report, _ = run(["--file", MAIN, "--post-type", "story"])
        self.assertEqual(list(articles), [wp_id(118)])
        # A byline plugin that names its terms after the person (no cap- slug) is read as is.
        self.assertEqual(articles[wp_id(118)]["authors"], ["Alex Writer"])
        self.assertIn("post type 'post' not selected (add --post-type post to import it)", report.skipped)
        articles, _, _ = run(["--file", MAIN, "--post-type", "post", "--post-type", "Story"])
        self.assertEqual(len(articles), len(DEFAULT_IMPORTED) + 1)

    def test_members_only_override(self):
        articles, _, _ = run(["--file", MAIN, "--include-members-only"])
        self.assertIn("Paid newsletter text.", articles[wp_id(113)]["content"])
        self.assertEqual(articles[wp_id(114)]["content"], "Free teaser.\n\nSubscriber-only analysis.")

    def test_members_only_values(self):
        def reason(meta, body=""):
            return wordpress_export._members_only_reason({"meta": meta}, body)
        access = wordpress_export.JETPACK_ACCESS
        self.assertIsNone(reason({access: ["everybody"]}))
        self.assertIsNone(reason({access: [""]}))
        self.assertIsNone(reason({access: ['a:1:{i:0;s:0:"";}']}))  # Jetpack reads non-text values as everybody
        self.assertIsNotNone(reason({access: ["subscribers"]}))
        self.assertIsNotNone(reason({access: ["paid_subscribers_all_tiers"]}))
        self.assertIsNotNone(reason({wordpress_export.JETPACK_PAID: ["1"]}))
        self.assertIsNone(reason({wordpress_export.JETPACK_PAID: ["0"]}))
        self.assertIsNotNone(reason({}, "<!-- wp:jetpack/paywall /-->"))

    def test_exclude_meta(self):
        articles, report, _ = run(["--file", MAIN, "--exclude-meta", "_paywall_level"])
        self.assertNotIn(wp_id(124), articles)
        self.assertEqual(report.skipped["excluded by --exclude-meta _paywall_level"], 1)
        articles, _, _ = run(["--file", MAIN, "--exclude-meta", "_paywall_level=free"])
        self.assertIn(wp_id(124), articles)
        articles, report, _ = run(["--file", MAIN, "--exclude-meta", "_paywall_level=premium"])
        self.assertEqual(report.skipped["excluded by --exclude-meta _paywall_level=premium"], 1)

    def test_reasons_from_the_file_are_sanitized(self):
        self.assertEqual(wordpress_export._status_reason("Pitch<script>"),
                         "status 'pitchscript' (only published posts are imported)")
        self.assertEqual(wordpress_export._status_reason(""),
                         "no publish status in the export (wp:status), so it can't be confirmed as published")
        self.assertEqual(wordpress_export._type_reason("x" * 100 + "'\n"),
                         f"post type '{'x' * 40}' not selected (add --post-type {'x' * 40} to import it)")


# ---------------------------------------------------------------- dates

class DateTests(unittest.TestCase):
    def test_utc_date_then_pubdate_with_its_offset(self):
        articles, _, _ = run(["--file", MAIN])
        self.assertEqual(articles[wp_id(101)]["publish_date"], "2024-03-05T14:30:00Z")
        self.assertEqual(articles[wp_id(123)]["publish_date"], "2024-01-12T15:00:00Z")  # pubDate -0500

    def test_missing_utc_date_is_skipped_unless_timezone_given(self):
        articles, report, _ = run(["--file", MAIN])
        self.assertNotIn(wp_id(112), articles)
        reason = next(r for r in report.skipped if r.startswith("no UTC publish date"))
        self.assertIn("--timezone", reason)
        articles, _, err = run(["--file", MAIN, "--timezone", "America/New_York"])
        self.assertEqual(articles[wp_id(112)]["publish_date"], "2009-07-04T22:00:00Z")  # EDT, not EST
        self.assertEqual(articles[wp_id(112)]["content"],
                         "Fireworks over the river drew a record crowd.\nPolice reported no arrests.")
        self.assertIn("1 post(s) had no UTC date", err)
        self.assertNotIn("doesn't match", err)

    def test_fixed_offsets_as_wordpress_offers_them(self):
        articles, _, _ = run(["--file", MAIN, "--timezone", "UTC-5"])
        self.assertEqual(articles[wp_id(112)]["publish_date"], "2009-07-04T23:00:00Z")
        tz = wordpress_export._timezone_arg
        sample = wordpress_export.datetime(2024, 1, 1)
        self.assertEqual(tz("UTC+5:30").utcoffset(sample).total_seconds(), 5.5 * 3600)
        self.assertEqual(tz("UTC+5.75").utcoffset(sample).total_seconds(), 5.75 * 3600)
        self.assertEqual(tz("-03:30").utcoffset(sample).total_seconds(), -3.5 * 3600)
        self.assertEqual(tz("UTC").utcoffset(sample).total_seconds(), 0)
        for bad in ("Mars/Olympus", "UTC+25", "+5:75"):
            with self.assertRaises(argparse.ArgumentTypeError):
                tz(bad)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["--file", MAIN, "--timezone", "Not/AZone"])

    def test_timezone_that_contradicts_the_export_is_reported(self):
        _, _, err = run(["--file", MAIN, "--timezone", "UTC"])
        self.assertIn("doesn't match the export's own UTC date", err)

    def test_placeholder_dates_are_rejected(self):
        rss, wp = wordpress_export._rss_datetime, wordpress_export._wp_datetime
        self.assertIsNone(rss("Mon, 30 Nov -0001 00:00:00 +0000"))  # email.utils would say 2000-11-30
        self.assertIsNone(rss("Thu, 01 Jan 1970 00:00:00 +0000"))
        self.assertIsNone(rss(""))
        self.assertEqual(rss("Tue, 05 Mar 2024 14:30:00 +0000").isoformat(), "2024-03-05T14:30:00+00:00")
        self.assertEqual(rss("05 Mar 2024 09:30 EST").isoformat(), "2024-03-05T14:30:00+00:00")
        self.assertIsNone(wp("0000-00-00 00:00:00"))
        self.assertIsNone(wp("2024-02-30 10:00:00"))
        self.assertEqual(wp("2024-02-29 10:00:00").isoformat(), "2024-02-29T10:00:00")


# ---------------------------------------------------------------- bylines

class BylineTests(unittest.TestCase):
    def test_post_author_login_is_mapped_to_display_name(self):
        articles, _, _ = run(["--file", MAIN])
        self.assertEqual(articles[wp_id(101)]["authors"], ["Jane Doe"])
        self.assertEqual(articles[wp_id(122)]["authors"], ["María González"])

    def test_coauthors_plus_users_and_guest_authors_in_byline_order(self):
        articles, _, _ = run(["--file", MAIN])
        # Not the uploader (jdoe): the Co-Authors Plus terms are the byline.
        self.assertEqual(articles[wp_id(102)]["authors"], ["María González", "Sam O’Neil"])

    def test_unresolved_coauthor_is_left_out_and_explained(self):
        articles, _, err = run(["--file", MAIN])
        self.assertEqual(articles[wp_id(119)]["authors"], ["Jane Doe"])
        self.assertNotIn("ghostwriter", json.dumps(articles[wp_id(119)]))
        self.assertIn("1 post(s) have Co-Authors Plus bylines whose names aren't in this export", err)
        self.assertIn("All content", err)

    def test_missing_byline_stays_empty(self):
        articles, report, _ = run(["--file", MAIN])
        self.assertEqual(articles[wp_id(103)]["authors"], [])
        self.assertEqual(sum(1 for a in articles.values() if not a["authors"]), 1)

    def test_byline_custom_field(self):
        articles, _, _ = run(["--file", MAIN, "--byline-meta", "byline"])
        self.assertEqual(articles[wp_id(121)]["authors"], ["Pat Smith", "Lee Wong"])
        self.assertEqual(articles[wp_id(101)]["authors"], ["Jane Doe"])  # no field: falls back

    def test_byline_field_shapes(self):
        people = wordpress_export._People()
        people.add_user({"wp:author_id": "2", "wp:author_login": "jdoe", "wp:author_display_name": "Jane Doe"})
        people.add_guest({"wp:post_id": "130", "wp:post_name": "cap-sam-oneil", "title": "Sam O’Neil",
                          "meta": {"cap-user_login": ["sam-oneil"]}})
        opts = wordpress_export._options(parse_args(["--file", MAIN]))
        bylines = wordpress_export._Bylines(opts)
        resolve = bylines._from_meta
        self.assertEqual(resolve(['a:2:{i:0;s:1:"2";i:1;i:130;}'], people), ["Jane Doe", "Sam O’Neil"])
        self.assertEqual(resolve(["[2]"], people), ["Jane Doe"])
        self.assertEqual(resolve(["Jane Doe, Metro Reporter"], people), ["Jane Doe, Metro Reporter"])
        self.assertEqual(resolve(["By Ann Lee, Bo Diaz & Cy Young"], people), ["Ann Lee", "Bo Diaz", "Cy Young"])
        self.assertEqual(resolve(["99"], people), [])
        self.assertEqual(bylines.unresolved_ids, 1)
        self.assertIsNone(wordpress_export._php_flat_array('a:1:{i:0;a:0:{}}'))
        self.assertEqual(wordpress_export._php_flat_array('a:1:{s:4:"main";s:5:"Ana ;";}'), ["Ana ;"])
        self.assertEqual(wordpress_export._php_flat_array('a:1:{i:0;s:6:"Zoë ;";}'), ["Zoë ;"])  # byte lengths
        self.assertIsNone(wordpress_export._php_flat_array('a:1:{i:0;s:9:"short";}'))

    def test_ignore_author(self):
        for name in ("webdesk", "Web Desk"):
            articles, _, _ = run(["--file", MAIN, "--ignore-author", name])
            self.assertEqual(articles[wp_id(121)]["authors"], [], name)
            self.assertEqual(articles[wp_id(101)]["authors"], ["Jane Doe"])

    def test_file_without_author_list_uses_the_creator_field(self):
        articles, _, _ = run(["--file", LEGACY])
        self.assertEqual(articles["wp-valleygazette-example-org-news-7"]["authors"], ["Ruth Baker"])


# ---------------------------------------------------------------- old, odd and broken files

RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Ledger</title><link>https://www.example-ledger.com</link>
<item><title>Story</title><link>https://www.example-ledger.com/story/</link>
<pubDate>Tue, 05 Mar 2024 14:30:00 +0000</pubDate><content:encoded><![CDATA[<p>Text</p>]]></content:encoded></item>
</channel></rss>
"""


class FileProblemTests(TempDirTestCase):
    def test_legacy_wxr_1_0_latin1_export_with_php_notice(self):
        articles, report, err = run(["--file", LEGACY])
        article = articles["wp-valleygazette-example-org-news-7"]
        self.assertEqual(article["headline"], "Mill & dam: a history — part 1")
        self.assertEqual(article["content"],
                         "The mill opened in 1851.\n\nThe dam came later, after the “great flood” of 1867.")
        self.assertEqual(article["publish_date"], "2009-11-15T17:00:00Z")
        self.assertEqual(report.skipped["draft or pending review (not published)"], 1)
        self.assertEqual(err.count("before the XML declaration"), 1)
        self.assertIn("WXR 1.0", err)

    def test_truncated_export(self):
        data = MAIN.read_bytes()
        path = self.write("cut.xml", data[: int(len(data) * 0.6)])
        msg = run_error(["--file", path])
        self.assertIn("ends unexpectedly", msg)
        self.assertIn("cut off", msg)
        self.assertIn("date range", msg)

    def test_php_warnings_in_the_middle(self):
        text = MAIN.read_text(encoding="utf-8")
        # PHP's HTML-formatted notice happens to be well-formed XML: it is ignored like any unknown element.
        harmless = text.replace("\t<item>", "<br />\n<b>Warning</b>:  Cannot modify header information - headers "
                                "already sent in <b>/var/www/wp-admin/includes/export.php</b> on line <b>42</b>"
                                "<br />\n\t<item>", 3)
        articles, _, _ = run(["--file", self.write("notice.xml", harmless)])
        self.assertEqual(sorted(articles), [wp_id(n) for n in DEFAULT_IMPORTED])
        # Plain-text warnings with a bare '&' are not.
        broken = text.replace("\t<item>", 'PHP Warning:  Undefined array key "x" in /var/www/wp-content/plugins/'
                              "r&d-tools/export.php on line 12\n\t<item>", 1)
        msg = run_error(["--file", self.write("warn.xml", broken)])
        self.assertIn("Couldn't read", msg)
        self.assertRegex(msg, r"line \d+, column \d+")
        self.assertIn("PHP 'Warning'", msg)

    def test_web_page_instead_of_export(self):
        for body in ("<!DOCTYPE html>\n<html><head><title>Log In</title></head><body><form></form></body></html>",
                     "<html><body><p>Log in<br></p></body></html>"):
            msg = run_error(["--file", self.write("login.xml", body)])
            self.assertIn("web page, not a WordPress export", msg)

    def test_rss_feed_instead_of_export(self):
        msg = run_error(["--file", self.write("feed.xml", RSS_FEED)])
        self.assertIn("RSS feed, not a WordPress export", msg)
        self.assertIn("feed importer", msg)
        atom = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title></feed>'
        self.assertIn("Atom/RSS feed", run_error(["--file", self.write("atom.xml", atom)]))

    def test_doctype_and_entities_are_refused(self):
        bomb = ('<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
                '<rss version="2.0"><channel><title>&b;</title></channel></rss>')
        self.assertIn("DOCTYPE", run_error(["--file", self.write("bomb.xml", bomb)]))

    def test_empty_and_non_xml_files(self):
        self.assertIn("is empty", run_error(["--file", self.write("empty.xml", b"")]))
        self.assertIn("isn't a WordPress export", run_error(["--file", self.write("notes.xml", "just some notes")]))

    def test_missing_paths_and_bad_archives(self):
        msg = run_error(["--file", self.tmp / "nope.xml"])
        self.assertIn("Export file not found", msg)
        self.assertIn("Tools > Export", msg)
        (self.tmp / "empty-folder").mkdir()
        (self.tmp / "empty-folder" / "uploads.tar.gz").write_bytes(gzip.compress(b"not an export"))
        self.assertIn("No export files", run_error(["--file", self.tmp / "empty-folder"]))
        self.assertIn("can't be opened", run_error(["--file", self.write("bad.zip", b"PK\x03\x04garbage")]))
        zpath = self.tmp / "no-xml.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("media/photo.jpg", b"\xff\xd8")
        self.assertIn("contains no .xml export files", run_error(["--file", zpath]))
        broken_gz = self.write("ledger.xml.gz", gzip.compress(MAIN.read_bytes())[:400])
        self.assertIn("Couldn't read", run_error(["--file", broken_gz]))

    def test_export_with_no_items(self):
        text = MAIN.read_text(encoding="utf-8")
        empty = text[: text.index("\t<item>")] + "</channel>\n</rss>\n"
        articles, report, err = run(["--file", self.write("empty-export.xml", empty)])
        self.assertEqual(articles, {})
        self.assertIn("0 item(s)", err)

    def test_non_utf8_declared_encoding(self):
        text = LEGACY.read_bytes().replace(b'encoding="ISO-8859-1"', b'encoding="windows-1252"')
        articles, _, _ = run(["--file", self.write("cp1252.xml", text)])
        self.assertIn("“great flood”", articles["wp-valleygazette-example-org-news-7"]["content"])


# ---------------------------------------------------------------- command line

class CliTests(TempDirTestCase):
    def test_help_says_where_to_find_each_value(self):
        parser = argparse.ArgumentParser(prog="wordpress-export")
        wordpress_export.add_arguments(parser)
        text = parser.format_help()
        for needle in ("Tools > Export", "All content", "Download Export File", "Settings > General",
                       "wp export", "--include-private", "--include-members-only", "--exclude-meta",
                       "--byline-meta", "No password or API key is needed", "--prefix", "Custom Fields"):
            self.assertIn(needle, text)
        self.assertNotIn("\n", wordpress_export.HELP)

    def test_default_prefix_comes_from_the_site_in_the_file(self):
        self.assertEqual(wordpress_export.default_prefix(parse_args(["--file", MAIN])),
                         "wordpress-export-example-ledger-com")
        self.assertEqual(wordpress_export.default_prefix(parse_args(["--file", LEGACY])),
                         "wordpress-export-valleygazette-example-org-news")
        self.assertEqual(wordpress_export.default_prefix(parse_args(["--file", WPCLI])),
                         "wordpress-export-example-ledger-com")
        self.assertEqual(wordpress_export.default_prefix(parse_args(["--file", MAIN, "--site-url", "ledger.example"])),
                         "wordpress-export-ledger-example")
        # Unreadable or missing: fall back to the file name without WordPress's date suffix, never raise.
        self.assertEqual(wordpress_export.default_prefix(parse_args(["--file", self.tmp / "gaz.WordPress.2023-01-02.xml"])),
                         "wordpress-export-gaz")
        junk = self.write("odd.WordPress.2023-01-02.xml", "<<<not xml")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(wordpress_export.default_prefix(parse_args(["--file", junk])), "wordpress-export-odd")

    def test_site_url_rewrites_links_and_namespaces_ids(self):
        staging = MAIN.read_text(encoding="utf-8").replace("https://www.example-ledger.com",
                                                           "https://staging.example-ledger.com")
        path = self.write("staging.xml", staging)
        articles, _, err = run(["--file", path, "--site-url", "https://www.example-ledger.com/"])
        self.assertIn(wp_id(101), articles)
        self.assertEqual(articles[wp_id(101)]["url"], "https://www.example-ledger.com/2024/03/05/council-budget-qa/")
        self.assertIn("will point to https://www.example-ledger.com", err)
        self.assertEqual(wordpress_export._rewrite_link("https://old.example.com.evil/x", "https://old.example.com",
                                                        "https://new.example.com"),
                         "https://old.example.com.evil/x")


# ---------------------------------------------------------------- review findings

TRIBUNE = FIXTURES / "tribune.WordPress.2024-06-01.xml"
HERALD = FIXTURES / "herald.WordPress.2012-01-02.xml"


def tribune_id(n):
    return f"wp-news-example-tribune-com-{n}"


class ReviewFindingTests(TempDirTestCase):
    def test_unclosed_media_shortcode_does_not_swallow_article_text(self):
        articles, _, _ = run(["--file", TRIBUNE])
        self.assertEqual(articles[tribune_id(301)]["content"],
                         "Intro paragraph.\n\nThe council voted 7-2 to approve the plan.\n\nThe plan takes effect in July.")
        strip = wordpress_export._strip_shortcodes
        text = '<p>A.</p>[youtube https://youtu.be/x]<p>KEEP ME.</p>[youtube]https://youtu.be/y[/youtube]<p>B.</p>'
        self.assertEqual(wordpress_export.html_to_text(strip(text)), "A.\n\nKEEP ME.\n\nB.")
        self.assertEqual(wordpress_export.html_to_text(strip("<p>[Video of the rally] and [Audio: the speech]</p>")),
                         "[Video of the rally] and [Audio: the speech]")

    def test_crafted_shortcodes_and_bylines_are_linear_time(self):
        # Each took seconds at 20 KB and grew quadratically (tens of minutes for a few hundred KB).
        strip, scrub = wordpress_export._strip_shortcodes, wordpress_export._scrub_name
        started = time.monotonic()
        for body in ("[a " + "=" * 300000, "[video " * 100000, "<!-- [a " + "=" * 300000 + " -->"):
            strip(body)
        scrub("a" * 300000 + "@" + "b" * 300000)
        scrub("a@" + "b." * 300000)
        self.assertLess(time.monotonic() - started, 5)
        # Same results as before on ordinary content.
        self.assertEqual(strip('[caption id="1" align="x"]<img/> Mayor speaks[/caption]').strip(), "<img/> Mayor speaks")
        self.assertEqual(strip("[video src=a.mp4][/video]Text").strip(), "Text")
        self.assertEqual(strip("[sic] and [Editor's note: x = y]"), "[sic] and [Editor's note: x = y]")
        self.assertEqual(scrub("Mia Wong (mia@example.com)"), "Mia Wong")
        self.assertEqual(scrub("jd@example.com"), "")
        self.assertEqual(scrub("Kim Lee <kim.lee@news.example.org>"), "Kim Lee")

    def test_iso_8859_1_site_export_is_not_mojibake(self):
        # blog_charset ISO-8859-1: wxr_cdata() utf8_encode()s the text, so the CDATA is UTF-8 in a file
        # that declares ISO-8859-1.
        articles, _, _ = run(["--file", HERALD])
        article = articles["wp-example-herald-org-41"]
        self.assertEqual(article["headline"], "Café owner’s new plan")
        self.assertEqual(article["content"], "The café on Main St. reopens “next week” — finally.")
        self.assertEqual(article["authors"], ["José López"])
        # Re-declared as Windows-1252 it reads the same.
        data = HERALD.read_bytes().replace(b'encoding="ISO-8859-1"', b'encoding="windows-1252"')
        articles, _, _ = run(["--file", self.write("herald-1252.xml", data)])
        self.assertEqual(articles["wp-example-herald-org-41"]["headline"], "Café owner’s new plan")

    def test_jetpack_paid_content_block_is_members_only_for_any_post_type(self):
        # Jetpack only computes _jetpack_memberships_contains_paid_content for 'post' and 'page' on save.
        articles, report, _ = run(["--file", TRIBUNE, "--post-type", "post", "--post-type", "story"])
        self.assertNotIn(tribune_id(302), articles)
        self.assertEqual(report.skipped["has paid or subscriber-only content (Jetpack); use --include-members-only "
                                        "to import"], 1)
        articles, _, _ = run(["--file", TRIBUNE, "--post-type", "story", "--include-members-only"])
        self.assertIn("Paid subscriber analysis.", articles[tribune_id(302)]["content"])

    def test_serialized_jetpack_access_value_is_read_like_jetpack_does(self):
        articles, report, _ = run(["--file", TRIBUNE])
        self.assertNotIn(tribune_id(304), articles)
        self.assertEqual(report.skipped["subscribers or paid members only (Jetpack); use --include-members-only "
                                        "to import"], 1)
        reason = wordpress_export._members_only_reason
        self.assertIsNone(reason({"meta": {wordpress_export.JETPACK_ACCESS: ['s:9:"everybody";']}}, ""))

    def test_logins_and_email_addresses_never_become_bylines(self):
        articles, _, err = run(["--file", TRIBUNE])
        # Legacy (pre-3.0) Co-Authors Plus terms: no cap- prefix, named by user_login.
        self.assertEqual(articles[tribune_id(303)]["authors"], ["Kim Chen"])
        dumped = json.dumps(articles, ensure_ascii=False)
        self.assertNotIn("ssmith", dumped)
        self.assertNotIn("@paper.com", dumped)
        self.assertIn("1 post(s) have Co-Authors Plus bylines whose names aren't in this export", err)
        # A file with no author list: dc:creator is all there is, but an e-mail login is not a name.
        bylines = wordpress_export._Bylines(wordpress_export._options(parse_args(["--file", MAIN])))
        empty = wordpress_export._People()
        self.assertEqual(bylines.resolve({"categories": [], "meta": {}, "dc:creator": "jane.roe@paper.com"}, empty), [])
        self.assertEqual(bylines.resolve({"categories": [], "meta": {}, "dc:creator": "Ruth Baker"}, empty),
                         ["Ruth Baker"])

    def test_byline_field_job_titles_and_structured_values(self):
        articles, _, err = run(["--file", TRIBUNE, "--byline-meta", "byline"])
        self.assertEqual(articles[tribune_id(305)]["authors"], ["Ana Ruiz", "Ben Okafor"])  # not "Staff Writers"
        # Records (PHP-serialized or JSON objects) aren't a list of names: fall back, don't emit fragments.
        self.assertEqual(articles[tribune_id(306)]["authors"], ["Kim Chen"])
        self.assertEqual(articles[tribune_id(307)]["authors"], ["Kim Chen"])
        self.assertEqual(articles[tribune_id(308)]["authors"], ["Mia Wong"])
        self.assertNotIn("example-tribune.com", json.dumps(articles[tribune_id(308)]["authors"]))
        self.assertIn("2 post(s) have a --byline-meta value", err)
        people = wordpress_export._People()
        people.add_user({"wp:author_id": "7", "wp:author_login": "kchen", "wp:author_display_name": "Kim Chen"})
        bylines = wordpress_export._Bylines(wordpress_export._options(parse_args(["--file", TRIBUNE])))
        self.assertEqual(bylines._from_meta(["i:7;"], people), ["Kim Chen"])
        self.assertEqual(bylines._from_meta(['s:8:"Lee Park";'], people), ["Lee Park"])
        self.assertEqual(bylines._from_meta(['O:8:"stdClass":1:{s:4:"name";s:3:"Ann";}'], people), [])
        split = wordpress_export._split_byline
        self.assertEqual(split("Jane Doe | Staff Writer"), ["Jane Doe"])
        self.assertEqual(split("Staff Writer"), ["Staff Writer"])  # nothing else to keep: left for the caller
        self.assertEqual(split("Jane Doe and John Smith, The Associated Press"),
                         ["Jane Doe", "John Smith", "The Associated Press"])

    def test_php_notice_after_the_end_of_the_export_is_ignored(self):
        text = MAIN.read_text(encoding="utf-8")
        tail = ("\n<br />\n<b>Deprecated</b>:  Function utf8_encode() is deprecated in "
                "<b>/var/www/wp-admin/includes/export.php</b> on line <b>31</b><br />\n")
        articles, _, err = run(["--file", self.write("tail.xml", text + tail)])
        self.assertEqual(sorted(articles), [wp_id(n) for n in DEFAULT_IMPORTED])
        self.assertEqual(err.count("after the end of the export"), 1)
        # Same with tiny chunks, so the junk starts in a later chunk than </rss>.
        with mock.patch.object(wordpress_export, "CHUNK", 64):
            articles, _, _ = run(["--file", self.write("tail2.xml", text + "PHP Notice: x & y\n")])
        self.assertEqual(sorted(articles), [wp_id(n) for n in DEFAULT_IMPORTED])

    def test_two_exports_joined_into_one_file_are_refused(self):
        text = MAIN.read_text(encoding="utf-8")
        for chunk in (1 << 20, 64):
            with mock.patch.object(wordpress_export, "CHUNK", chunk):
                msg = run_error(["--file", self.write("joined.xml", text + text)])
            self.assertIn("joined", msg)
            self.assertIn("separately", msg)


class EndToEndTests(TempDirTestCase):
    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(import_archive, "IMPORTERS", ["wordpress_export"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = import_archive.main(["wordpress-export", *[str(a) for a in argv]])
        return code, out.getvalue(), err.getvalue()

    def test_cli_writes_articles_and_returns_zero(self):
        code, out, err = self.main(["--file", MAIN, "--out-dir", self.tmp])
        self.assertEqual(code, 0, err)
        files = sorted(self.tmp.iterdir())
        self.assertEqual([f.name for f in files], ["wordpress-export-example-ledger-com-0001.json"])
        written = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual([a["id"] for a in written], [wp_id(n) for n in DEFAULT_IMPORTED])
        self.assertTrue(all(set(a) == {"id", "headline", "content", "url", "authors", "publish_date"}
                            for a in written))
        by_id = {a["id"]: a for a in written}
        self.assertEqual(by_id[wp_id(102)]["authors"], ["María González", "Sam O’Neil"])
        self.assertIn("Imported 8 article(s) from wordpress-export.", out)
        self.assertIn("1 have no byline", out)
        self.assertIn("1 skipped: draft or pending review (not published)", out)
        self.assertIn("Contents: post 19 (15 published)", err)
        for secret in SECRETS:
            self.assertNotIn(secret, out + err + files[0].read_text(encoding="utf-8"))

    def test_rerun_replaces_and_limit_stops_early(self):
        self.assertEqual(self.main(["--file", MAIN, "--out-dir", self.tmp])[0], 0)
        full = sorted(self.tmp.iterdir())
        full_bytes = full[0].read_bytes()
        # A --limit trial run writes separate -sample files and leaves the full import alone.
        code, out, err = self.main(["--file", MAIN, "--out-dir", self.tmp, "--limit", "2"])
        self.assertEqual(code, 0)
        self.assertIn("-sample", err)
        files = sorted(self.tmp.iterdir())
        self.assertEqual([f.name for f in files], [full[0].name, full[0].name.replace("-0001", "-sample-0001")])
        self.assertEqual(full[0].read_bytes(), full_bytes)
        sample = json.loads(files[1].read_text(encoding="utf-8"))
        self.assertEqual([a["id"] for a in sample], [wp_id(101), wp_id(102)])
        # Re-running the full import replaces its own files in place.
        self.assertEqual(self.main(["--file", MAIN, "--out-dir", self.tmp])[0], 0)
        self.assertEqual(sorted(f.name for f in self.tmp.iterdir()), [f.name for f in files])

    def test_cli_failure_returns_one_and_keeps_earlier_output(self):
        self.assertEqual(self.main(["--file", MAIN, "--out-dir", self.tmp])[0], 0)
        before = {f.name: f.read_bytes() for f in self.tmp.iterdir()}
        data = MAIN.read_bytes()
        cut = self.tmp.parent / f"{self.tmp.name}-cut.xml"
        cut.write_bytes(data[: len(data) // 2])
        self.addCleanup(cut.unlink)
        code, _, err = self.main(["--file", cut, "--out-dir", self.tmp, "--prefix", "wordpress-export-example-ledger-com"])
        self.assertEqual(code, 1)
        self.assertIn("Import failed", err)
        self.assertIn("cut off", err)
        self.assertEqual({f.name: f.read_bytes() for f in self.tmp.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()

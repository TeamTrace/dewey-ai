import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import argparse
import contextlib
import csv
import gzip
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timezone
from unittest import mock

import import_archive
from importers import csv_import
from importers.common import ImporterError, ImportReport, normalize

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "csv_import"


def fx(name: str) -> str:
    return str(FIXTURES / name)


def parse(argv):
    parser = argparse.ArgumentParser(prog="csv")
    csv_import.add_arguments(parser)
    return parser.parse_args(argv)


def run_csv(argv):
    """Run the importer with its own options; returns (raw items, stderr text)."""
    args = parse(argv)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        raws = list(csv_import.iter_articles(args))
    return raws, err.getvalue()


def run_error(argv) -> str:
    """Run the importer and return the ImporterError message it must raise."""
    args = parse(argv)
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            list(csv_import.iter_articles(args))
        except ImporterError as e:
            return str(e)
    raise AssertionError("expected ImporterError")


def normalized(raws):
    report = ImportReport("csv")
    articles = [a for a in (normalize(r, report) for r in raws) if a]
    return articles, report


def by_id(articles):
    return {a["id"]: a for a in articles}


def skip_reasons(raws):
    return Counter(r["_skip"] for r in raws if "_skip" in r)


def url_id(url: str, prefix: str = "csv") -> str:
    return f"{prefix}-{hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]}"


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="dewey-csv-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, content, encoding="utf-8") -> str:
        path = self.tmp / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding=encoding, newline="")
        return str(path)

    def write_rows(self, name, rows, delimiter=",", encoding="utf-8") -> str:
        buf = io.StringIO()
        csv.writer(buf, delimiter=delimiter, lineterminator="\n").writerows(rows)
        return self.write(name, buf.getvalue(), encoding)


# ---------------------------------------------------------------- main CMS export

class ArticlesFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raws, cls.err = run_csv(["--file", fx("articles.csv")])
        cls.articles, cls.report = normalized(cls.raws)
        cls.by_id = by_id(cls.articles)

    def test_mapping_is_detected_and_printed_before_import(self):
        self.assertIn("delimiter ',' (detected)", self.err)
        for line in ("headline   <- 'Title' (column 2)", "content    <- 'Body' (column 3)",
                     "url        <- 'Permalink' (column 4)", "date       <- 'Published' (column 6)",
                     "authors    <- 'Byline' (column 5)", "id         <- 'ID' (column 1)",
                     "status     <- 'Status' (column 7)", "visibility <- 'Visibility' (column 8)",
                     "password   <- (none)", "not used: 'Excerpt'"):
            self.assertIn(line, self.err)
        self.assertLess(self.err.index("Column mapping"), self.err.index("Read 18 row(s)"))

    def test_html_entities_in_titles_are_decoded(self):
        self.assertEqual(self.by_id["csv-101"]["headline"], "Council approves & funds new transit plan")
        self.assertEqual(self.by_id["csv-102"]["headline"], "Tom & Jerry’s \"big\" day")
        self.assertEqual(self.by_id["csv-115"]["headline"], "Mayor “won’t run” again")

    def test_html_content_becomes_text_and_scripts_are_dropped(self):
        self.assertEqual(self.by_id["csv-101"]["content"],
                         "The city council voted 9–8 on Thursday to fund the plan…\n\nRead the full plan.")
        self.assertEqual(self.by_id["csv-115"]["content"], "The mayor said she won’t seek a third term.")

    def test_multiline_plain_text_with_commas_and_quotes(self):
        self.assertEqual(self.by_id["csv-102"]["content"], "First line, with a comma.\n\nHe said \"hello\" twice.")
        self.assertEqual(self.by_id["csv-110"]["content"], "Plain text about the library's centennial.")

    def test_multiple_authors_split_cleaned_and_deduplicated(self):
        self.assertEqual(self.by_id["csv-101"]["authors"], ["Jane Doe", "John Smith"])
        self.assertEqual(self.by_id["csv-111"]["authors"], ["María José García", "Li Wei"])
        self.assertEqual(self.by_id["csv-115"]["authors"], ["Jane Doe", "Ann Lee"])
        self.assertEqual(self.by_id["csv-109"]["authors"], ["Ann Lee"])  # "By Ann Lee"

    def test_missing_byline_stays_empty(self):
        self.assertEqual(self.by_id["csv-102"]["authors"], [])  # empty cell
        self.assertEqual(self.by_id["csv-110"]["authors"], [])  # NULL marker, not an author named NULL
        for a in self.articles:
            self.assertNotIn("NULL", a["authors"])
            self.assertNotIn("Staff", a["authors"])

    def test_time_zones(self):
        self.assertEqual(self.by_id["csv-101"]["publish_date"], "2024-05-01T13:30:00Z")  # -04:00 offset
        self.assertEqual(self.by_id["csv-102"]["publish_date"], "2024-03-10T14:00:00Z")  # naive: UTC default
        self.assertEqual(self.by_id["csv-109"]["publish_date"], "2025-01-15T13:00:00Z")  # RFC 822 -0500
        self.assertEqual(self.by_id["csv-110"]["publish_date"], "2023-09-05T00:00:00Z")  # "Sept. 5, 2023"
        self.assertEqual(self.by_id["csv-111"]["publish_date"], "2024-01-15T08:00:00Z")  # Z
        self.assertEqual(self.by_id["csv-115"]["publish_date"], "2024-04-20T16:45:10Z")  # fraction + offset

    def test_not_public_or_not_published_rows_are_skipped_with_reasons(self):
        reasons = skip_reasons(self.raws)
        text = "\n".join(reasons)
        self.assertIn("status 'draft' is not published", text)
        self.assertIn("status 'private' is not published", text)
        self.assertIn("status 'trash' is not published", text)
        self.assertIn("visibility 'members': members-only", text)
        self.assertIn("visibility 'paid': members-only", text)
        self.assertIn("blank status in column 'Status'", text)
        self.assertIn("dated in the future, probably scheduled (use --include-future", text)
        self.assertIn("--published-values", text)
        for skipped in ("csv-103", "csv-104", "csv-105", "csv-106", "csv-112", "csv-114", "csv-117"):
            self.assertNotIn(skipped, self.by_id)

    def test_status_and_visibility_match_ignoring_case(self):
        self.assertIn("csv-115", self.by_id)  # "Published" / "Public"

    def test_malformed_rows_are_counted_not_invented(self):
        self.assertEqual(skip_reasons(self.raws)["unrecognized date format (set --date-format)"], 1)
        self.assertIn("line 11: 'sometime last week'", self.err)
        self.assertEqual(self.report.skipped["no headline"], 1)
        self.assertEqual(self.report.skipped["no valid http(s) URL"], 1)
        self.assertEqual(self.report.skipped["no article text"], 1)

    def test_ids_are_namespaced_from_the_id_column(self):
        self.assertEqual(sorted(self.by_id), ["csv-101", "csv-102", "csv-109", "csv-110", "csv-111",
                                              "csv-115", "csv-116"])

    def test_content_kind_is_reported(self):
        self.assertIn("content: 7 HTML, 3 plain text", self.err)

    def test_published_values_can_admit_private_and_include_future(self):
        raws, _ = run_csv(["--file", fx("articles.csv"), "--published-values", "publish,published,private",
                           "--include-future"])
        ids = by_id(normalized(raws)[0])
        self.assertIn("csv-104", ids)
        self.assertIn("csv-106", ids)
        self.assertNotIn("csv-103", ids)  # draft still out

    def test_status_column_can_be_ignored_explicitly(self):
        raws, err = run_csv(["--file", fx("articles.csv"), "--status-col", "none", "--visibility-col", "none"])
        ids = by_id(normalized(raws)[0])
        self.assertIn("csv-103", ids)
        self.assertIn("status     <- (none)", err)


# ---------------------------------------------------------------- WordPress database dump

class WordPressTableTests(unittest.TestCase):
    def test_where_status_password_and_gmt_column(self):
        raws, err = run_csv(["--file", fx("wp_posts.tsv"), "--where", "post_type=post"])
        self.assertIn("delimiter tab (from the .tsv name)", err)
        self.assertIn("date       <- 'post_date_gmt' (column 4)  [naive times read as UTC", err)
        self.assertIn("url        <- 'permalink' (column 12)", err)
        self.assertIn("id         <- 'ID' (column 1)", err)
        self.assertIn("authors    <- (none)  [no byline column", err)
        reasons = skip_reasons(raws)
        self.assertEqual(reasons["post_type is 'revision', not 'post' (--where)"], 1)
        self.assertEqual(reasons["post_type is 'page', not 'post' (--where)"], 1)
        self.assertEqual(reasons["post_type is 'nav_menu_item', not 'post' (--where)"], 1)
        self.assertEqual(reasons["password-protected (its text isn't public)"], 1)
        for status in ("draft", "private", "future"):
            self.assertEqual(reasons[f"status '{status}' is not published (list it in --published-values "
                                     "to import it)"], 1)
        articles, report = normalized(raws)
        ids = by_id(articles)
        self.assertEqual(sorted(ids), ["csv-501", "csv-508"])
        a = ids["csv-501"]
        self.assertEqual(a["headline"], "Primary day – what to know")
        self.assertEqual(a["content"], "Voters head to the polls on Tuesday.\n\nPolls close at 8 p.m. "
                                       "See where to vote.")
        self.assertEqual(a["publish_date"], "2024-05-01T13:30:00Z")
        self.assertEqual(a["authors"], [])
        self.assertEqual(ids["csv-508"]["headline"], "Results: \"too close to call\"")
        self.assertEqual(ids["csv-508"]["content"], "Plain paragraph one.\n\nPlain paragraph two, sponsored by AT&T.")

    def test_gmt_column_stays_utc_even_with_a_timezone(self):
        raws, _ = run_csv(["--file", fx("wp_posts.tsv"), "--where", "post_type=post",
                           "--timezone", "America/New_York"])
        self.assertEqual(by_id(normalized(raws)[0])["csv-501"]["publish_date"], "2024-05-01T13:30:00Z")

    def test_local_post_date_with_timezone(self):
        raws, _ = run_csv(["--file", fx("wp_posts.tsv"), "--where", "post_type=post", "--date-col", "post_date",
                           "--timezone", "America/New_York"])
        ids = by_id(normalized(raws)[0])
        self.assertEqual(ids["csv-501"]["publish_date"], "2024-05-01T13:30:00Z")  # EDT, -4
        self.assertEqual(ids["csv-508"]["publish_date"], "2023-11-08T01:15:00Z")  # EST, -5

    def test_numeric_author_ids_are_not_bylines(self):
        raws, err = run_csv(["--file", fx("wp_posts.tsv"), "--where", "post_type=post",
                             "--authors-col", "post_author"])
        for a in normalized(raws)[0]:
            self.assertEqual(a["authors"], [])
        self.assertIn("Ignored 2 byline value(s) that are numbers (user ids, not names), e.g. '3'", err)

    def test_where_on_unknown_column(self):
        msg = run_error(["--file", fx("wp_posts.tsv"), "--where", "kind=post"])
        self.assertIn("--where 'kind'", msg)
        self.assertIn("'post_type'", msg)


# ---------------------------------------------------------------- Excel exports

class ExcelTests(TempDirTest):
    def test_cp1252_file_needs_encoding_flag(self):
        msg = run_error(["--file", fx("excel_eu_cp1252.csv"), "--date-format", "%d/%m/%Y %H:%M"])
        self.assertIn("isn't valid utf-8-sig text", msg)
        self.assertIn("--encoding cp1252", msg)

    def test_day_first_dates_are_never_guessed(self):
        msg = run_error(["--file", fx("excel_eu_cp1252.csv"), "--encoding", "cp1252"])
        self.assertIn("won't guess", msg)
        self.assertIn("--date-format '%m/%d/%Y %H:%M'", msg)
        self.assertIn("--date-format '%d/%m/%Y %H:%M'", msg)
        self.assertIn("'25/12/2023 18:30' only fits day first", msg)

    def test_day_first_with_date_format_semicolons_and_cp1252(self):
        raws, err = run_csv(["--file", fx("excel_eu_cp1252.csv"), "--encoding", "cp1252",
                             "--date-format", "%d/%m/%Y %H:%M", "--timezone", "Europe/Berlin"])
        self.assertIn("delimiter ';' (detected)", err)
        articles, _ = normalized(raws)
        self.assertEqual(len(articles), 3)
        cafe, concert, budget = articles
        self.assertEqual(cafe["headline"], "Café-Besitzer klagt über Mieten")
        self.assertEqual(cafe["authors"], ["Jürgen Müller", "Anna Weiß"])
        self.assertEqual(cafe["publish_date"], "2023-12-25T17:30:00Z")  # CET, +1
        self.assertEqual(concert["publish_date"], "2024-01-05T08:15:00Z")  # 5 January, not May 1
        self.assertIn("ab 25 €", concert["content"])
        self.assertEqual(budget["authors"], [])

    def test_us_excel_bom_crlf_and_ambiguity_hint(self):
        msg = run_error(["--file", fx("excel_us_bom.csv")])
        self.assertIn("'5/1/2024 1:05 PM'", msg)
        self.assertIn("--date-format '%m/%d/%Y %I:%M %p'", msg)
        self.assertIn("'1/15/2024 9:00 AM' only fits month first", msg)

    def test_us_excel_with_format_and_dst_aware_timezone(self):
        raws, err = run_csv(["--file", fx("excel_us_bom.csv"), "--date-format", "%m/%d/%Y %I:%M %p",
                             "--timezone", "America/New_York"])
        self.assertIn("headline   <- 'Headline' (column 2)", err)  # BOM stripped from the header row
        self.assertIn("id         <- 'Article ID' (column 1)", err)
        self.assertIn("authors    <- 'Author(s)' (column 5)", err)
        ids = by_id(normalized(raws)[0])
        storm = ids["csv-20240501001"]
        self.assertEqual(storm["publish_date"], "2024-05-01T17:05:00Z")  # EDT
        self.assertEqual(storm["authors"], ["Pat O'Neil, Jr.", "Sam Rivera"])
        self.assertEqual(storm["headline"], "Storm knocks out power to 10,000")
        self.assertEqual(ids["csv-20240115002"]["publish_date"], "2024-01-15T14:00:00Z")  # EST
        self.assertEqual(ids["csv-20240115002"]["content"], "Plain text body.\nSecond line.")
        self.assertEqual(ids["csv-20231231003"]["publish_date"], "2024-01-01T04:59:00Z")  # crosses midnight
        self.assertEqual(ids["csv-20231231003"]["authors"], [])

    def test_fixed_offset_negative_needs_equals(self):
        raws, _ = run_csv(["--file", fx("excel_us_bom.csv"), "--date-format", "%m/%d/%Y %I:%M %p",
                           "--timezone-offset=-05:00"])
        ids = by_id(normalized(raws)[0])
        self.assertEqual(ids["csv-20240501001"]["publish_date"], "2024-05-01T18:05:00Z")
        raws, _ = run_csv(["--file", fx("excel_us_bom.csv"), "--date-format", "%m/%d/%Y %I:%M %p",
                           "--timezone-offset", "UTC+5:30"])
        self.assertEqual(by_id(normalized(raws)[0])["csv-20240501001"]["publish_date"], "2024-05-01T07:35:00Z")

    def test_excel_sep_line(self):
        path = self.write("sep.csv", "sep=;\r\nTitle;Body;URL;Date\r\n"
                                     "Hello;<p>World</p>;https://news.example.com/h/;2024-01-02\r\n")
        raws, err = run_csv(["--file", path])
        self.assertIn("delimiter ';' (from the file's sep= line)", err)
        a, = normalized(raws)[0]
        self.assertEqual((a["headline"], a["content"]), ("Hello", "World"))

    def test_scientific_notation_ids_are_refused(self):
        path = self.write_rows("sci.csv", [["id", "title", "body", "url", "date"],
                                           ["1.23457E+17", "A", "B", "https://news.example.com/a/", "2024-01-01"]])
        msg = run_error(["--file", path])
        self.assertIn("scientific notation", msg)
        self.assertIn("--id-col none", msg)
        raws, _ = run_csv(["--file", path, "--id-col", "none"])
        self.assertEqual(normalized(raws)[0][0]["id"], url_id("https://news.example.com/a/"))

    def test_xlsx_workbook_is_refused_with_hint(self):
        path = self.write("articles.xlsx", b"PK\x03\x04\x14\x00\x06\x00" + b"\x00" * 64)
        msg = run_error(["--file", path])
        self.assertIn("Excel workbook", msg)
        self.assertIn("CSV UTF-8", msg)

    def test_utf16_unicode_text_is_detected_by_bom(self):
        text = "Title\tBody\tLink\tDate\r\nZürich news\t<p>Grüezi</p>\thttps://news.example.ch/z/\t2024-03-01T10:00:00+01:00\r\n"
        path = self.write("unicode.txt", b"\xff\xfe" + text.encode("utf-16-le"))
        raws, err = run_csv(["--file", path])
        self.assertIn("encoding utf-16 (UTF-16 byte-order mark)", err)
        self.assertIn("delimiter tab", err)
        a, = normalized(raws)[0]
        self.assertEqual((a["headline"], a["publish_date"]), ("Zürich news", "2024-03-01T09:00:00Z"))

    def test_excel_truncated_cells_are_reported(self):
        path = self.write_rows("cut.csv", [["title", "body", "url", "date"],
                                           ["Long", "x" * 32767, "https://news.example.com/l/", "2024-01-01"]])
        _, err = run_csv(["--file", path])
        self.assertIn("exactly 32,767 characters", err)

    def test_excel_serial_dates_get_a_hint(self):
        path = self.write_rows("serial.csv", [["title", "body", "url", "date"],
                                              ["A", "B", "https://news.example.com/a/", "45413"],
                                              ["C", "D", "https://news.example.com/c/", "45414.5"]])
        msg = run_error(["--file", path])
        self.assertIn("Excel serial day numbers", msg)


# ---------------------------------------------------------------- explicit mapping (Drupal view export)

class DrupalViewTests(unittest.TestCase):
    def test_required_columns_missing_lists_headers(self):
        msg = run_error(["--file", fx("drupal_nodes.csv")])
        self.assertIn("URL: looked for url, permalink", msg)
        self.assertIn("--url-col COLUMN", msg)
        self.assertIn("publish date: looked for", msg)
        self.assertIn("--date-col COLUMN", msg)
        self.assertIn("1 'nid', 2 'title', 3 'body_value'", msg)
        self.assertIn("9 'path'", msg)

    def test_relative_paths_need_base_url(self):
        msg = run_error(["--file", fx("drupal_nodes.csv"), "--url-col", "path", "--date-col", "created"])
        self.assertIn("--base-url", msg)
        self.assertIn("'/news/2024/05/01/budget-hearing'", msg)

    def test_explicit_mapping_epoch_dates_numeric_status_and_base_url(self):
        raws, err = run_csv(["--file", fx("drupal_nodes.csv"), "--url-col", "path", "--date-col", "Created",
                             "--authors-col", "field_byline", "--authors-sep", ",",
                             "--base-url", "https://www.example.org/"])
        self.assertIn("status     <- 'status' (column 7)", err)
        self.assertIn("content    <- 'body_value' (column 3)", err)
        self.assertEqual(skip_reasons(raws)["status '0' is not published (list it in --published-values "
                                            "to import it)"], 1)
        ids = by_id(normalized(raws)[0])
        self.assertEqual(sorted(ids), ["csv-1201", "csv-1203"])
        hearing = ids["csv-1201"]
        self.assertEqual(hearing["url"], "https://www.example.org/news/2024/05/01/budget-hearing")
        self.assertEqual(hearing["publish_date"], "2024-05-01T13:00:00Z")
        self.assertEqual(hearing["headline"], "Budget hearing set for Monday & Tuesday")
        self.assertEqual(ids["csv-1203"]["authors"], ["Ann Lee", "Sam Rivera"])
        self.assertEqual(ids["csv-1203"]["publish_date"], "2023-11-14T22:13:20Z")

    def test_columns_by_number_and_id_prefix(self):
        raws, _ = run_csv(["--file", fx("drupal_nodes.csv"), "--url-col", "9", "--date-col", "#5",
                           "--base-url", "https://www.example.org", "--id-prefix", "drupal-example"])
        self.assertEqual(sorted(by_id(normalized(raws)[0])), ["drupal-example-1201", "drupal-example-1203"])


class ColumnOptionTests(TempDirTest):
    def test_missing_content_column(self):
        msg = run_error(["--file", fx("missing_content.csv")])
        self.assertIn("article text: looked for content, body, text", msg)
        self.assertIn("1 'Title', 2 'Summary', 3 'Link', 4 'Date'", msg)

    def test_excerpt_column_used_only_when_named(self):
        raws, _ = run_csv(["--file", fx("missing_content.csv"), "--content-col", "summary"])
        self.assertEqual(normalized(raws)[0][0]["content"], "Just a summary")

    def test_unknown_column_name(self):
        msg = run_error(["--file", fx("articles.csv"), "--headline-col", "Hed"])
        self.assertIn("--headline-col 'Hed': there is no column with that name", msg)
        self.assertIn("2 'Title'", msg)

    def test_column_number_out_of_range(self):
        msg = run_error(["--file", fx("articles.csv"), "--headline-col", "42"])
        self.assertIn("the file has 9 column(s)", msg)

    def test_required_column_cannot_be_none(self):
        msg = run_error(["--file", fx("articles.csv"), "--url-col", "none"])
        self.assertIn("--url-col can't be 'none'", msg)

    def test_no_header_needs_numbers(self):
        msg = run_error(["--file", fx("no_header.csv"), "--no-header"])
        self.assertIn("--headline-col N", msg)

    def test_no_header_with_numbers(self):
        raws, err = run_csv(["--file", fx("no_header.csv"), "--no-header", "--url-col", "1", "--headline-col", "2",
                             "--content-col", "3", "--date-col", "4", "--authors-col", "5"])
        a, b = normalized(raws)[0]
        self.assertEqual((a["headline"], a["content"], a["authors"]), ("Alpha headline", "Alpha body", ["Jane Doe"]))
        self.assertEqual((b["headline"], b["authors"], b["publish_date"]), ("Beta headline", [], "2024-02-02T00:00:00Z"))
        self.assertEqual(a["id"], url_id("https://news.example.com/a/"))

    def test_header_names_match_loosely_and_duplicates_warn(self):
        path = self.write_rows("loose.csv", [["Headline", "Article Body", "URL", "Publish Date", "Title", "Title"],
                                             ["Real hed", "<p>Body</p>", "https://news.example.com/x/",
                                              "2024-01-01", "t1", "t2"]])
        raws, err = run_csv(["--file", path, "--content-col", "article_body"])
        self.assertIn("date       <- 'Publish Date' (column 4)", err)
        a, = normalized(raws)[0]
        self.assertEqual(a["headline"], "Real hed")
        path = self.write_rows("dup.csv", [["Title", "Title", "Body", "URL", "Date"],
                                           ["first", "second", "b", "https://news.example.com/y/", "2024-01-01"]])
        raws, err = run_csv(["--file", path])
        self.assertIn("2 columns are named 'Title'", err)
        self.assertEqual(normalized(raws)[0][0]["headline"], "first")

    def test_url_id_is_stable_sha1(self):
        path = self.write_rows("noid.csv", [["title", "body", "url", "date"],
                                            ["A", "B", "https://news.example.com/stable/", "2024-01-01"]])
        first = normalized(run_csv(["--file", path])[0])[0][0]["id"]
        second = normalized(run_csv(["--file", path])[0])[0][0]["id"]
        self.assertEqual(first, second)
        self.assertEqual(first, "csv-" + hashlib.sha1(b"https://news.example.com/stable/").hexdigest()[:16])

    def test_blank_id_is_skipped_with_reason(self):
        path = self.write_rows("blankid.csv", [["id", "title", "body", "url", "date"],
                                               ["", "A", "B", "https://news.example.com/a/", "2024-01-01"],
                                               ["7", "C", "D", "https://news.example.com/c/", "2024-01-01"]])
        raws, _ = run_csv(["--file", path])
        self.assertIn("blank id in column 'id' (--id-col none uses URL-based ids)", skip_reasons(raws))

    def test_unsafe_id_values_are_hashed(self):
        path = self.write_rows("guid.csv", [["guid", "title", "body", "url", "date"],
                                            ["https://news.example.com/?p=9", "A", "B", "https://news.example.com/a/",
                                             "2024-01-01"]])
        a, = normalized(run_csv(["--file", path])[0])[0]
        self.assertEqual(a["id"], "csv-" + hashlib.sha1(b"https://news.example.com/?p=9").hexdigest()[:16])

    def test_url_column_without_urls(self):
        path = self.write_rows("slugs.csv", [["title", "body", "link", "date"],
                                             ["A", "B", "my-story-slug", "2024-01-01"]])
        msg = run_error(["--file", path])
        self.assertIn("doesn't hold web addresses", msg)

    def test_bad_option_values(self):
        self.assertIn("no such file", run_error(["--file", str(self.tmp / "missing.csv")]))
        self.assertIn("is a folder", run_error(["--file", str(self.tmp)]))
        self.assertIn("isn't a known text encoding", run_error(["--file", fx("articles.csv"), "--encoding", "klingon"]))
        self.assertIn("give one character", run_error(["--file", fx("articles.csv"), "--delimiter", ";;"]))
        self.assertIn("--base-url", run_error(["--file", fx("articles.csv"), "--base-url", "example.com"]))
        self.assertIn("--id-prefix", run_error(["--file", fx("articles.csv"), "--id-prefix", "bad prefix!"]))
        self.assertIn("COLUMN=VALUE", run_error(["--file", fx("articles.csv"), "--where", "post_type"]))
        self.assertIn("Unknown time zone", run_error(["--file", fx("articles.csv"), "--timezone", "Mars/Olympus"]))
        self.assertIn("+HH:MM", run_error(["--file", fx("articles.csv"), "--timezone-offset", "+25:00"]))

    def test_timezone_flags_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse(["--file", fx("articles.csv"), "--assume-utc", "--timezone", "UTC"])

    def test_empty_file(self):
        self.assertIn("has no rows", run_error(["--file", self.write("empty.csv", "")]))


# ---------------------------------------------------------------- dates, bylines, content

class DateParserTests(unittest.TestCase):
    def parse(self, value, formats=(), tz=timezone.utc):
        dt = csv_import._DateParser(list(formats), tz, False).parse(value)
        return dt and dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_iso_and_database_forms(self):
        self.assertEqual(self.parse("2024-05-01T13:00:00Z"), "2024-05-01T13:00:00Z")
        self.assertEqual(self.parse("2024-05-01 13:00:00+00"), "2024-05-01T13:00:00Z")  # PostgreSQL timestamptz
        self.assertEqual(self.parse("2024-05-01 13:00:00 UTC"), "2024-05-01T13:00:00Z")  # BigQuery
        self.assertEqual(self.parse("2024-05-01T09:00:00.123-04:00"), "2024-05-01T13:00:00Z")
        self.assertEqual(self.parse("20240501"), "2024-05-01T00:00:00Z")
        self.assertEqual(self.parse("2024-5-1 9:05"), "2024-05-01T09:05:00Z")
        self.assertEqual(self.parse("2024/05/01 13:00"), "2024-05-01T13:00:00Z")

    def test_unix_timestamps(self):
        self.assertEqual(self.parse("1714568400"), "2024-05-01T13:00:00Z")
        self.assertEqual(self.parse("1714568400000"), "2024-05-01T13:00:00Z")
        self.assertEqual(self.parse("1714568400.5"), "2024-05-01T13:00:00Z")
        self.assertEqual(self.parse("1714568400", formats=["unix"]), "2024-05-01T13:00:00Z")
        self.assertEqual(self.parse("1714568400000", formats=["unix-ms"]), "2024-05-01T13:00:00Z")

    def test_month_names_and_ap_style(self):
        self.assertEqual(self.parse("May 1, 2024"), "2024-05-01T00:00:00Z")
        self.assertEqual(self.parse("1 May 2024"), "2024-05-01T00:00:00Z")
        self.assertEqual(self.parse("Sept. 5, 2023, 1:05 p.m."), "2023-09-05T13:05:00Z")
        self.assertEqual(self.parse("Wednesday, May 1st, 2024 9:30 AM"), "2024-05-01T09:30:00Z")
        self.assertEqual(self.parse("01-May-2024"), "2024-05-01T00:00:00Z")
        self.assertEqual(self.parse("Wed, 01 May 2024 13:00:00 GMT"), "2024-05-01T13:00:00Z")

    def test_ambiguous_numeric_dates_raise_without_format(self):
        for value in ("05/01/2024", "13/05/2024", "1.5.24", "01-05-2024 10:00"):
            with self.assertRaises(csv_import._AmbiguousDate, msg=value):
                self.parse(value)

    def test_explicit_formats_decide_and_others_stay_unambiguous(self):
        self.assertEqual(self.parse("05/01/2024", ["%d/%m/%Y"]), "2024-01-05T00:00:00Z")
        self.assertEqual(self.parse("05/01/2024", ["%m/%d/%Y"]), "2024-05-01T00:00:00Z")
        self.assertEqual(self.parse("2024-05-01", ["%d/%m/%Y"]), "2024-05-01T00:00:00Z")
        self.assertIsNone(self.parse("05/01/24", ["%d/%m/%Y"]))  # unmatched: skipped, not guessed

    def test_garbage_is_none(self):
        for value in ("sometime last week", "2024", "TBD", "99/99/9999x"):
            self.assertIsNone(self.parse(value), value)

    def test_format_suggestions(self):
        m = csv_import._NUMERIC_DM.match("5/1/2024 1:05:09 PM")
        self.assertEqual(csv_import._suggest_formats(m), ("%m/%d/%Y %I:%M:%S %p", "%d/%m/%Y %I:%M:%S %p"))
        m = csv_import._NUMERIC_DM.match("05.01.24")
        self.assertEqual(csv_import._suggest_formats(m), ("%m.%d.%y", "%d.%m.%y"))


class BylineTests(unittest.TestCase):
    def split(self, value, seps=(";",)):
        return csv_import._split_authors(value, list(seps))

    def test_semicolon_default(self):
        self.assertEqual(self.split("Jane Doe; John Smith"), ["Jane Doe", "John Smith"])
        self.assertEqual(self.split("Smith, Jr., John"), ["Smith, Jr., John"])  # ';' leaves commas alone

    def test_comma_keeps_suffixes_attached(self):
        self.assertEqual(self.split("Pat O'Neil, Jr., Sam Rivera, Lee Kim III", [","]),
                         ["Pat O'Neil, Jr.", "Sam Rivera", "Lee Kim III"])
        self.assertEqual(self.split("Martin Luther King, Jr", [","]), ["Martin Luther King, Jr"])

    def test_and_separator_and_by_prefix(self):
        self.assertEqual(self.split("By Jane Doe, John Smith and Ann Lee", [",", " and "]),
                         ["Jane Doe", "John Smith", "Ann Lee"])
        self.assertEqual(self.split("Byron Hall"), ["Byron Hall"])

    def test_json_list_and_entities(self):
        self.assertEqual(self.split('["Jane Doe", {"name": "Ann Lee"}]'), ["Jane Doe", "Ann Lee"])
        self.assertEqual(self.split("Ren&eacute;e O&#39;Connor; Tom &amp; Jerry Inc"),
                         ["Renée O'Connor", "Tom & Jerry Inc"])

    def test_line_breaks_in_a_cell_separate_names(self):
        self.assertEqual(self.split("Jane Doe\r\nBy John Smith\n"), ["Jane Doe", "John Smith"])

    def test_null_markers(self):
        for value in ("", "NULL", "\\N", "N/A", "  "):
            self.assertEqual(self.split(value), [], value)

    def test_emails_are_not_bylines(self):
        path_rows = [["title", "body", "url", "date", "author"],
                     ["A", "B", "https://news.example.com/a/", "2024-01-01", "jane@example.com; Jane Doe"]]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "e.csv"
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(path_rows)
            raws, err = run_csv(["--file", str(path)])
        self.assertEqual(normalized(raws)[0][0]["authors"], ["Jane Doe"])
        self.assertIn("e-mail addresses", err)


class ContentModeTests(TempDirTest):
    def rows(self, body):
        return self.write_rows("c.csv", [["title", "body", "url", "date"],
                                         ["T", body, "https://news.example.com/t/", "2024-01-01"]])

    def test_plain_text_with_angle_brackets_stays_text(self):
        raws, _ = run_csv(["--file", self.rows("If x < 3 and y > 2,\nthen stop.")])
        self.assertEqual(raws[0]["content"], "If x < 3 and y > 2,\nthen stop.")
        self.assertEqual(normalized(raws)[0][0]["content"], "If x < 3 and y > 2,\nthen stop.")

    def test_force_text_and_force_html(self):
        raws, _ = run_csv(["--file", self.rows("Use <b>bold</b>."), "--content-is-text"])
        self.assertEqual(normalized(raws)[0][0]["content"], "Use <b>bold</b>.")
        raws, _ = run_csv(["--file", self.rows("Line one\r\nLine two"), "--content-is-html"])
        self.assertIn("content_html", raws[0])

    def test_entities_alone_mean_html(self):
        raws, _ = run_csv(["--file", self.rows("Fish &amp; chips")])
        self.assertEqual(normalized(raws)[0][0]["content"], "Fish & chips")


# ---------------------------------------------------------------- delimiters, size, compression

class FileHandlingTests(TempDirTest):
    def test_semicolon_and_tab_are_sniffed(self):
        path = self.write("semi.csv", 'title;body;url;date\n"Hello, world";"a, b; c";https://news.example.com/s/;2024-01-01\n')
        raws, err = run_csv(["--file", path])
        self.assertIn("delimiter ';'", err)
        self.assertEqual(normalized(raws)[0][0]["headline"], "Hello, world")
        path = self.write("tabs.txt", "title\tbody\turl\tdate\nHi, there\tText, more\thttps://news.example.com/t/\t2024-01-01\n")
        raws, err = run_csv(["--file", path])
        self.assertIn("delimiter tab", err)
        self.assertEqual(normalized(raws)[0][0]["content"], "Text, more")

    def test_entity_heavy_html_does_not_fool_the_sniffer(self):
        body = "<p>" + "Caf&eacute;&nbsp;&mdash;&nbsp;r&eacute;sum&eacute;; na&iuml;ve; " * 200 + "</p>"
        path = self.write_rows("entities.csv", [["title", "body", "url", "date"]] +
                               [[f"T{i}", body, f"https://news.example.com/{i}/", "2024-01-01"] for i in range(5)])
        raws, err = run_csv(["--file", path])
        self.assertIn("delimiter ','", err)
        self.assertEqual(len(normalized(raws)[0]), 5)

    def test_relative_urls_in_a_mixed_column_are_counted(self):
        path = self.write_rows("mixed.csv", [["title", "body", "url", "date"],
                                             ["A", "B", "https://news.example.com/a/", "2024-01-01"],
                                             ["C", "D", "/news/c/", "2024-01-01"]])
        raws, err = run_csv(["--file", path])
        self.assertEqual(len(normalized(raws)[0]), 1)
        self.assertIn("1 row(s) have a path instead of a full URL", err)

    def test_boolean_published_column_gets_a_hint(self):
        path = self.write_rows("flag.csv", [["title", "body", "url", "published", "date"],
                                            ["A", "B", "https://news.example.com/a/", "true", "2024-01-01"],
                                            ["C", "D", "https://news.example.com/c/", "false", "2024-01-02"]])
        msg = run_error(["--file", path])
        self.assertIn("--status-col 'published'", msg)
        raws, _ = run_csv(["--file", path, "--status-col", "published", "--date-col", "date"])
        self.assertEqual([a["headline"] for a in normalized(raws)[0]], ["A"])
        self.assertIn("status 'false' is not published", "\n".join(skip_reasons(raws)))

    def test_explicit_delimiter(self):
        path = self.write("pipe.csv", "title|body|url|date\nA|B|https://news.example.com/p/|2024-01-01\n")
        raws, err = run_csv(["--file", path, "--delimiter", "pipe"])
        self.assertIn("delimiter '|' (given)", err)
        self.assertEqual(len(normalized(raws)[0]), 1)

    def test_very_large_fields_and_limit_is_restored(self):
        before = csv.field_size_limit()
        body = "<p>" + ("All the news that fits. " * 20000) + "</p>"  # ~480 KB, over the 128 KB default
        path = self.write_rows("big.csv", [["title", "body", "url", "date"],
                                           ["Big", body, "https://news.example.com/big/", "2024-01-01"]])
        raws, _ = run_csv(["--file", path])
        self.assertGreater(len(normalized(raws)[0][0]["content"]), 400_000)
        self.assertEqual(csv.field_size_limit(), before)

    def test_gzipped_file(self):
        path = self.tmp / "articles.csv.gz"
        with gzip.open(path, "wb") as g:
            g.write((FIXTURES / "articles.csv").read_bytes())
        raws, err = run_csv(["--file", str(path)])
        self.assertIn("gzipped", err)
        self.assertEqual(len(normalized(raws)[0]), 7)

    def test_ragged_rows_are_reported(self):
        path = self.write("ragged.csv", "title,body,url,date\nA,B,https://news.example.com/a/,2024-01-01,extra\n"
                                        "C,D,https://news.example.com/c/,2024-01-02\n")
        _, err = run_csv(["--file", path])
        self.assertIn("1 row(s) have a different number of columns than the header (first at line 2)", err)

    def test_mid_file_ambiguous_date_aborts(self):
        rows = [["title", "body", "url", "date"]]
        rows += [[f"T{i}", "B", f"https://news.example.com/{i}/", "2024-01-01"] for i in range(600)]
        rows.append(["Late", "B", "https://news.example.com/late/", "03/04/2024"])
        path = self.write_rows("late.csv", rows)
        msg = run_error(["--file", path])
        self.assertIn("'03/04/2024' (line 602)", msg)

    def test_many_rows_stream_with_progress(self):
        rows = [["id", "title", "body", "url", "date", "author"]]
        rows += [[str(i), f"Story {i}", f"<p>Body {i}</p>", f"https://news.example.com/{i}/",
                  f"2024-01-{1 + i % 28:02d}T12:00:00Z", "Jane Doe" if i % 2 else ""] for i in range(2500)]
        path = self.write_rows("many.csv", rows)
        with mock.patch.object(csv_import, "PROGRESS_EVERY", 1000):
            raws, err = run_csv(["--file", path])
        self.assertIn("1,000 rows read", err)
        self.assertIn("2,000 rows read", err)
        self.assertEqual(len(normalized(raws)[0]), 2500)


# ---------------------------------------------------------------- end to end through import_archive

class EndToEndTests(TempDirTest):
    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = import_archive.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_publisher_option_names_the_publisher_on_every_article(self):
        # For a source that isn't the newsroom's own: without it the indexer labels the articles
        # with NEWSROOM_NAME, presenting a partner's stories as the newsroom's (#14).
        out_dir = self.tmp / "partner"
        code, out, err = self.main(["csv", "--file", fx("articles.csv"), "--out-dir", str(out_dir),
                                    "--publisher", " Partner   Daily "])
        self.assertEqual(code, 0, err)
        articles = json.loads(next(out_dir.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual({a["publisher"] for a in articles}, {"Partner Daily"})
        self.assertIn("Publisher on every article: Partner Daily", out)

    def test_without_the_publisher_option_articles_name_none(self):
        out_dir = self.tmp / "own"
        code, out, err = self.main(["csv", "--file", fx("articles.csv"), "--out-dir", str(out_dir)])
        self.assertEqual(code, 0, err)
        articles = json.loads(next(out_dir.glob("*.json")).read_text(encoding="utf-8"))
        self.assertTrue(all("publisher" not in a for a in articles))
        self.assertNotIn("Publisher on every article", out)

    def test_every_importer_has_the_publisher_option(self):
        parser, modules = import_archive.build_parser()
        subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        self.assertEqual(set(subparsers.choices), set(modules))
        for name, sub in subparsers.choices.items():
            self.assertIn("--publisher NAME", sub.format_help(), name)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["csv", "--file", "x.csv", "--publisher", "  "])

    def test_import_writes_json(self):
        out_dir = self.tmp / "data"
        code, out, err = self.main(["csv", "--file", fx("articles.csv"), "--out-dir", str(out_dir)])
        self.assertEqual(code, 0, err)
        files = sorted(out_dir.glob("*.json"))
        self.assertEqual([f.name for f in files], ["csv-articles-0001.json"])
        articles = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual([a["id"] for a in articles], ["csv-101", "csv-102", "csv-109", "csv-110", "csv-111",
                                                       "csv-115"])
        first = articles[0]
        self.assertEqual(set(first), {"id", "headline", "content", "url", "authors", "publish_date"})
        self.assertEqual(first["publish_date"], "2024-05-01T13:30:00Z")
        self.assertIn("Imported 6 article(s) from csv.", out)
        self.assertIn("1 duplicate(s) skipped", out)
        self.assertIn("2 have no byline", out)
        self.assertIn("skipped: status 'draft' is not published", out)
        self.assertIn("Column mapping", err)

    def test_reimport_replaces_with_same_ids(self):
        out_dir = self.tmp / "data"
        argv = ["csv", "--file", fx("wp_posts.tsv"), "--where", "post_type=post", "--out-dir", str(out_dir),
                "--prefix", "wp-db", "--id-prefix", "wp-example.com"]
        self.assertEqual(self.main(argv)[0], 0)
        self.assertEqual(self.main(argv)[0], 0)
        files = sorted(out_dir.glob("*.json"))
        self.assertEqual([f.name for f in files], ["wp-db-0001.json"])
        ids = [a["id"] for a in json.loads(files[0].read_text(encoding="utf-8"))]
        self.assertEqual(ids, ["wp-example.com-501", "wp-example.com-508"])

    def test_many_rows_split_into_files_and_limit(self):
        rows = [["title", "body", "url", "date"]]
        rows += [[f"Story {i}", f"Body {i}", f"https://news.example.com/{i}/", "2024-01-01"] for i in range(2500)]
        path = self.write_rows("many.csv", rows)
        code, out, _ = self.main(["csv", "--file", path, "--out-dir", str(self.tmp / "all")])
        self.assertEqual(code, 0)
        self.assertEqual(len(list((self.tmp / "all").glob("csv-many-*.json"))), 3)
        code, out, err = self.main(["csv", "--file", path, "--out-dir", str(self.tmp / "few"), "--limit", "5"])
        self.assertEqual(code, 0)
        self.assertIn("Imported 5 article(s)", out)
        self.assertIn("Read 5 row(s)", err)

    def test_failure_returns_1_and_leaves_output_untouched(self):
        out_dir = self.tmp / "data"
        out_dir.mkdir()
        keep = out_dir / "csv-excel_us_bom-0001.json"
        keep.write_text("[]", encoding="utf-8")
        code, _, err = self.main(["csv", "--file", fx("excel_us_bom.csv"), "--out-dir", str(out_dir)])
        self.assertEqual(code, 1)
        self.assertIn("Import failed:", err)
        self.assertIn("--date-format", err)
        self.assertEqual(keep.read_text(encoding="utf-8"), "[]")
        self.assertEqual(sorted(p.name for p in out_dir.iterdir()), ["csv-excel_us_bom-0001.json"])

    def test_help_explains_where_values_come_from(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            import_archive.main(["csv", "--help"])
        text = out.getvalue()
        for phrase in ("Where to get the file", "CSV UTF-8", "File > Download", "\\copy", "--date-format",
                       "--timezone", "cp1252", "Secrets: none", "post_type=post", "(default: utf-8-sig"):
            self.assertIn(phrase, text)

    def test_default_prefix(self):
        self.assertEqual(csv_import.default_prefix(argparse.Namespace(file=pathlib.Path("x/Articles 2024.csv.gz"))),
                         "csv-Articles 2024")
        self.assertEqual(csv_import.default_prefix(argparse.Namespace(file=pathlib.Path("wp_posts.tsv"))),
                         "csv-wp_posts")


class InterfaceTests(unittest.TestCase):
    def test_module_interface(self):
        self.assertEqual(csv_import.NAME, "csv")
        self.assertNotIn("\n", csv_import.HELP)
        for attr in ("add_arguments", "default_prefix", "iter_articles"):
            self.assertTrue(callable(getattr(csv_import, attr)))

    def test_now_is_not_hardcoded(self):
        # The scheduled-post cutoff follows the clock: a row dated tomorrow+2 is skipped.
        future = datetime.now(timezone.utc).replace(microsecond=0)
        future = future.replace(year=future.year + 1).strftime("%Y-%m-%dT%H:%M:%SZ")
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "f.csv"
            path.write_text(f"title,body,url,date\nA,B,https://news.example.com/f/,{future}\n", encoding="utf-8")
            raws, _ = run_csv(["--file", str(path)])
        self.assertIn("dated in the future", raws[0]["_skip"])


# ---------------------------------------------------------------- review: silent wrong data

class ZoneAndClockTests(unittest.TestCase):
    """AM/PM and time zones written in prose dates must be honoured, or the row refused, never dropped."""

    def parse(self, value, formats=(), tz=timezone.utc):
        dt = csv_import._DateParser(list(formats), tz, False).parse(value)
        return dt and dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_pm_is_not_dropped(self):
        self.assertEqual(self.parse("May 1, 2024 1:05 PM"), "2024-05-01T13:05:00Z")
        self.assertEqual(self.parse("12 Jan 2024 3:00 PM"), "2024-01-12T15:00:00Z")
        self.assertEqual(self.parse("Jan 5 2024 10:00 PM"), "2024-01-05T22:00:00Z")
        self.assertEqual(self.parse("May 1, 2024 1:05PM"), "2024-05-01T13:05:00Z")
        self.assertEqual(self.parse("Wednesday, May 1, 2024 at 1:05 pm"), "2024-05-01T13:05:00Z")

    def test_zone_names_and_offsets_are_applied(self):
        self.assertEqual(self.parse("May 1, 2024 1:05 PM EDT"), "2024-05-01T17:05:00Z")
        self.assertEqual(self.parse("Jan 5, 2024 10:00 PM UTC"), "2024-01-05T22:00:00Z")
        self.assertEqual(self.parse("May 1 2024 13:00:00 GMT-0400"), "2024-05-01T17:00:00Z")
        self.assertEqual(self.parse("Wed May 01 2024 09:00:00 GMT-0400 (Eastern Daylight Time)"),
                         "2024-05-01T13:00:00Z")  # JavaScript Date.toString()
        self.assertEqual(self.parse("5 Jan 2024 22:00 CET"), "2024-01-05T21:00:00Z")
        self.assertEqual(self.parse("2024-05-01 09:00:00 EDT"), "2024-05-01T13:00:00Z")
        self.assertEqual(self.parse("Tue, 30 Apr 2024 21:00:00 PDT"), "2024-05-01T04:00:00Z")
        self.assertEqual(self.parse("Wed May 1 09:00:00 EDT 2024"), "2024-05-01T13:00:00Z")  # Unix date(1)
        self.assertEqual(self.parse("Wed, 01 May 24 13:00:00 GMT"), "2024-05-01T13:00:00Z")  # RFC 822 2-digit year
        # A zone in the text wins over --timezone, which is only for dates without one.
        from zoneinfo import ZoneInfo
        self.assertEqual(self.parse("May 1, 2024 1:05 PM EDT", tz=ZoneInfo("Europe/Berlin")),
                         "2024-05-01T17:05:00Z")

    def test_us_region_zones_follow_daylight_saving(self):
        self.assertEqual(self.parse("Jan. 12, 2024, 3:00 p.m. ET"), "2024-01-12T20:00:00Z")  # EST
        self.assertEqual(self.parse("July 4, 2024, 9:30 p.m. ET"), "2024-07-05T01:30:00Z")  # EDT
        self.assertEqual(self.parse("July 4, 2024 6:00 PM PT"), "2024-07-05T01:00:00Z")

    def test_time_range_is_not_an_offset(self):
        self.assertIsNone(self.parse("May 1, 2024 10:00-11:30"))
        self.assertEqual(self.parse("1 May 2024 13:00:00 +0530"), "2024-05-01T07:30:00Z")

    def test_unknown_zones_are_refused_not_ignored(self):
        for value in ("Mar 3, 2024 9:15 AM IST", "1 May 2024 13:00 XYZ",
                      "May 1 2024 09:00 (Eastern Daylight Time)"):
            with self.assertRaises(csv_import._UnknownZone, msg=value):
                self.parse(value)

    def test_user_format_still_applies_with_a_zone_after_it(self):
        self.assertEqual(self.parse("5/1/2024 1:05 PM EST", ["%m/%d/%Y %I:%M %p"]), "2024-05-01T18:05:00Z")
        self.assertEqual(self.parse("Mar 3, 2024 9:15 AM IST", ["%b %d, %Y %I:%M %p IST"],
                                    tz=timezone(csv_import.timedelta(hours=5, minutes=30))),
                         "2024-03-03T03:45:00Z")

    def test_weekday_and_dash_numeric_dates_are_still_never_guessed(self):
        for value in ("Mon, 05/01/2024 10:00", "Wed, 05/01/2024 - 10:00", "05/01/2024, 10:00 AM"):
            with self.assertRaises(csv_import._AmbiguousDate, msg=value):
                self.parse(value)
        m = csv_import._numeric_dm("Wed, 05/01/2024 - 10:00")
        self.assertEqual(csv_import._suggest_formats(m), ("%m/%d/%Y - %H:%M", "%d/%m/%Y - %H:%M"))
        self.assertEqual(self.parse("Wed, 05/01/2024 - 10:00", ["%m/%d/%Y - %H:%M"]), "2024-05-01T10:00:00Z")

    def test_ap_style_fixture(self):
        raws, err = run_csv(["--file", fx("ap_style_dates.csv")])
        ids = by_id(normalized(raws)[0])
        self.assertEqual(ids["csv-A-1"]["publish_date"], "2024-05-01T17:05:00Z")
        self.assertEqual(ids["csv-A-2"]["publish_date"], "2024-01-12T20:00:00Z")
        self.assertEqual(ids["csv-A-3"]["publish_date"], "2024-05-01T13:00:00Z")
        self.assertEqual(ids["csv-A-4"]["publish_date"], "2023-11-05T22:30:00Z")
        self.assertEqual(ids["csv-A-5"]["publish_date"], "2024-01-05T21:00:00Z")
        self.assertNotIn("csv-A-6", ids)
        self.assertEqual(skip_reasons(raws)["time zone 'IST' in the date isn't one the importer knows (match it "
                                            "in --date-format and set --timezone-offset)"], 1)
        self.assertIn("'Mar 3, 2024 9:15 AM IST'", err)


class DrupalViewsTests(unittest.TestCase):
    def test_medium_date_format_is_never_guessed(self):
        msg = run_error(["--file", fx("drupal_views_published.csv"), "--url-col", "Path"])
        self.assertIn("won't guess", msg)
        self.assertIn("--date-format '%m/%d/%Y - %H:%M'", msg)
        self.assertIn("'Tue, 11/14/2023 - 17:13' only fits month first", msg)

    def test_published_yes_no_column_filters_rows(self):
        raws, err = run_csv(["--file", fx("drupal_views_published.csv"), "--url-col", "Path",
                             "--date-format", "%m/%d/%Y - %H:%M", "--timezone", "America/New_York"])
        self.assertIn("date       <- 'Post date' (column 4)", err)
        self.assertIn("status     <- 'Published' (column 5)", err)
        self.assertEqual(skip_reasons(raws)["status 'no' is not published (list it in --published-values "
                                            "to import it)"], 1)
        articles, _ = normalized(raws)
        self.assertEqual([a["headline"] for a in articles], ["Budget hearing set for Monday", "Parks plan unveiled"])
        self.assertEqual(articles[0]["publish_date"], "2024-05-01T14:00:00Z")  # EDT
        self.assertEqual(articles[1]["publish_date"], "2023-11-14T22:13:00Z")  # EST
        self.assertEqual(articles[0]["authors"], [])  # 'Authored by' is a login, not a byline column

    def test_published_column_with_dates_is_not_a_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "p.csv"
            path.write_text("title,body,url,post_date,published\n"
                            "A,B,https://news.example.com/a/,2024-05-01 10:00,2024-05-01 10:00\n", encoding="utf-8")
            raws, err = run_csv(["--file", str(path)])
        self.assertIn("holds dates, not a published yes/no flag", err)
        self.assertEqual(len(normalized(raws)[0]), 1)


class HiddenFlagTests(unittest.TestCase):
    """Soft-deleted, draft and private flags in an application database export."""

    def test_flag_columns_are_detected_and_rows_skipped(self):
        raws, err = run_csv(["--file", fx("rails_articles.csv")])
        self.assertIn("hidden     <- 'deleted_at' (column 11), 'is_draft' (column 12)", err)
        reasons = skip_reasons(raws)
        self.assertEqual(reasons["flagged in column 'deleted_at' (deleted, draft, private or members-only; "
                                 "see --hidden-col)"], 1)
        self.assertEqual(reasons["flagged in column 'is_draft' (deleted, draft, private or members-only; "
                                 "see --hidden-col)"], 1)
        self.assertNotIn("every row is taken to be published", err)
        ids = by_id(normalized(raws)[0])
        self.assertEqual(sorted(ids), ["csv-1", "csv-4"])
        self.assertEqual(ids["csv-1"]["publish_date"], "2024-05-01T13:00:00Z")

    def test_flags_can_be_chosen_or_ignored(self):
        raws, _ = run_csv(["--file", fx("rails_articles.csv"), "--hidden-col", "is_draft"])
        self.assertEqual(sorted(by_id(normalized(raws)[0])), ["csv-1", "csv-2", "csv-4"])
        raws, err = run_csv(["--file", fx("rails_articles.csv"), "--hidden-col", "none"])
        self.assertIn("hidden     <- (none)", err)
        self.assertEqual(len(normalized(raws)[0]), 4)

    def test_display_name_byline_beats_login_author(self):
        raws, err = run_csv(["--file", fx("rails_articles.csv")])
        self.assertIn("authors    <- 'byline' (column 7)", err)
        ids = by_id(normalized(raws)[0])
        self.assertEqual(ids["csv-1"]["authors"], ["Jane Doe"])
        self.assertEqual(ids["csv-4"]["authors"], ["Ann Lee", "Sam Rivera"])

    def test_login_like_bylines_are_flagged(self):
        _, err = run_csv(["--file", fx("rails_articles.csv"), "--authors-col", "author"])
        self.assertIn("look like user names", err)
        self.assertIn("'jdoe'", err)
        _, err = run_csv(["--file", fx("rails_articles.csv")])
        self.assertNotIn("look like user names", err)


class WordPressRestFlatTests(unittest.TestCase):
    def test_gmt_date_column_is_preferred_over_local_date(self):
        raws, err = run_csv(["--file", fx("wp_rest_flat.csv")])
        self.assertIn("date       <- 'date_gmt' (column 3)  [naive times read as UTC", err)
        reasons = skip_reasons(raws)
        self.assertEqual(reasons["status 'private' is not published (list it in --published-values to import it)"], 1)
        ids = by_id(normalized(raws)[0])
        self.assertEqual(ids["csv-9001"]["publish_date"], "2024-05-01T13:30:00Z")
        self.assertEqual(ids["csv-9002"]["publish_date"], "2023-11-08T01:15:00Z")
        self.assertEqual(ids["csv-9001"]["headline"], "Primary day – what to know")
        self.assertEqual(ids["csv-9001"]["authors"], [])  # numeric user ids


class MisalignedRowTests(TempDirTest):
    def test_row_with_unquoted_delimiter_is_skipped_not_shifted(self):
        path = self.write("shift.csv", "ID,URL,Date,Title,Body,Author\n"
                                       "1,https://news.example.com/1,2024-05-01,Storm, flood hits town,<p>Body text</p>,Jane Doe\n"
                                       "2,https://news.example.com/2,2024-05-02,Calm day,<p>Quiet</p>,Ann Lee\n")
        raws, err = run_csv(["--file", path])
        articles, _ = normalized(raws)
        self.assertEqual([a["id"] for a in articles], ["csv-2"])
        for a in articles:
            self.assertNotIn("Body text", a["authors"])
        self.assertEqual(skip_reasons(raws)["row has more cells than the header row (an unquoted delimiter "
                                            "inside a field?), so its columns can't be trusted"], 1)
        self.assertIn("first at line 2", err)

    def test_trailing_empty_cells_are_harmless(self):
        path = self.write("trail.csv", "title,body,url,date\nA,B,https://news.example.com/a/,2024-01-01,,\n")
        self.assertEqual(len(normalized(run_csv(["--file", path])[0])[0]), 1)


class EncodingSafetyTests(TempDirTest):
    MIXED = ("title,body,url,date\n"
             "Café opens,<p>Café’s first day</p>,https://news.example.com/1,2024-05-01\n"
             "Señora García,<p>Niño</p>,https://news.example.com/2,2024-05-02\n").encode("utf-8") + \
        b"Bad byte,<p>x \x92 y</p>,https://news.example.com/3,2024-05-03\n"

    def test_mostly_utf8_file_does_not_send_user_to_cp1252(self):
        msg = run_error(["--file", self.write("mixed.csv", self.MIXED)])
        self.assertIn("line 4", msg)
        self.assertIn("garble", msg)
        self.assertIn("--encoding-errors replace", msg)

    def test_utf8_read_as_cp1252_is_refused(self):
        msg = run_error(["--file", self.write("mixed.csv", self.MIXED), "--encoding", "cp1252"])
        self.assertIn("looks like UTF-8", msg)
        self.assertIn("CafÃ©", msg)

    def test_one_good_line_and_one_bad_byte_is_still_utf8(self):
        tie = ("title,body,url,date\nCafé opens,<p>Café</p>,https://news.example.com/1,2024-05-01\n"
               ).encode("utf-8") + b"Bad byte,<p>x \x92 y</p>,https://news.example.com/2,2024-05-02\n"
        path = self.write("tie.csv", tie)
        self.assertIn("--encoding-errors replace", run_error(["--file", path]))
        self.assertNotIn("try --encoding cp1252", run_error(["--file", path]))
        self.assertIn("looks like UTF-8", run_error(["--file", path, "--encoding", "cp1252"]))

    def test_encoding_errors_replace_keeps_the_rest_intact(self):
        raws, err = run_csv(["--file", self.write("mixed.csv", self.MIXED), "--encoding-errors", "replace"])
        articles, _ = normalized(raws)
        self.assertEqual([a["headline"] for a in articles], ["Café opens", "Señora García", "Bad byte"])
        self.assertEqual(articles[2]["content"], "x � y")
        self.assertIn("1 row(s) had bytes that aren't valid utf-8-sig", err)

    def test_cp1252_error_names_the_right_line(self):
        msg = run_error(["--file", fx("excel_eu_cp1252.csv"), "--date-format", "%d/%m/%Y %H:%M"])
        self.assertIn("line 2", msg)

    def test_saved_web_page_is_refused(self):
        path = self.write("export.csv", "\n<!DOCTYPE html>\n<html><head><title>Sign in</title></head></html>\n")
        msg = run_error(["--file", path])
        self.assertIn("web page", msg)
        path = self.write("wxr.csv", '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"></rss>\n')
        self.assertIn("wordpress-export", run_error(["--file", path]))

    def test_utf16_with_replace_names_the_real_encoding(self):
        text = "Title\tBody\tLink\tDate\r\nZürich\t<p>Grüezi</p>\thttps://news.example.ch/z/\t2024-03-01\r\n"
        path = self.write("u16.txt", b"\xff\xfe" + text.encode("utf-16-le") + b"\x00\xd8")  # lone surrogate
        raws, err = run_csv(["--file", path, "--encoding-errors", "replace"])
        self.assertEqual(len(normalized(raws)[0]), 1)
        self.assertNotIn("utf-8-sig", err)


class ReviewEndToEndTests(TempDirTest):
    def test_rails_export_end_to_end(self):
        out, err = io.StringIO(), io.StringIO()
        out_dir = self.tmp / "data"
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = import_archive.main(["csv", "--file", fx("rails_articles.csv"), "--out-dir", str(out_dir),
                                        "--id-prefix", "csv-newsapp"])
        self.assertEqual(code, 0, err.getvalue())
        files = sorted(out_dir.glob("*.json"))
        self.assertEqual([f.name for f in files], ["csv-rails_articles-0001.json"])
        articles = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual([(a["id"], a["headline"], a["authors"]) for a in articles],
                         [("csv-newsapp-1", "Council passes budget", ["Jane Doe"]),
                          ("csv-newsapp-4", "Weekend events", ["Ann Lee", "Sam Rivera"])])
        self.assertEqual(articles[0]["content"], "The council passed the budget 9–8.")
        self.assertIn("1 skipped: flagged in column 'deleted_at'", out.getvalue())


if __name__ == "__main__":
    unittest.main()

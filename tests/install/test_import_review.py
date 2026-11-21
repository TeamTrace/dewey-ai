"""review_import(): what the wizard says about articles an importer just wrote.

The importer already reports what it skipped. These are the failures that import
cleanly and only show up later -- a byline field that isn't per-article, a date
that never varies, content that is really an excerpt -- so the wizard names them
while the user can still change the answer.
"""
import pathlib
import sys

import importlib.util
import json
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]


def load_install():
    spec = importlib.util.spec_from_file_location("dewey_install_review", REPO / "install.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


install = load_install()


def article(**over):
    doc = {"id": "wp-1", "headline": "A headline", "url": "https://example.news/a",
           "publish_date": "2025-01-02", "authors": ["Jane Doe"],
           "content": "word " * 300}
    doc.update(over)
    return doc


def corpus(n=4, **over):
    out = []
    for i in range(n):
        doc = article(id=f"wp-{i}", url=f"https://example.news/{i}",
                      publish_date=f"2025-01-0{i + 1}", authors=[f"Writer {i}"])
        doc.update(over)
        out.append(doc)
    return out


def saw(findings):
    return " ".join(f.saw for f in findings)


class CleanImportTest(unittest.TestCase):

    def test_a_healthy_import_has_nothing_to_say(self):
        self.assertEqual(install.review_import(corpus()), [])

    def test_no_articles_is_not_a_finding(self):
        self.assertEqual(install.review_import([]), [])

    def test_a_single_article_is_not_judged_on_variety(self):
        # One article trivially has one byline and one date; neither is a problem.
        self.assertEqual(install.review_import([article()]), [])


class BylineTest(unittest.TestCase):

    def test_no_byline_anywhere_is_reported_but_not_blocking(self):
        findings = install.review_import(corpus(authors=[]))
        self.assertEqual(len(findings), 1)
        self.assertFalse(findings[0].stop)
        self.assertIn("None of the 4 articles has a byline", findings[0].saw)
        self.assertIn("--byline-field", findings[0].fix)

    def test_the_same_byline_on_every_article_blocks(self):
        findings = [f for f in install.review_import(corpus(authors=["Afea Tucker"]))
                    if "credited to" in f.saw]
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0].stop, "a uniform byline should stop the full import")
        self.assertIn("'Afea Tucker'", findings[0].saw)
        self.assertIn("Compare a few with the live site", findings[0].fix)
        # The importer now reads yoast_head_json.author post by post (#4), so the advice no
        # longer blames that field.
        self.assertNotIn("yoast", findings[0].fix)

    def test_one_shared_byline_among_several_is_not_flagged(self):
        docs = corpus()
        docs[0]["authors"] = docs[1]["authors"] = ["Shared Name"]
        self.assertNotIn("credited to", saw(install.review_import(docs)))

    def test_two_names_in_one_string_are_reported(self):
        docs = corpus()
        docs[0]["authors"] = ["Samantha Storey and Brian Freskos"]
        findings = [f for f in install.review_import(docs) if "more than one name" in f.saw]
        self.assertEqual(len(findings), 1)
        self.assertFalse(findings[0].stop)
        self.assertIn("Samantha Storey and Brian Freskos", findings[0].saw)

    def test_a_comma_in_a_byline_is_reported_too(self):
        docs = corpus()
        docs[0]["authors"] = ["Mascia, Jennifer"]
        self.assertIn("more than one name", saw(install.review_import(docs)))


class FieldMappingTest(unittest.TestCase):

    def test_repeated_ids_block(self):
        docs = corpus()
        docs[1]["id"] = docs[0]["id"]
        findings = [f for f in install.review_import(docs) if "id(s) appear" in f.saw]
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0].stop)

    def test_articles_without_a_usable_url(self):
        docs = corpus()
        docs[0]["url"] = "/relative/path"
        docs[1]["url"] = ""
        findings = [f for f in install.review_import(docs) if "no usable url" in f.saw]
        self.assertEqual(len(findings), 1)
        self.assertIn("2 of 4", findings[0].saw)
        self.assertFalse(findings[0].stop)

    def test_one_date_for_everything(self):
        findings = [f for f in install.review_import(corpus(publish_date="2026-11-19"))
                    if "publish date" in f.saw]
        self.assertEqual(len(findings), 1)
        self.assertIn("2026-11-19", findings[0].saw)

    def test_excerpt_length_content_blocks(self):
        findings = [f for f in install.review_import(corpus(content="A short teaser."))
                    if "median article" in f.saw]
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0].stop)


class ReportingTest(unittest.TestCase):

    def test_a_blocking_finding_makes_the_report_say_no(self):
        self.assertFalse(install.report_import_review(corpus(authors=["One Name"])))

    def test_a_non_blocking_finding_still_allows_continuing(self):
        self.assertTrue(install.report_import_review(corpus(url="/relative")))

    def test_a_clean_import_allows_continuing(self):
        self.assertTrue(install.report_import_review(corpus()))

    def test_no_articles_allows_continuing(self):
        self.assertTrue(install.report_import_review([]))


class LoadImportedTest(unittest.TestCase):

    def test_reads_every_file_and_ignores_unreadable_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = pathlib.Path(tmp, "a-0001.json")
            good.write_text(json.dumps(corpus(2)))
            broken = pathlib.Path(tmp, "b-0001.json")
            broken.write_text("{not json")
            notalist = pathlib.Path(tmp, "c-0001.json")
            notalist.write_text(json.dumps({"id": "x"}))
            docs = install.load_imported([good, broken, notalist, pathlib.Path(tmp, "missing.json")])
        self.assertEqual(len(docs), 2)


if __name__ == "__main__":
    unittest.main()


class ProvenanceTest(unittest.TestCase):
    """A byline can be present, varied and still wrong: the WordPress author field is
    whoever published the story, which on some sites is an editor. No check on the data
    can see that, so the wizard says where the names came from."""

    def plan(self, source, *args):
        return install.ImportPlan(source, list(args))

    def test_wordpress_api_bylines_get_a_spot_check_prompt(self):
        findings = [f for f in install.review_import(corpus(), self.plan("wordpress-api", "--site", "https://x"))
                    if "WordPress author field" in f.saw]
        self.assertEqual(len(findings), 1)
        self.assertFalse(findings[0].stop, "an advisory should not block the import")
        self.assertIn("check the bylines match", findings[0].fix)

    def test_an_explicit_byline_field_means_the_advisory_is_not_needed(self):
        plan = self.plan("wordpress-api", "--site", "https://x", "--byline-field", "acf.byline")
        self.assertNotIn("WordPress author field", saw(install.review_import(corpus(), plan)))

    def test_other_importers_do_not_get_it(self):
        for source in ("wordpress-export", "csv", "feed", "ghost", "drupal", "json"):
            self.assertNotIn("WordPress author field",
                             saw(install.review_import(corpus(), self.plan(source))),
                             f"{source} should not mention the WordPress author field")

    def test_no_bylines_means_no_spot_check_to_do(self):
        plan = self.plan("wordpress-api", "--site", "https://x")
        self.assertNotIn("WordPress author field", saw(install.review_import(corpus(authors=[]), plan)))

    def test_without_a_plan_the_review_still_works(self):
        self.assertEqual(install.review_import(corpus()), [])

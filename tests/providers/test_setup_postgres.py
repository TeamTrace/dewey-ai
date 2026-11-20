"""
Tests for app/setup_postgres.py's loading step: two sources that claim the same
article id for stories on different sites are refused instead of one silently
replacing the other in the index (articles are upserted by id). No database.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402

import import_archive  # noqa: E402
import setup_postgres  # noqa: E402

GUID = "https://newsroom.example/?p=123"


def rss(site: str, headline: str, link: str) -> str:
    return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>{site}</title><link>{site}</link>'
            f'<item><title>{headline}</title><link>{link}</link><guid isPermaLink="false">{GUID}</guid>'
            f'<pubDate>Tue, 05 Mar 2024 14:30:00 +0000</pubDate>'
            f'<description>{headline} text.</description></item></channel></rss>')


def article(id_, url, headline="H"):
    return {"id": id_, "url": url, "headline": headline, "content": "Text.", "authors": [],
            "publish_date": "2024-03-05T14:30:00Z"}


class ConflictingIdTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.data = self.dir / "data"
        self.data.mkdir()

    def load(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return setup_postgres.load_documents(self.data)

    def import_feed(self, name, xml):
        path = self.dir / name
        path.write_text(xml, encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(import_archive.main(["feed", "--file", str(path), "--out-dir", str(self.data)]), 0)

    def test_second_feed_reusing_a_guid_cannot_replace_the_story(self):
        # A partner's or compromised feed reuses the newsroom's public guid; its file sorts last, so
        # the upsert used to replace the genuine headline, text and URL.
        self.import_feed("legit.xml", rss("https://newsroom.example/", "Real story", "https://newsroom.example/real/"))
        self.import_feed("zz-partner.xml", rss("https://partner.example/", "Fake story", "https://partner.example/fake/"))
        ids = {json.loads(p.read_text())[0]["id"] for p in self.data.glob("*.json")}
        self.assertEqual(len(ids), 1)  # the importer gives both the same id
        with self.assertRaises(setup_postgres.ConflictingIdsError) as ctx:
            self.load()
        message = str(ctx.exception)
        self.assertIn("feed-legit-0001.json (https://newsroom.example/real/)", message)
        self.assertIn("feed-zz-partner-0001.json (https://partner.example/fake/)", message)

    def test_same_story_from_two_sources_on_one_site_still_loads(self):
        # e.g. wordpress-export for the old archive and wordpress-api for recent posts: same ids by design.
        (self.data / "a.json").write_text(json.dumps([article("wp-site-1", "https://www.news.example/a/")]))
        (self.data / "b.json").write_text(json.dumps([article("wp-site-1", "http://news.example/a/", "H2"),
                                                      article("wp-site-2", "https://news.example/b/")]))
        self.assertEqual(len(self.load()), 3)


if __name__ == "__main__":
    unittest.main()

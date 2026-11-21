"""
Tests for the author-entry check every retriever shares (app/providers/base.py:
author_names). It decides which entries of metadata["authors"] are used, skipped
or refused, so that a byline search can never quietly become a search of every
writer's stories. No database, network or API key.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import unittest  # noqa: E402

from providers.base import MetadataError, UserFacingError, author_names  # noqa: E402

NO_AUTHORS = object()


def meta(authors=NO_AUTHORS):
    """Search metadata with this "authors" value. Called with no argument, it has no "authors" key."""
    metadata = {"question": "q", "date_range": {"start_date": None, "end_date": None}}
    if authors is not NO_AUTHORS:
        metadata["authors"] = authors
    return metadata


class AuthorNamesTests(unittest.TestCase):
    def refused(self, authors):
        """The message of the MetadataError that this "authors" value is refused with."""
        with self.assertRaises(MetadataError) as cm:
            author_names(meta(authors))
        return str(cm.exception)

    # --- used and skipped

    def test_names_come_back_in_order_and_unchanged(self):
        # No trimming and no de-duplication here: each store compares names in its own way
        # (Postgres ignores case and spacing, Azure matches exactly). \u00a0 is a no-break space.
        entries = [{"name": "Jane Doe"}, {"name": " john  roe "}, {"name": "Jane Doe"}, {"name": "Sam\u00a0Lee"}]
        self.assertEqual(author_names(meta(entries)), ["Jane Doe", " john  roe ", "Jane Doe", "Sam\u00a0Lee"])

    def test_missing_none_or_empty_authors_mean_no_author_filter(self):
        self.assertEqual(author_names(meta()), [])
        for authors in (None, [], ()):
            with self.subTest(authors=authors):
                self.assertEqual(author_names(meta(authors)), [])

    def test_blank_names_are_skipped(self):
        # A blank name names nobody, so skipping it loses nothing that was asked for.
        blanks = [{"name": ""}, {"name": "   "}, {"name": "\u00a0"}, {"name": "\t\n"}, {"name": None}]
        self.assertEqual(author_names(meta(blanks)), [])
        self.assertEqual(author_names(meta([{"name": "\u00a0"}, {"name": "Jane Doe"}, {"name": None}])),
                         ["Jane Doe"])

    def test_tuples_and_extra_keys_are_accepted(self):
        self.assertEqual(author_names(meta(({"name": "Jane Doe", "extra": 1},))), ["Jane Doe"])

    # --- refused

    def test_plain_string_entry_is_refused(self):
        message = self.refused(["Jane Doe"])
        for part in ("Author entry 1 of 1", "a plain string", '{"name": "Jane Doe"}', "did not run the search"):
            with self.subTest(part=part):
                self.assertIn(part, message)

    def test_mixed_list_is_refused_and_the_bad_entry_is_named(self):
        # Refused whole: a filter on John Roe alone would leave Jane Doe's stories out without a word.
        self.assertIn("Author entry 2 of 2", self.refused([{"name": "John Roe"}, "Jane Doe"]))
        self.assertIn("Author entry 1 of 2", self.refused(["Jane Doe", {"name": "John Roe"}]))
        # Skipped entries are counted, so the position is the entry's place in the list as given.
        self.assertIn("Author entry 3 of 4", self.refused([{"name": ""}, {"name": None}, "Jane Doe", {"name": "x"}]))

    def test_other_malformed_entries_are_refused(self):
        for entry in (None, 5, ["Jane Doe"], {}, {"Name": "Jane Doe"}, {"name": 5}, {"name": True},
                      {"name": ["Jane Doe"]}):
            with self.subTest(entry=entry):
                self.refused([entry])
        self.assertIn("is None", self.refused([None]))
        self.assertIn("is of type list", self.refused([["Jane Doe"]]))
        self.assertIn('has no "name"', self.refused([{}]))
        self.assertIn('has no "name"', self.refused([{"Name": "Jane Doe"}]))  # keys are case-sensitive
        self.assertIn("of type int, not text", self.refused([{"name": 5}]))

    def test_authors_that_is_not_a_list_is_refused(self):
        # A bare string used to be read letter by letter and a single dict key by key: no entry
        # was a dict, so the Postgres store searched with no author filter at all.
        values = ("Jane Doe", "", {"name": "Jane Doe"}, 5, b"Jane Doe", ({"name": n} for n in ["Jane Doe"]))
        for authors in values:
            with self.subTest(authors=authors):
                self.assertIn('"authors" in the search metadata must be a list', self.refused(authors))
        self.assertIn("but it is a plain string", self.refused("Jane Doe"))
        self.assertIn("but it is of type dict", self.refused({"name": "Jane Doe"}))

    def test_messages_give_position_and_kind_never_the_value(self):
        # The value can be text a model wrote, and the message is logged: an entry's position and
        # kind are enough to find the mistake.
        probe = "Zelda Quill 7731"
        for authors in ([probe], probe, [{probe: "x"}], [{"name": 77317731}], [{"name": [probe]}], [[probe]],
                        {probe: probe}, 77317731):
            with self.subTest(authors=authors):
                message = self.refused(authors)
                self.assertNotIn("Zelda", message)
                self.assertNotIn("7731", message)

    def test_error_is_a_value_error_and_not_a_user_facing_one(self):
        self.assertTrue(issubclass(MetadataError, ValueError))
        # The MCP server passes a UserFacingError's message on to its clients. Through MCP, only
        # ask_archive can reach this error, when the search plan from Dewey's own model is
        # malformed, and a client can't fix that: it gets the server's generic message with a log
        # reference, and whoever runs Dewey finds this message in the log.
        self.assertFalse(issubclass(MetadataError, UserFacingError))


if __name__ == "__main__":
    unittest.main()

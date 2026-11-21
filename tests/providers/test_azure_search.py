"""
Tests for the Azure retriever's search filter (app/providers/azure_search.py):
the date and byline filter it writes for Azure AI Search, the author entries it
refuses, and that it reads author entries the same way as the Postgres store.

They need the packages in requirements.txt but no Azure account, key or
network: stand-ins take the place of the Azure OpenAI and Azure AI Search
clients, and no real client is built. Nobody has run the azure stack itself
since this fork began (docs/AZURE.md), so these tests check what Dewey writes,
not what Azure does with it.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import contextlib  # noqa: E402
import importlib  # noqa: E402
import io  # noqa: E402
import types  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

# The packages from requirements.txt that the modules under test import. Only these are
# guarded, and only against being absent: a package that is installed but fails to import
# is an error, not a skip.
THIRD_PARTY = ("azure.core.credentials", "azure.search.documents", "dateutil.parser", "numpy", "openai",
               "pgvector.psycopg", "psycopg", "pydantic")
try:
    for _name in THIRD_PARTY:
        importlib.import_module(_name)
except ModuleNotFoundError as e:  # pragma: no cover
    MISSING_PACKAGES = f"needs the packages in requirements.txt ({e})"
else:
    MISSING_PACKAGES = ""

if not MISSING_PACKAGES:
    # Dewey's own modules, outside the guard above: if one of them is broken, these tests must
    # fail, not be skipped. Nothing else in the test suite imports the Azure retriever.
    from models import AzureOpenAIConfig, AzureSearchConfig
    from providers import azure_search, base
    from providers.azure_search import build_filter
    from providers.pgvector_store import build_where


class _NoAuthors:
    def __repr__(self):  # shown in subTest labels
        return 'no "authors" key'


NO_AUTHORS = _NoAuthors()


def meta(authors=(), start=None, end=None):
    """Well-formed search metadata: one {"name": ...} entry per name."""
    return {"question": "q", "date_range": {"start_date": start, "end_date": end},
            "authors": [{"name": a} for a in authors]}


def with_authors(authors=NO_AUTHORS):
    """Search metadata whose "authors" value is passed on as given, for the malformed cases;
    called with no argument, it has no "authors" key. date_range is always there, because
    build_filter() reads it directly."""
    metadata = {"question": "q", "date_range": {"start_date": None, "end_date": None}}
    if authors is not NO_AUTHORS:
        metadata["authors"] = authors
    return metadata


@unittest.skipIf(MISSING_PACKAGES, MISSING_PACKAGES)
class BuildFilterTests(unittest.TestCase):
    def test_no_filters(self):
        self.assertIsNone(build_filter(meta()))

    def test_dates_and_authors_combine(self):
        self.assertEqual(
            build_filter(meta(["Jane Doe"], start="2024-01-01", end="2024-12-31")),
            "publish_date ge 2024-01-01T00:00:00Z and publish_date le 2024-12-31T23:59:59Z and "
            "authors/any(a: a eq 'Jane Doe')")

    def test_unparseable_dates_are_dropped(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(build_filter(meta(start="last spring", end="2024-02-30")))
        self.assertIn("Ignoring unparseable date", err.getvalue())

    def test_names_are_matched_exactly_and_quoted(self):
        # Azure compares bylines exactly, so a name is not trimmed or collapsed here (the Postgres
        # store ignores case and spacing). A single quote is doubled, as OData string literals need.
        self.assertEqual(build_filter(meta(["O'Brien", " Jane  Doe "])),
                         "authors/any(a: a eq 'O''Brien' or a eq ' Jane  Doe ')")

    def test_plain_string_author_is_refused(self):
        # It used to fail with AttributeError ('str' object has no attribute 'get'), while the
        # Postgres store left the name out and searched every writer's stories. The rules are in
        # providers/base.py (author_names).
        with self.assertRaises(base.MetadataError):
            build_filter({"question": "q", "date_range": {"start_date": None, "end_date": None},
                          "authors": ["Jane Doe"]})

    def test_mixed_author_list_is_refused(self):
        with self.assertRaises(base.MetadataError):
            build_filter(with_authors(["Jane Doe", {"name": "John Roe"}]))

    def test_other_malformed_authors_are_refused(self):
        # [{}] used to be left out of the filter without a word; the others raised AttributeError.
        # The last two are an "authors" value that is not a list: a bare string and a single entry.
        for authors in ([{}], [None], [{"name": 5}], "Jane Doe", {"name": "Jane Doe"}):
            with self.subTest(authors=authors), self.assertRaises(base.MetadataError):
                build_filter(with_authors(authors))

    def test_blank_names_mean_no_author_filter(self):
        # A name of only spaces used to become `a eq ' '`, a filter that matches no article.
        # \u00a0 is a no-break space.
        self.assertIsNone(build_filter(meta(["", " ", "\u00a0", None])))
        self.assertEqual(build_filter(meta(["", " "], start="2024-01-01")), "publish_date ge 2024-01-01T00:00:00Z")

    def test_missing_or_none_authors_mean_no_author_filter(self):
        # These used to raise KeyError and TypeError. The Postgres store already read both as
        # "no author filter".
        self.assertIsNone(build_filter(with_authors()))
        self.assertIsNone(build_filter(with_authors(None)))


@unittest.skipIf(MISSING_PACKAGES, MISSING_PACKAGES)
class BothStoresTests(unittest.TestCase):
    """build_where() (Postgres) and build_filter() (Azure) read "authors" through one shared
    check, base.author_names(). These rows fail if either store goes back to reading the
    entries itself."""

    # ("authors" value, the names both stores must filter on). NO_AUTHORS: the key is missing.
    USED_OR_SKIPPED = [
        ([{"name": "Jane Doe"}], ["Jane Doe"]),
        ([{"name": "Jane Doe"}, {"name": "John Roe"}], ["Jane Doe", "John Roe"]),
        (({"name": "Jane Doe", "extra": 1},), ["Jane Doe"]),
        ([{"name": ""}, {"name": "Jane Doe"}, {"name": None}], ["Jane Doe"]),
        ([{"name": ""}, {"name": "  "}, {"name": "\u00a0"}, {"name": None}], []),
        ([], []),
        (None, []),
        (NO_AUTHORS, []),
    ]
    REFUSED = [
        ["Jane Doe"],
        ["Jane Doe", {"name": "John Roe"}],
        [{"name": "John Roe"}, "Jane Doe"],
        [None],
        [{}],
        [{"name": 5}],
        "Jane Doe",
        {"name": "Jane Doe"},
    ]

    def test_both_stores_use_skip_and_refuse_the_same_entries(self):
        for authors, names in self.USED_OR_SKIPPED:
            with self.subTest(authors=authors):
                _, params = build_where(with_authors(authors))
                self.assertEqual(params.get("authors", []), names)
                clause = "authors/any(a: " + " or ".join(f"a eq '{name}'" for name in names) + ")"
                self.assertEqual(build_filter(with_authors(authors)), clause if names else None)
        for authors in self.REFUSED:
            for build in (build_where, build_filter):
                with self.subTest(authors=authors, store=build.__name__), self.assertRaises(base.MetadataError):
                    build(with_authors(authors))


class FakeEmbeddings:
    """Stands in for the Azure OpenAI client's `embeddings`: counts the requests, sends none."""

    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return types.SimpleNamespace(data=[types.SimpleNamespace(embedding=[0.1, 0.2, 0.3])])


class FakeSearchClient:
    """Stands in for azure.search.documents.SearchClient: keeps each search's arguments and
    finds nothing."""

    def __init__(self, endpoint, index_name, credential):
        self.searches = []

    def search(self, **kwargs):
        self.searches.append(kwargs)
        return []


@unittest.skipIf(MISSING_PACKAGES, MISSING_PACKAGES)
class RetrieveTests(unittest.TestCase):
    """AzureSearchRetriever.retrieve() with both Azure clients replaced by stand-ins."""

    def retriever(self):
        embeddings = FakeEmbeddings()
        # The retriever builds its own search client, so the class is swapped while it does.
        with mock.patch.object(azure_search, "SearchClient", FakeSearchClient):
            retriever = azure_search.AzureSearchRetriever(
                types.SimpleNamespace(embeddings=embeddings),
                AzureOpenAIConfig(endpoint="https://example.invalid", api_key="made-up-key",
                                  embedding_deployment="embedding", embedding_model="embedding-model",
                                  chat_deployment="chat", chat_model="chat-model"),
                AzureSearchConfig(service_endpoint="https://example.invalid", index_name="dewey",
                                  key="made-up-key"),
            )
        return retriever, embeddings, retriever.search_client

    def test_malformed_author_stops_the_search_before_any_azure_call(self):
        # The embedding request can cost money, and a failure there would hide the clear error.
        retriever, embeddings, search_client = self.retriever()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(base.MetadataError):
            retriever.retrieve(with_authors(["Jane Doe"]))
        self.assertEqual(embeddings.calls, 0)
        self.assertEqual(search_client.searches, [])

    def test_well_formed_request_sends_its_filter_with_the_search(self):
        # The filter is built first, but a request it accepts still goes on to search with it.
        retriever, embeddings, search_client = self.retriever()
        with contextlib.redirect_stderr(io.StringIO()):  # the retriever prints each filter to stderr
            sources = retriever.retrieve(meta(["Jane Doe"], start="2024-01-01"))
        self.assertEqual(sources, [])
        self.assertEqual(embeddings.calls, 1)
        (search,) = search_client.searches
        self.assertEqual(search["filter"], "publish_date ge 2024-01-01T00:00:00Z and authors/any(a: a eq 'Jane Doe')")


if __name__ == "__main__":
    unittest.main()

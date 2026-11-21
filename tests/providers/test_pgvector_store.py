"""
Tests for the Postgres store's search filters (app/providers/pgvector_store.py),
including the author entries it refuses rather than leaves out of the filter,
and for its warning about a pgvector older than 0.8.

The unit tests need the packages in requirements.txt but no database: the
version-warning tests search through a fake connection that reports whichever
pgvector version a test asks for. The PostgresFilterTests run the SQL for real
against a throwaway Postgres with pgvector, only when DEWEY_TEST_DATABASE_URL
is set, e.g.

    DEWEY_TEST_DATABASE_URL='postgresql://postgres@/dewey?host=/var/run/postgresql' \\
        python -m unittest tests.providers.test_pgvector_store

Each test class builds Dewey's real schema (schema_sql: tables, the byline
function and every index) in its own new schema, first on the search_path, and
drops that schema at the end, so an existing Dewey index in the same database
is never read or changed. The vector extension is created only if the
database doesn't have it yet (inside that schema, so it goes with it).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import contextlib  # noqa: E402
import importlib.util  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import random  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

try:
    import numpy as np
    import psycopg
    from pgvector.psycopg import register_vector

    from providers import base, pgvector_store
    from providers.pgvector_store import HYBRID_SQL, build_where
except ImportError as e:  # pragma: no cover
    pgvector_store = None
    IMPORT_ERROR = f"needs the packages in requirements.txt ({e})"
else:
    IMPORT_ERROR = ""

DATABASE_URL = os.environ.get("DEWEY_TEST_DATABASE_URL", "").strip()
REPO = pathlib.Path(__file__).resolve().parents[2]


def meta(authors=(), start=None, end=None):
    return {"question": "q", "date_range": {"start_date": start, "end_date": end},
            "authors": [{"name": a} for a in authors]}


class FakeEmbedder:
    """Stands in for an Embedder (providers/embeddings.py): counts the calls, loads no model
    and contacts nothing."""
    model, dim = "fake", 3

    def __init__(self):
        self.calls = 0

    def embed(self, texts, input_type):
        self.calls += 1
        return [[0.1, 0.2, 0.3] for _ in texts]


class FakeReranker:
    """Stands in for a Reranker (providers/embeddings.py): keeps the order it is given."""

    def rank(self, query, documents, top_k):
        return list(range(top_k))


class FakeResult:
    """What FakeConnection.execute() returns."""

    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeConnection:
    """Stands in for a psycopg connection to a database that passes check_index() and reports
    one pgvector version. It records every statement it is sent, and each search finds one
    article."""

    FOUND = ("The council voted on the budget.", "https://news.example.org/budget-vote", "Budget vote",
             ["Jane Doe"], datetime(2024, 3, 1, 12, tzinfo=timezone.utc), "The Example")

    def __init__(self, extversion, barrier=None):
        self.extversion = extversion
        self.barrier = barrier  # holds threads at the version query until all of them have arrived
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        sql = " ".join(str(sql).split())
        self.statements.append(sql)
        if "extversion" in sql:
            if self.barrier is not None:
                self.barrier.wait(timeout=10)
            return FakeResult([(self.extversion,)])
        if "FROM index_info" in sql:
            return FakeResult([("embed_model", FakeEmbedder.model), ("embed_dim", str(FakeEmbedder.dim))])
        if "to_regprocedure" in sql:
            return FakeResult([("dewey_byline_keys(text[])",)])
        if sql.startswith("WITH semantic"):
            return FakeResult([self.FOUND])
        return FakeResult([])

    def count(self, text):
        return sum(text in statement for statement in self.statements)


@unittest.skipIf(pgvector_store is None, IMPORT_ERROR)
class BuildWhereTests(unittest.TestCase):
    def test_no_filters(self):
        self.assertEqual(build_where(meta()), ("TRUE", {}))
        self.assertEqual(build_where({"question": "q"}), ("TRUE", {}))

    def test_date_filters_are_unchanged(self):
        where, params = build_where(meta(start="2024-01-01", end="2024-12-31"))
        self.assertEqual(where, "a.publish_date >= (%(start_date)s::date)::timestamp AT TIME ZONE 'UTC' AND "
                                "a.publish_date < (%(end_date)s::date + 1)::timestamp AT TIME ZONE 'UTC'")
        self.assertEqual(params, {"start_date": "2024-01-01", "end_date": "2024-12-31"})

    def test_unparseable_dates_are_dropped(self):
        # The warning may go to stdout or stderr (the MCP server requires stderr: stdout is its channel).
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(build_where(meta(start="last spring", end="2024-02-30")), ("TRUE", {}))
        self.assertIn("Ignoring unparseable start_date", out.getvalue() + err.getvalue())

    def test_authors_are_one_bound_parameter(self):
        name = "O'Brien'); DROP TABLE articles; --"
        where, params = build_where(meta([name, "Jane Doe"]))
        self.assertEqual(where, pgvector_store.AUTHOR_MATCH_SQL)
        self.assertNotIn("O'Brien", where)
        self.assertEqual(params, {"authors": [name, "Jane Doe"]})

    def test_both_sides_are_normalized_the_same_way(self):
        where, _ = build_where(meta(["Jane Doe"]))
        self.assertEqual(where, "dewey_byline_keys(a.authors) && dewey_byline_keys(%(authors)s::text[])")
        norm = "lower(btrim(regexp_replace(n, E'[\\\\s\\\\u00a0]+', ' ', 'g')))"
        self.assertIn(f"SELECT ARRAY(SELECT {norm} FROM unnest(names) AS n)", pgvector_store.BYLINE_KEYS_SQL)

    def test_byline_function_and_its_index_are_in_the_schema(self):
        stmts = pgvector_store.schema_sql(3)
        function = stmts.index(pgvector_store.BYLINE_KEYS_SQL)
        index = next(i for i, s in enumerate(stmts) if "articles_byline_keys_idx" in s)
        self.assertLess(function, index)
        self.assertIn("IMMUTABLE", pgvector_store.BYLINE_KEYS_SQL)  # required for an index expression
        self.assertIn("USING gin (dewey_byline_keys(authors))", stmts[index])

    def test_requested_names_are_cleaned(self):
        _, params = build_where({"question": "q", "authors": [
            {"name": "  jane   doe "}, {"name": "Jane Doe"}, {"name": "   "}, {"name": None}, {"name": "Sam Lee"},
            {"name": "Sam\tLee"}]})
        self.assertEqual(params, {"authors": ["jane doe", "Sam Lee"]})
        self.assertEqual(build_where(meta(["", " "])), ("TRUE", {}))

    def test_plain_string_author_is_refused(self):
        # It used to be left out without a word, which made this ("TRUE", {}): a search of every
        # writer's stories. The rules are in providers/base.py (author_names).
        with self.assertRaises(base.MetadataError):
            build_where({"question": "q", "authors": ["Jane Doe"]})

    def test_mixed_author_list_is_refused(self):
        # It used to filter on John Roe alone, so Jane Doe's stories went missing without a word.
        with self.assertRaises(base.MetadataError):
            build_where({"question": "q", "authors": ["Jane Doe", {"name": "John Roe"}]})

    def test_other_malformed_authors_are_refused(self):
        # Each of these used to give ("TRUE", {}) too. The last two are an "authors" value that
        # is not a list: a bare string and a single entry.
        for authors in ([{}], [None], [{"name": 5}], "Jane Doe", {"name": "Jane Doe"}):
            with self.subTest(authors=authors), self.assertRaises(base.MetadataError):
                build_where({"question": "q", "authors": authors})

    def test_authors_and_dates_combine(self):
        where, params = build_where(meta(["Jane Doe"], start="2024-01-01"))
        self.assertTrue(where.startswith("a.publish_date >= "))
        self.assertTrue(where.endswith(pgvector_store.AUTHOR_MATCH_SQL))
        self.assertEqual(set(params), {"start_date", "authors"})


@unittest.skipIf(pgvector_store is None, IMPORT_ERROR)
class RetrieveTests(unittest.TestCase):
    """PgVectorStore.retrieve() up to the point where it opens the database: a stand-in
    embedder, and connect() replaced so that no connection is ever attempted."""

    def store(self):
        embedder = FakeEmbedder()
        store = pgvector_store.PgVectorStore(
            pgvector_store.PgVectorConfig("postgresql://unused.invalid/dewey"), embedder, reranker=None)
        return store, embedder

    def test_malformed_author_stops_the_search_before_the_embedding_call(self):
        # The embedding call can cost money, and a failure there would hide the clear error.
        store, embedder = self.store()
        store.connect = lambda: self.fail("the store connected to the database")
        with self.assertRaises(base.MetadataError):
            store.retrieve({"question": "q", "authors": ["Jane Doe"]})
        self.assertEqual(embedder.calls, 0)

    def test_well_formed_request_embeds_the_question_then_opens_the_database(self):
        # The filter is built first, but a request it accepts still goes on to search.
        class Reached(Exception):
            pass

        def connect():
            raise Reached()

        store, embedder = self.store()
        store.connect = connect
        with self.assertRaises(Reached):
            store.retrieve(meta(["Jane Doe"], start="2024-01-01"))
        self.assertEqual(embedder.calls, 1)


@unittest.skipIf(pgvector_store is None, IMPORT_ERROR)
class PgvectorVersionWarningTests(unittest.TestCase):
    """A pgvector older than 0.8 has no iterative index scans, so date and byline searches can
    miss stories. PgVectorStore says so once per process, on stderr, and goes on searching.
    No database: a FakeConnection reports whichever version a test asks for."""

    FILTERED = meta(["Jane Doe"], start="2024-01-01", end="2024-12-31")
    WARNING = "Dewey: warning:"
    SET = "SET hnsw.iterative_scan = relaxed_order"
    FOUND = [FakeConnection.FOUND[1]]  # the URLs every search here returns
    # A version, the (major, minor) it is compared by, and the part of it Dewey repeats in messages.
    VERSIONS = [
        ("0.6.0", (0, 6), "0.6.0"), ("0.7.4", (0, 7), "0.7.4"), ("0.8", (0, 8), "0.8"),
        ("0.8.0", (0, 8), "0.8.0"), ("0.8.7", (0, 8), "0.8.7"), ("0.10.0", (0, 10), "0.10.0"),
        ("1", (1, 0), "1"), (" 0.8.1 ", (0, 8), "0.8.1"), ("0.8.7-2", (0, 8), "0.8.7"),
        ("0.8beta", (0, 8), "0.8"), ("0.6.0\x1b[31mRED", (0, 6), "0.6.0"),
        ("abc", None, None), ("", None, None), ("v0.8.1", None, None), (".8", None, None),
        (chr(0x0660) + ".8.7", None, None),  # an Arabic-Indic zero: int() reads it, pgvector never writes it
        (None, None, None), (0.8, None, None), (b"0.8.7", None, None),
    ]

    def setUp(self):
        # Once per process: give each test the unused flag a new process starts with, and put the
        # real one back afterwards. register_vector() is patched out because it needs a real connection.
        for patcher in (mock.patch.object(pgvector_store, "_old_pgvector_warned", threading.Lock()),
                        mock.patch.object(pgvector_store, "register_vector")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def store(self, extversion, barrier=None):
        conn = FakeConnection(extversion, barrier)
        store = pgvector_store.PgVectorStore(
            pgvector_store.PgVectorConfig("postgresql://unused.invalid/dewey"), FakeEmbedder(), FakeReranker())
        store.connect = lambda: conn
        return store, conn

    def quietly(self, fn):
        """fn()'s result, then what it wrote to stdout and to stderr."""
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            result = fn()
        return result, out.getvalue(), err.getvalue()

    def urls(self, sources):
        return [json.loads(source)["url"] for source in sources]

    def test_old_pgvector_warns_once_on_stderr_and_searches_still_run(self):
        store, conn = self.store("0.6.0")
        found, out, err = self.quietly(lambda: [store.retrieve(self.FILTERED), store.retrieve(self.FILTERED)])
        # The guides in docs/ quote these first words, so that a reader can search for them.
        self.assertTrue(err.startswith(
            "Dewey: warning: the pgvector extension in this database is version 0.6.0, older than 0.8.\n"), err)
        for words in ("date and byline searches can miss stories", "Searches still run",
                      "Install pgvector 0.8 or newer on the database server, if it isn't there already.",
                      "as a Postgres superuser", "ALTER EXTENSION vector UPDATE;",
                      "'Upgrade pgvector' in docs/STACKS.md",
                      "once per run, at the first search (for the indexer: when indexing starts)"):
            self.assertIn(words, err)
        self.assertNotIn("each time Dewey starts", err)  # it comes at the first search, not at start-up
        self.assertEqual(err.count(self.WARNING), 1)  # once, not once per search
        self.assertEqual(out, "")  # stdout is the MCP server's JSON-RPC channel
        self.assertEqual([self.urls(sources) for sources in found], [self.FOUND, self.FOUND])
        self.assertEqual(conn.count("WITH semantic"), 2)  # both searches ran
        self.assertEqual(conn.count("SET hnsw"), 0)  # the setting doesn't exist before 0.8

    def test_no_warning_on_0_8_or_newer(self):
        for extversion in ("0.8", "0.8.0", "0.8.7", "0.10.0", "1.0.0"):
            with self.subTest(extversion=extversion):
                store, conn = self.store(extversion)
                found, out, err = self.quietly(
                    lambda: [store.retrieve(self.FILTERED), store.retrieve(self.FILTERED)])
                self.assertEqual((out, err), ("", ""))
                self.assertEqual(conn.count(self.SET), 2)  # on every connection, as before
                self.assertEqual([self.urls(sources) for sources in found], [self.FOUND, self.FOUND])
        self.assertFalse(pgvector_store._old_pgvector_warned.locked())  # the one warning is still unused

    def test_one_warning_per_process_not_per_store_or_per_search(self):
        # The indexer's path and the search path share the one warning, and so do two stores.
        indexer, _ = self.store("0.7.4")
        searcher, _ = self.store("0.7.4")
        (chunks, first, second), out, err = self.quietly(
            lambda: (indexer.index_documents([]), searcher.retrieve(self.FILTERED), searcher.retrieve(meta())))
        self.assertEqual((chunks, self.urls(first), self.urls(second)), (0, self.FOUND, self.FOUND))
        self.assertEqual(err.count(self.WARNING), 1)
        self.assertIn("is version 0.7.4, older than 0.8.", err)
        self.assertEqual(out, "")

    def test_searches_arriving_together_get_one_warning(self):
        # The MCP server and the demo page run searches in worker threads. Every thread is held at
        # the version query until the last has arrived, and stderr is slow: code that printed
        # before it marked the warning as given would let them all through.
        class SlowStream(io.StringIO):
            def write(self, text):
                time.sleep(0.005)
                return super().write(text)

        count = 8
        store, _ = self.store("0.6.0", barrier=threading.Barrier(count))
        found, errors = [], []

        def search():
            try:
                found.append(self.urls(store.retrieve(self.FILTERED)))
            except Exception as e:  # collected: an exception in a thread would otherwise go unseen
                errors.append(e)

        out, err = io.StringIO(), SlowStream()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            threads = [threading.Thread(target=search) for _ in range(count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(found, [self.FOUND] * count)
        self.assertEqual(err.getvalue().count(self.WARNING), 1)
        self.assertEqual(out.getvalue(), "")

    def test_a_version_dewey_cannot_read_warns_instead_of_failing_the_search(self):
        # Such a version used to raise ValueError out of every search. It is treated as older
        # than 0.8, and nothing of it is repeated.
        for extversion in ("zzz\x1b[31m", "", "v0.8.1", None):
            with self.subTest(extversion=extversion), \
                    mock.patch.object(pgvector_store, "_old_pgvector_warned", threading.Lock()):
                store, conn = self.store(extversion)
                found, out, err = self.quietly(lambda: store.retrieve(self.FILTERED))
                self.assertEqual(self.urls(found), self.FOUND)
                self.assertTrue(err.startswith("Dewey: warning: Dewey can't read the version of the pgvector "
                                               "extension in this database, so it treats it as older than 0.8.\n"),
                                err)
                self.assertEqual(err.count(self.WARNING), 1)
                for raw in ("zzz", "\x1b", "v0.8.1"):
                    self.assertNotIn(raw, err)
                self.assertEqual(conn.count("SET hnsw"), 0)
                self.assertEqual(out, "")

    def test_only_the_numbers_of_a_version_are_repeated(self):
        # extversion comes from the database. Whatever follows its numbers stays out of terminals and logs.
        store, _ = self.store("0.6.0\x1b[31mRED\nDewey: warning: all is well")
        _, _, err = self.quietly(lambda: store.retrieve(self.FILTERED))
        self.assertIn("is version 0.6.0, older than 0.8.\n", err)
        self.assertNotIn("\x1b", err)
        self.assertNotIn("RED", err)
        self.assertEqual(err.count(self.WARNING), 1)

    def test_a_warning_that_cannot_be_written_does_not_stop_the_search(self):
        class BrokenPipe(io.StringIO):
            def write(self, text):
                raise BrokenPipeError(32, "Broken pipe")

        closed = io.StringIO()
        closed.close()
        # With no stderr at all, print(file=None) would write to stdout.
        for name, stream in (("closed", closed), ("broken pipe", BrokenPipe()), ("none", None)):
            with self.subTest(stderr=name), \
                    mock.patch.object(pgvector_store, "_old_pgvector_warned", threading.Lock()):
                store, conn = self.store("0.6.0")
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(stream):
                    found = [store.retrieve(self.FILTERED), store.retrieve(self.FILTERED)]
                self.assertEqual([self.urls(sources) for sources in found], [self.FOUND, self.FOUND])
                self.assertEqual(conn.count("WITH semantic"), 2)
                self.assertEqual(out.getvalue(), "")

    def test_versions_are_read_as_numbers(self):
        for text, version, shown in self.VERSIONS:
            with self.subTest(text=text):
                self.assertEqual(pgvector_store.pgvector_version(text), version)
                self.assertEqual(pgvector_store.pgvector_version_text(text), shown)
        self.assertEqual(pgvector_store.MIN_PGVECTOR, (0, 8))
        self.assertGreater(pgvector_store.pgvector_version("0.10.0"), pgvector_store.MIN_PGVECTOR)  # not text: "0.10" < "0.8"
        self.assertLess(pgvector_store.pgvector_version("0.7.4"), pgvector_store.MIN_PGVECTOR)
        pgvector_store.pgvector_version("9" * 5000)  # int() refuses more than 4,300 digits: this must not raise

    def test_the_wizard_reads_versions_the_same_way(self):
        # install.py is standard library only and runs before the packages this module needs are
        # installed, so it carries its own copy of the rule. This keeps the two from drifting apart.
        spec = importlib.util.spec_from_file_location("dewey_install_pgvector", REPO / "install.py")
        install = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = install  # dataclasses look their module up here
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(install)
        self.assertEqual(install.MIN_PGVECTOR, pgvector_store.MIN_PGVECTOR)
        for text, _, _ in self.VERSIONS + [("9" * 5000, None, None)]:
            with self.subTest(text=repr(text)[:40]):
                self.assertEqual(install.pgvector_version(text), pgvector_store.pgvector_version(text))
                self.assertEqual(install.pgvector_version_text(text), pgvector_store.pgvector_version_text(text))


@unittest.skipIf(pgvector_store is None, IMPORT_ERROR)
class DatabaseUrlCheckTests(unittest.TestCase):
    """A DATABASE_URL psycopg can't parse is refused at startup, without repeating psycopg's
    message: it quotes the unparseable part, usually the password."""

    BAD = [("postgresql://dewey:Tr4ce%Secret@db.example.org:5432/dewey", "Tr4ce%Secret"),
           ("postgresql://dewey:S3cr3t Pass@db.example.org/dewey", "S3cr3t Pass"),
           ("postgres:/dewey:S3cr3tPass@db.example.org/dewey", "S3cr3tPass"),
           ("'postgresql://dewey:GoodPass123@db.example.org/dewey'", "GoodPass123")]

    def test_valid_urls_pass(self):
        import providers

        for url in ["postgresql://dewey:GoodPass123@db:5432/dewey?sslmode=require",
                    "postgresql://dewey:Good%25Pass@db/dewey",
                    "postgresql://postgres@/dewey?host=/var/run/postgresql",
                    "host=db user=dewey password=x dbname=dewey"]:
            with self.subTest(url=url):
                providers.check_database_url(url)

    def test_malformed_urls_are_refused_without_the_password(self):
        import providers

        for url, secret in self.BAD:
            with self.subTest(url=url), self.assertRaises(providers.StackConfigError) as cm:
                providers.check_database_url(url)
            self.assertNotIn(secret, str(cm.exception))
            self.assertIn("percent-encoded", str(cm.exception))

    def test_build_pgvector_store_checks_it(self):
        from unittest import mock

        import providers

        url, secret = self.BAD[0]
        env = {"DEWEY_STACK": "anthropic", "VOYAGE_API_KEY": "test-key", "DATABASE_URL": url}
        with mock.patch.dict(os.environ, env, clear=True), \
                self.assertRaises(providers.StackConfigError) as cm:
            providers.build_pgvector_store()
        self.assertNotIn(secret, str(cm.exception))


ARTICLES = [
    # id, authors, publish_date (UTC)
    ("a1", ["Jane Doe"], "2024-03-01T12:00:00Z"),
    ("a2", ["JANE  DOE"], "2024-05-01T12:00:00Z"),
    ("a3", ["Jane Doe ", "Sam Lee"], "2023-12-31T23:30:00Z"),
    ("a4", ["John Doe"], "2024-06-01T12:00:00Z"),
    ("a5", [], "2024-06-02T12:00:00Z"),
    ("a6", ["José Núñez"], "2024-07-01T12:00:00Z"),
    ("a7", ["Jane Doering"], "2024-07-02T12:00:00Z"),
]
DIM = 3


class PrivateSchemaMixin:
    """Dewey's schema, built with schema_sql() in a schema of its own that is dropped afterwards."""

    @classmethod
    def open_private_schema(cls, dim=DIM):
        cls.conn = psycopg.connect(DATABASE_URL, autocommit=True)
        cls.schema = f"dewey_test_{os.getpid()}_{random.randrange(10**6)}"
        cls.conn.execute(f"CREATE SCHEMA {cls.schema}")
        cls.conn.execute(f"SET search_path TO {cls.schema}, public")
        for stmt in pgvector_store.schema_sql(dim):
            cls.conn.execute(stmt)
        register_vector(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.execute(f"DROP SCHEMA {cls.schema} CASCADE")
        cls.conn.close()


@unittest.skipIf(pgvector_store is None, IMPORT_ERROR)
@unittest.skipUnless(DATABASE_URL, "set DEWEY_TEST_DATABASE_URL to a throwaway Postgres with pgvector to run")
class PostgresFilterTests(PrivateSchemaMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.open_private_schema()
        for i, (aid, authors, published) in enumerate(ARTICLES):
            cls.conn.execute("INSERT INTO articles (id, headline, url, authors, publish_date, content) "
                             "VALUES (%s, %s, %s, %s, %s, %s)",
                             (aid, f"Headline {aid}", f"https://news.example.org/{aid}", authors, published,
                              "city council budget vote"))
            cls.conn.execute("INSERT INTO chunks (chunk_id, article_id, chunk_index, content, embedding) "
                             "VALUES (%s, %s, 0, %s, %s)",
                             (f"{aid}_0", aid, "city council budget vote",
                              np.asarray([1.0, 0.1 * i, 0.0], dtype=np.float32)))
        cls.conn.execute("ANALYZE articles")
        cls.unicode_lower = cls.conn.execute("SELECT lower('É') = 'é'").fetchone()[0]

    def ids(self, metadata):
        where, params = build_where(metadata)
        rows = self.conn.execute(f"SELECT a.id FROM articles a WHERE {where} ORDER BY a.id", params).fetchall()
        return [r[0] for r in rows]

    def test_tables_are_in_the_private_schema(self):
        schema = self.conn.execute("SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                                   "WHERE c.oid = 'articles'::regclass").fetchone()[0]
        self.assertEqual(schema, self.schema)

    def test_author_filter_can_use_an_index(self):
        # Without an index the planner can only check bylines after the HNSW scan has picked its
        # nearest candidates, which loses a rare author's stories (see PostgresRecallTests).
        where, params = build_where(meta(["jane doe"]))
        self.conn.execute("SET enable_seqscan = off")
        try:
            plan = "\n".join(r[0] for r in self.conn.execute(f"EXPLAIN SELECT a.id FROM articles a WHERE {where}",
                                                              params).fetchall())
        finally:
            self.conn.execute("RESET enable_seqscan")
        self.assertIn("articles_byline_keys_idx", plan)

    def test_byline_ignores_case_and_spacing(self):
        self.assertEqual(self.ids(meta(["jane doe"])), ["a1", "a2", "a3"])
        self.assertEqual(self.ids(meta(["  JANE   doe "])), ["a1", "a2", "a3"])
        self.assertEqual(self.ids(meta([" Sam   LEE "])), ["a3"])

    def test_no_partial_matches(self):
        self.assertEqual(self.ids(meta(["Jane"])), [])
        self.assertEqual(self.ids(meta(["Doe"])), [])

    def test_any_listed_author(self):
        self.assertEqual(self.ids(meta(["jane doe", "john doe"])), ["a1", "a2", "a3", "a4"])

    def test_accented_names(self):
        self.assertEqual(self.ids(meta(["José Núñez"])), ["a6"])
        if not self.unicode_lower:
            self.skipTest("this database's LC_CTYPE is C: lower() folds only A-Z")
        self.assertEqual(self.ids(meta(["JOSÉ NÚÑEZ"])), ["a6"])

    def test_hostile_name_is_just_a_name(self):
        self.assertEqual(self.ids(meta(["x'); DROP TABLE articles; --"])), [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM articles").fetchone()[0], len(ARTICLES))

    def test_dates_still_filter(self):
        self.assertEqual(self.ids(meta(start="2024-06-01")), ["a4", "a5", "a6", "a7"])
        self.assertEqual(self.ids(meta(end="2023-12-31")), ["a3"])  # inclusive end, in UTC
        self.assertEqual(self.ids(meta(["jane doe"], start="2024-01-01", end="2024-04-30")), ["a1"])

    def test_hybrid_query_applies_the_filter(self):
        where, params = build_where(meta(["jane doe"]))
        params.update(qvec=np.asarray([1.0, 0.0, 0.0], dtype=np.float32), qtext="council budget",
                      k=pgvector_store.CANDIDATES_PER_METHOD, rrf_k=pgvector_store.RRF_K)
        rows = self.conn.execute(HYBRID_SQL.format(where=where), params).fetchall()
        # row is (content, url, headline, authors, publish_date, publisher); index rather
        # than unpack, so another column added to HYBRID_SQL does not break this.
        self.assertEqual(sorted(row[1].rsplit("/", 1)[1] for row in rows), ["a1", "a2", "a3"])

    def test_index_check_names_a_missing_byline_function(self):
        store = pgvector_store.PgVectorStore(pgvector_store.PgVectorConfig(DATABASE_URL), embedder=None,
                                             reranker=None)
        store._expected_index_info = lambda: {}
        self.conn.execute("CREATE TEMP TABLE index_info (key text PRIMARY KEY, value text NOT NULL)")
        # Only the temporary schema: functions are never looked up there, so no dewey_byline_keys is visible.
        self.conn.execute("SET search_path TO pg_temp")
        try:
            with self.assertRaisesRegex(pgvector_store.IndexMismatchError, "setup_postgres.py"):
                store.check_index(self.conn)
        finally:
            self.conn.execute(f"SET search_path TO {self.schema}, public")
            self.conn.execute("DROP TABLE pg_temp.index_info")

    def test_register_handles_the_pgvector_this_database_has(self):
        # The real _register() against whatever pgvector this database has. Written to pass on
        # 0.8 or newer (iterative scans switched on, nothing printed) and on an older one (no SET,
        # one warning on stderr); on both, a filtered search then runs on the same connection.
        extversion = self.conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()[0]
        new_enough = tuple(int(n) for n in extversion.split(".")[:2]) >= (0, 8)  # real ones are plain numbers
        store = pgvector_store.PgVectorStore(pgvector_store.PgVectorConfig(DATABASE_URL), embedder=None,
                                             reranker=None)
        where, params = build_where(meta(["jane doe"], start="2024-01-01"))
        params.update(qvec=np.asarray([1.0, 0.0, 0.0], dtype=np.float32), qtext="council budget",
                      k=pgvector_store.CANDIDATES_PER_METHOD, rrf_k=pgvector_store.RRF_K)
        # A connection of its own, as every search has: _register() changes a setting of the session.
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            conn.execute(f"SET search_path TO {self.schema}, public")
            with mock.patch.object(pgvector_store, "_old_pgvector_warned", threading.Lock()), \
                    contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                store._register(conn)
                store._register(conn)  # a later search in the same process
            rows = conn.execute(HYBRID_SQL.format(where=where), params).fetchall()
            setting = conn.execute("SHOW hnsw.iterative_scan").fetchone()[0] if new_enough else None
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(sorted(row[1].rsplit("/", 1)[1] for row in rows), ["a1", "a2"])
        if new_enough:
            self.assertEqual(setting, "relaxed_order")
            self.assertEqual(err.getvalue(), "")
        else:
            self.assertEqual(err.getvalue().count("Dewey: warning:"), 1)
            self.assertIn(f"is version {extversion}, older than 0.8.", err.getvalue())


@unittest.skipIf(pgvector_store is None, IMPORT_ERROR)
@unittest.skipUnless(DATABASE_URL, "set DEWEY_TEST_DATABASE_URL to a throwaway Postgres with pgvector to run")
class PostgresRecallTests(PrivateSchemaMixin, unittest.TestCase):
    """A rare byline in a bigger archive: vector search must still find every one of its stories."""

    ARCHIVE, RARE = 5000, 10
    DIM = 8

    @classmethod
    def setUpClass(cls):
        cls.open_private_schema(cls.DIM)
        rng, names = np.random.default_rng(7), [f"Writer {i}" for i in range(3000)]
        with cls.conn.cursor() as cur:
            with cur.copy("COPY articles (id, headline, url, authors, publish_date, content) FROM STDIN") as cp:
                for i in range(cls.ARCHIVE):
                    authors = ["Rare  Writer"] if i % (cls.ARCHIVE // cls.RARE) == 0 else [names[i % len(names)]]
                    cp.write_row((f"r{i}", "h", f"https://news.example.org/r{i}", authors, "2024-01-01T00:00:00Z",
                                  "city council budget vote"))
            with cur.copy("COPY chunks (chunk_id, article_id, chunk_index, content, embedding) "
                          "FROM STDIN WITH (FORMAT BINARY)") as cp:
                cp.set_types(["text", "text", "int4", "text", "vector"])
                for i in range(cls.ARCHIVE):
                    cp.write_row((f"r{i}_0", f"r{i}", 0, "city council budget vote",
                                  rng.standard_normal(cls.DIM).astype(np.float32)))
        cls.conn.execute("ANALYZE articles")
        cls.conn.execute("ANALYZE chunks")
        cls.qvec = rng.standard_normal(cls.DIM).astype(np.float32)

    def test_rare_author_is_found_by_vector_search(self):
        where, params = build_where(meta(["rare writer"]))
        # A query that matches no words, so every row comes from the vector (semantic) half.
        params.update(qvec=self.qvec, qtext="zzzqqq", k=pgvector_store.CANDIDATES_PER_METHOD,
                      rrf_k=pgvector_store.RRF_K)
        rows = self.conn.execute(HYBRID_SQL.format(where=where), params).fetchall()
        self.assertEqual(len(rows), self.RARE)
        self.assertEqual({tuple(r[3]) for r in rows}, {("Rare  Writer",)})


if __name__ == "__main__":
    unittest.main()

"""
Tests for the Postgres store's search filters (app/providers/pgvector_store.py).

The unit tests need the packages in requirements.txt but no database. The
PostgresFilterTests run the SQL for real against a throwaway Postgres with
pgvector, only when DEWEY_TEST_DATABASE_URL is set, e.g.

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
import io  # noqa: E402
import os  # noqa: E402
import random  # noqa: E402
import unittest  # noqa: E402

try:
    import numpy as np
    import psycopg
    from pgvector.psycopg import register_vector

    from providers import pgvector_store
    from providers.pgvector_store import HYBRID_SQL, build_where
except ImportError as e:  # pragma: no cover
    pgvector_store = None
    IMPORT_ERROR = f"needs the packages in requirements.txt ({e})"
else:
    IMPORT_ERROR = ""

DATABASE_URL = os.environ.get("DEWEY_TEST_DATABASE_URL", "").strip()


def meta(authors=(), start=None, end=None):
    return {"question": "q", "date_range": {"start_date": start, "end_date": end},
            "authors": [{"name": a} for a in authors]}


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
            {"name": "  jane   doe "}, {"name": "Jane Doe"}, {"name": "   "}, {"name": None}, {}, "Sam Lee",
            {"name": "Sam\tLee"}]})
        self.assertEqual(params, {"authors": ["jane doe", "Sam Lee"]})
        self.assertEqual(build_where(meta(["", " "])), ("TRUE", {}))

    def test_authors_and_dates_combine(self):
        where, params = build_where(meta(["Jane Doe"], start="2024-01-01"))
        self.assertTrue(where.startswith("a.publish_date >= "))
        self.assertTrue(where.endswith(pgvector_store.AUTHOR_MATCH_SQL))
        self.assertEqual(set(params), {"start_date", "authors"})


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

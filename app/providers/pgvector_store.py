"""
Postgres + pgvector archive store. Embeddings and reranking are pluggable
(hosted Voyage AI or open-weight models run locally); see embeddings.py.

This is the non-Azure replacement for Azure AI Search. It reproduces the
original pipeline step by step:

    Azure AI Search                       This module
    ------------------------------------  ---------------------------------------
    SplitSkill (512 tokens, 96 overlap)   chunk_text() - word-based approximation
    AzureOpenAIEmbeddingSkill             Embedder.embed(input_type="document")
    keyword search (search_text, "any")   Postgres full-text search, OR-ed terms
    vector search (k=50)                  pgvector cosine distance (k=50)
    hybrid fusion                         reciprocal rank fusion (RRF, k=60)
    semantic ranker, top 10               Reranker.rank(), top 10
    OData date/author filters             SQL WHERE with bound parameters
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import date, timezone
from typing import Any, Dict, Iterable, List, Sequence

import numpy as np
import psycopg
from dateutil.parser import parse
from pgvector.psycopg import register_vector

from .embeddings import Embedder, Reranker

# Azure's SplitSkill used 512 tokens with 96 overlap. English prose averages
# roughly 0.75 words per token, so ~380/70 words is an approximation, not an
# exact match; change it here if you need different chunking.
CHUNK_WORDS = 380
CHUNK_OVERLAP_WORDS = 70

CANDIDATES_PER_METHOD = 50  # matches k_nearest_neighbors=50 in the Azure path
RRF_K = 60
TOP_N = 10                  # matches top=10 in the Azure path


@dataclass
class PgVectorConfig:
    database_url: str


class IndexMismatchError(Exception):
    """The database was indexed with a different embedding model or dimension."""


def schema_sql(embed_dim: int) -> List[str]:
    return [
        "CREATE EXTENSION IF NOT EXISTS vector",
        # Records which embedding model built the index: vectors from two
        # different models are not comparable, so mixing them would silently
        # return wrong results.
        """
        CREATE TABLE IF NOT EXISTS index_info (
            key   text PRIMARY KEY,
            value text NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS articles (
            id           text PRIMARY KEY,
            headline     text NOT NULL,
            url          text NOT NULL,
            authors      text[] NOT NULL DEFAULT '{}',
            publish_date timestamptz NOT NULL,
            content      text NOT NULL,
            -- Who produced the article. Empty on rows indexed before this column
            -- existed; the newsroom's own archive is filled in by setup_postgres.
            publisher    text NOT NULL DEFAULT ''
        )
        """,
        # CREATE TABLE IF NOT EXISTS leaves an existing table alone, so upgrade it.
        "ALTER TABLE articles ADD COLUMN IF NOT EXISTS publisher text NOT NULL DEFAULT ''",
        f"""
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id    text PRIMARY KEY,
            article_id  text NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
            chunk_index integer NOT NULL,
            content     text NOT NULL,
            embedding   vector({embed_dim}) NOT NULL,
            tsv         tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
        )
        """,
        "CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops)",
        "CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv)",
        "CREATE INDEX IF NOT EXISTS chunks_article_idx ON chunks (article_id)",
        BYLINE_KEYS_SQL,
        f"CREATE INDEX IF NOT EXISTS articles_byline_keys_idx ON articles USING gin ({BYLINE_KEYS_FUNCTION}(authors))",
        "CREATE INDEX IF NOT EXISTS articles_publish_date_idx ON articles (publish_date)",
    ]


def chunk_text(text: str, size: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP_WORDS) -> List[str]:
    words = text.split()
    if not words:
        return []
    step = size - overlap
    chunks = []
    for start in range(0, len(words), step):
        chunks.append(" ".join(words[start:start + size]))
        if start + size >= len(words):
            break
    return chunks


def article_id(doc: Dict[str, Any]) -> str:
    """Use the document's own id; otherwise derive a stable one from its URL."""
    if doc.get("id"):
        return str(doc["id"])
    return "url-" + hashlib.sha1(doc["url"].encode("utf-8")).hexdigest()[:16]


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


# How a byline name is compared, applied in SQL to both the stored names and the
# requested ones: runs of whitespace (including the no-break space that pasted
# bylines often carry) become one space, the ends are trimmed, and the result is
# lower-cased. So "jane  doe" finds "Jane Doe". An E'' string, so the backslashes
# mean the same whatever standard_conforming_strings is set to. lower() follows
# the database's LC_CTYPE: in a database created with the plain "C" locale only
# A-Z are case-folded (tested: C.UTF-8 folds accented letters too).
_NORMALIZED_NAME = "lower(btrim(regexp_replace({}, E'[\\\\s\\\\u00a0]+', ' ', 'g')))"
# The same, over a whole byline array. IMMUTABLE so that articles can have a GIN
# index on it: without one, an author filter can only be checked after the HNSW
# scan has picked its nearest candidates, and a rare author's stories are lost
# from the vector half of the search. Changing the body changes what the index
# holds: drop and re-create articles_byline_keys_idx after any change.
BYLINE_KEYS_FUNCTION = "dewey_byline_keys"
BYLINE_KEYS_SQL = (
    f"CREATE OR REPLACE FUNCTION {BYLINE_KEYS_FUNCTION}(names text[]) RETURNS text[] "
    "LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE "
    f"AS $$ SELECT ARRAY(SELECT {_NORMALIZED_NAME.format('n')} FROM unnest(names) AS n) $$"
)
AUTHOR_MATCH_SQL = f"{BYLINE_KEYS_FUNCTION}(a.authors) && {BYLINE_KEYS_FUNCTION}(%(authors)s::text[])"


def _clean_author_names(authors: Any) -> List[str]:
    """Requested byline names, whitespace-collapsed, without blanks or case-insensitive repeats."""
    names, seen = [], set()
    for author in authors or []:
        name = author.get("name") if isinstance(author, dict) else None
        if not isinstance(name, str):
            continue
        name = " ".join(name.split())  # str.split() also splits on no-break and other Unicode spaces
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            names.append(name)
    return names


def build_where(metadata: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """SQL filter on `articles a`, equivalent to the Azure OData filter. Values are bound, never interpolated."""
    clauses, params = [], {}
    date_range = dict(metadata.get("date_range") or {})
    for key in ("start_date", "end_date"):
        # The model writes these; drop anything that isn't a YYYY-MM-DD date
        # rather than letting the SQL cast fail the whole request.
        if date_range.get(key) and not _is_iso_date(date_range[key]):
            print(f"Ignoring unparseable {key}: {date_range[key]!r}")
            date_range[key] = None
    if date_range.get("start_date"):
        clauses.append("a.publish_date >= (%(start_date)s::date)::timestamp AT TIME ZONE 'UTC'")
        params["start_date"] = date_range["start_date"]
    if date_range.get("end_date"):
        # Inclusive end date in UTC, like `le {end}T23:59:59Z` in the Azure path.
        clauses.append("a.publish_date < (%(end_date)s::date + 1)::timestamp AT TIME ZONE 'UTC'")
        params["end_date"] = date_range["end_date"]
    authors = _clean_author_names(metadata.get("authors"))
    if authors:
        # Any listed author, ignoring case and spacing (see _NORMALIZED_NAME), through the
        # articles_byline_keys_idx index. The Azure path's `a eq '...'` is exact; a journalist
        # typing "jane doe" means Jane Doe.
        clauses.append(AUTHOR_MATCH_SQL)
        params["authors"] = authors
    return (" AND ".join(clauses) if clauses else "TRUE"), params


HYBRID_SQL = """
WITH semantic AS (
    SELECT c.chunk_id,
           row_number() OVER (ORDER BY c.embedding <=> %(qvec)s) AS rank
    FROM chunks c JOIN articles a ON a.id = c.article_id
    WHERE {where}
    ORDER BY c.embedding <=> %(qvec)s
    LIMIT %(k)s
),
keyword AS (
    SELECT c.chunk_id,
           row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q.query) DESC) AS rank
    FROM chunks c
    JOIN articles a ON a.id = c.article_id,
    -- OR the query terms together, like Azure's default searchMode "any";
    -- plainto_tsquery alone would AND every word of a full-sentence question.
    LATERAL (SELECT NULLIF(replace(plainto_tsquery('english', %(qtext)s)::text, '&', '|'), '')::tsquery AS query) q
    WHERE q.query IS NOT NULL AND c.tsv @@ q.query AND {where}
    ORDER BY ts_rank_cd(c.tsv, q.query) DESC
    LIMIT %(k)s
),
fused AS (
    SELECT chunk_id, sum(1.0 / (%(rrf_k)s + rank)) AS score
    FROM (SELECT * FROM semantic UNION ALL SELECT * FROM keyword) ranked
    GROUP BY chunk_id
)
SELECT c.content, a.url, a.headline, a.authors, a.publish_date, a.publisher
FROM fused f
JOIN chunks c ON c.chunk_id = f.chunk_id
JOIN articles a ON a.id = c.article_id
ORDER BY f.score DESC
LIMIT %(k)s
"""


class PgVectorStore:
    def __init__(self, config: PgVectorConfig, embedder: Embedder, reranker: Reranker):
        self.config = config
        self.embedder = embedder
        self.reranker = reranker
        self._index_checked = False

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.config.database_url, autocommit=True)

    def _register(self, conn: psycopg.Connection) -> None:
        register_vector(conn)
        # pgvector >= 0.8 can keep scanning the HNSW index when a WHERE filter
        # discards candidates; without it, selective date/author filters can
        # return fewer than k vector matches.
        row = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
        if row and tuple(int(p) for p in row[0].split(".")[:2]) >= (0, 8):
            conn.execute("SET hnsw.iterative_scan = relaxed_order")

    # ---------- setup / indexing ----------

    def _expected_index_info(self) -> Dict[str, str]:
        return {"embed_model": self.embedder.model, "embed_dim": str(self.embedder.dim)}

    def check_index(self, conn: psycopg.Connection) -> None:
        """Raise if the database was built with a different embedding model/dimension."""
        if self._index_checked:
            return
        try:
            stored = dict(conn.execute("SELECT key, value FROM index_info").fetchall())
        except psycopg.errors.UndefinedTable:
            raise IndexMismatchError(
                "No Dewey index found in this database. Run `python app/setup_postgres.py` first."
            ) from None
        expected = self._expected_index_info()
        diffs = {k: (stored.get(k), v) for k, v in expected.items() if stored.get(k) != v}
        if diffs:
            detail = ", ".join(f"{k}: index has {old!r}, config asks for {new!r}" for k, (old, new) in diffs.items())
            raise IndexMismatchError(
                f"Embedding settings don't match this database ({detail}). Either restore the "
                "original EMBEDDING_PROVIDER/EMBED_MODEL/EMBED_DIM settings, or index into a fresh "
                "database with `python app/setup_postgres.py`."
            )
        # A constant from this module, not a value: inlined because to_regprocedure takes cstring before PG 16.
        if conn.execute(f"SELECT to_regprocedure('{BYLINE_KEYS_FUNCTION}(text[])')").fetchone()[0] is None:
            raise IndexMismatchError(
                f"This database has no {BYLINE_KEYS_FUNCTION}() function, which author filters use: it was "
                "set up by an earlier version of Dewey. Run `python app/setup_postgres.py` again to add it "
                "and its index (this also embeds the articles in data/ again)."
            )
        self._index_checked = True

    def create_schema(self) -> None:
        with self.connect() as conn:
            for stmt in schema_sql(self.embedder.dim):
                conn.execute(stmt)
            for key, value in self._expected_index_info().items():
                conn.execute(
                    "INSERT INTO index_info (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                    (key, value),
                )
            self.check_index(conn)

    def embed_documents(self, texts: Sequence[str], batch_size: int = 64) -> List[List[float]]:
        vectors: List[List[float]] = []
        for i in range(0, len(texts), batch_size):
            vectors.extend(self.embedder.embed(texts[i:i + batch_size], input_type="document"))
        return vectors

    def index_documents(self, documents: Iterable[Dict[str, Any]]) -> int:
        """Upsert articles and replace their chunks. Returns the number of chunks written."""
        total = 0
        with self.connect() as conn:
            self.check_index(conn)
            self._register(conn)
            for doc in documents:
                aid = article_id(doc)
                pieces = chunk_text(doc["content"])
                if not pieces:
                    continue
                # Prefix each chunk with its headline so the embedding carries
                # the article topic (Azure indexed headline as its own field).
                vectors = self.embed_documents([f"{doc['headline']}\n\n{p}" for p in pieces])
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO articles (id, headline, url, authors, publish_date, content, publisher)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            headline = EXCLUDED.headline, url = EXCLUDED.url,
                            authors = EXCLUDED.authors, publish_date = EXCLUDED.publish_date,
                            content = EXCLUDED.content, publisher = EXCLUDED.publisher
                        """,
                        (aid, doc["headline"], doc["url"], list(doc["authors"]),
                         parse(doc["publish_date"]), doc["content"],
                         str(doc.get("publisher") or "").strip()),
                    )
                    conn.execute("DELETE FROM chunks WHERE article_id = %s", (aid,))
                    with conn.cursor() as cur:
                        cur.executemany(
                            "INSERT INTO chunks (chunk_id, article_id, chunk_index, content, embedding) "
                            "VALUES (%s, %s, %s, %s, %s)",
                            [
                                (f"{aid}_{i}", aid, i, piece, np.asarray(vec, dtype=np.float32))
                                for i, (piece, vec) in enumerate(zip(pieces, vectors))
                            ],
                        )
                total += len(pieces)
        return total

    # ---------- query time ----------

    def retrieve(self, metadata: Dict[str, Any]) -> List[str]:
        question = metadata["question"]
        qvec = self.embedder.embed([question], input_type="query")[0]

        where, params = build_where(metadata)
        params.update(qvec=np.asarray(qvec, dtype=np.float32), qtext=question,
                      k=CANDIDATES_PER_METHOD, rrf_k=RRF_K)

        with self.connect() as conn:
            self.check_index(conn)
            self._register(conn)
            rows = conn.execute(HYBRID_SQL.format(where=where), params).fetchall()

        if not rows:
            return []

        order = self.reranker.rank(
            question,
            [f"{headline}\n\n{content}" for content, _, headline, _, _, _ in rows],
            top_k=min(TOP_N, len(rows)),
        )

        sources = []
        for index in order:
            content, url, headline, authors, publish_date, publisher = rows[index]
            sources.append(json.dumps({
                "url": url,
                "publisher": publisher,
                "publish_date": publish_date.astimezone(timezone.utc).date().isoformat(),
                "authors": list(authors),
                "headline": headline,
                "content": content.replace("\n", " ").replace("\r", " "),
            }))
        return sources

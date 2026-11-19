"""
Postgres + pgvector archive store with Voyage AI embeddings and reranking.

This is the non-Azure replacement for Azure AI Search. It reproduces the
original pipeline step by step:

    Azure AI Search                       This module
    ------------------------------------  ---------------------------------------
    SplitSkill (512 tokens, 96 overlap)   chunk_text() - word-based approximation
    AzureOpenAIEmbeddingSkill             Voyage `embed(input_type="document")`
    keyword search (search_text, "any")   Postgres full-text search, OR-ed terms
    vector search (k=50)                  pgvector cosine distance (k=50)
    hybrid fusion                         reciprocal rank fusion (RRF, k=60)
    semantic ranker, top 10               Voyage `rerank`, top 10
    OData date/author filters             SQL WHERE with bound parameters
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import date, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import psycopg
import voyageai
from dateutil.parser import parse
from pgvector.psycopg import register_vector

DEFAULT_EMBED_MODEL = "voyage-4"
DEFAULT_RERANK_MODEL = "rerank-3"
DEFAULT_EMBED_DIM = 1024  # voyage-4 default output dimension

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
    embed_model: str = DEFAULT_EMBED_MODEL
    rerank_model: str = DEFAULT_RERANK_MODEL
    embed_dim: int = DEFAULT_EMBED_DIM


def schema_sql(embed_dim: int) -> List[str]:
    return [
        "CREATE EXTENSION IF NOT EXISTS vector",
        """
        CREATE TABLE IF NOT EXISTS articles (
            id           text PRIMARY KEY,
            headline     text NOT NULL,
            url          text NOT NULL,
            authors      text[] NOT NULL DEFAULT '{}',
            publish_date timestamptz NOT NULL,
            content      text NOT NULL
        )
        """,
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
        "CREATE INDEX IF NOT EXISTS articles_authors_idx ON articles USING gin (authors)",
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
    authors = [a["name"] for a in metadata.get("authors") or [] if a.get("name")]
    if authors:
        # Exact, case-sensitive match on any listed author, like `a eq '...'` in Azure.
        clauses.append("a.authors && %(authors)s::text[]")
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
SELECT c.content, a.url, a.headline, a.authors, a.publish_date
FROM fused f
JOIN chunks c ON c.chunk_id = f.chunk_id
JOIN articles a ON a.id = c.article_id
ORDER BY f.score DESC
LIMIT %(k)s
"""


class PgVectorStore:
    def __init__(self, config: PgVectorConfig, voyage_client: Optional[voyageai.Client] = None):
        self.config = config
        # voyageai.Client() reads VOYAGE_API_KEY from the environment.
        self.voyage = voyage_client or voyageai.Client()

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

    def create_schema(self) -> None:
        with self.connect() as conn:
            for stmt in schema_sql(self.config.embed_dim):
                conn.execute(stmt)

    def embed_documents(self, texts: Sequence[str], batch_size: int = 64) -> List[List[float]]:
        vectors: List[List[float]] = []
        for i in range(0, len(texts), batch_size):
            result = self.voyage.embed(
                list(texts[i:i + batch_size]),
                model=self.config.embed_model,
                input_type="document",
                output_dimension=self.config.embed_dim,
            )
            vectors.extend(result.embeddings)
        return vectors

    def index_documents(self, documents: Iterable[Dict[str, Any]]) -> int:
        """Upsert articles and replace their chunks. Returns the number of chunks written."""
        total = 0
        with self.connect() as conn:
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
                        INSERT INTO articles (id, headline, url, authors, publish_date, content)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            headline = EXCLUDED.headline, url = EXCLUDED.url,
                            authors = EXCLUDED.authors, publish_date = EXCLUDED.publish_date,
                            content = EXCLUDED.content
                        """,
                        (aid, doc["headline"], doc["url"], list(doc["authors"]),
                         parse(doc["publish_date"]), doc["content"]),
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
        qvec = self.voyage.embed(
            [question],
            model=self.config.embed_model,
            input_type="query",
            output_dimension=self.config.embed_dim,
        ).embeddings[0]

        where, params = build_where(metadata)
        params.update(qvec=np.asarray(qvec, dtype=np.float32), qtext=question,
                      k=CANDIDATES_PER_METHOD, rrf_k=RRF_K)

        with self.connect() as conn:
            self._register(conn)
            rows = conn.execute(HYBRID_SQL.format(where=where), params).fetchall()

        if not rows:
            return []

        reranked = self.voyage.rerank(
            question,
            [f"{headline}\n\n{content}" for content, _, headline, _, _ in rows],
            model=self.config.rerank_model,
            top_k=min(TOP_N, len(rows)),
        )

        sources = []
        for r in reranked.results:
            content, url, headline, authors, publish_date = rows[r.index]
            sources.append(json.dumps({
                "url": url,
                "publish_date": publish_date.astimezone(timezone.utc).date().isoformat(),
                "authors": list(authors),
                "headline": headline,
                "content": content.replace("\n", " ").replace("\r", " "),
            }))
        return sources

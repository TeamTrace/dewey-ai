"""
Embedding and reranking backends for the Postgres store.

    EMBEDDING_PROVIDER=voyage  hosted Voyage AI API (needs VOYAGE_API_KEY)
    EMBEDDING_PROVIDER=local   open-weight voyage-4-nano (Apache 2.0) run on this
                               machine through the same `voyageai` SDK; no key.
                               Needs `pip install -r requirements-local.txt`.

    RERANK_PROVIDER=voyage     hosted Voyage AI rerank API (needs VOYAGE_API_KEY)
    RERANK_PROVIDER=local      open-weight cross-encoder run on this machine via
                               sentence-transformers (default BAAI/bge-reranker-v2-m3,
                               Apache 2.0). Needs requirements-local.txt.
    RERANK_PROVIDER=none       no reranking: keep the hybrid-search (RRF) order.

Local models are downloaded from Hugging Face on first use and cached there
(by default under ~/.cache/huggingface).
"""
from typing import List, Optional, Protocol, Sequence

EMBED_DEFAULTS = {"voyage": "voyage-4", "local": "voyage-4-nano"}
RERANK_DEFAULTS = {"voyage": "rerank-3", "local": "BAAI/bge-reranker-v2-m3", "none": ""}
DEFAULT_EMBED_DIM = 1024  # supported by voyage-4 (default) and voyage-4-nano


class Embedder(Protocol):
    model: str
    dim: int

    def embed(self, texts: Sequence[str], input_type: str) -> List[List[float]]:
        """input_type is "document" (indexing) or "query" (search)."""
        ...


class Reranker(Protocol):
    def rank(self, query: str, documents: Sequence[str], top_k: int) -> List[int]:
        """Return indices into `documents`, best first, at most top_k."""
        ...


class VoyageEmbedder:
    """
    Hosted Voyage models and the local voyage-4-nano share one code path: the
    `voyageai` SDK runs voyage-4-nano locally (no API key) and sends other
    model names to the hosted API.
    """

    def __init__(self, model: str, dim: int = DEFAULT_EMBED_DIM, client=None):
        import voyageai

        self.model = model
        self.dim = dim
        # The SDK only requires VOYAGE_API_KEY when a hosted model is called.
        self.client = client or voyageai.Client()

    def embed(self, texts: Sequence[str], input_type: str) -> List[List[float]]:
        result = self.client.embed(
            list(texts), model=self.model, input_type=input_type, output_dimension=self.dim
        )
        return result.embeddings


class VoyageReranker:
    def __init__(self, model: str, client=None):
        import voyageai

        self.model = model
        self.client = client or voyageai.Client()

    def rank(self, query: str, documents: Sequence[str], top_k: int) -> List[int]:
        result = self.client.rerank(query, list(documents), model=self.model, top_k=top_k)
        return [r.index for r in result.results]


class CrossEncoderReranker:
    """Open-weight cross-encoder via sentence-transformers' CrossEncoder.rank()."""

    def __init__(self, model: str, encoder=None):
        self.model = model
        self._encoder = encoder

    @property
    def encoder(self):
        if self._encoder is None:  # load lazily: the model is ~0.5B parameters
            from sentence_transformers import CrossEncoder

            self._encoder = CrossEncoder(self.model)
        return self._encoder

    def rank(self, query: str, documents: Sequence[str], top_k: int) -> List[int]:
        ranked = self.encoder.rank(query, list(documents), top_k=top_k)
        return [int(r["corpus_id"]) for r in ranked]


class NoReranker:
    """Keeps the order the hybrid search returned."""

    model = ""

    def rank(self, query: str, documents: Sequence[str], top_k: int) -> List[int]:
        return list(range(min(top_k, len(documents))))


def make_embedder(provider: str, model: Optional[str] = None, dim: int = DEFAULT_EMBED_DIM) -> Embedder:
    if provider not in EMBED_DEFAULTS:
        raise ValueError(f"EMBEDDING_PROVIDER must be one of {tuple(EMBED_DEFAULTS)}, got {provider!r}")
    return VoyageEmbedder(model or EMBED_DEFAULTS[provider], dim)


def make_reranker(provider: str, model: Optional[str] = None) -> Reranker:
    if provider == "voyage":
        return VoyageReranker(model or RERANK_DEFAULTS["voyage"])
    if provider == "local":
        return CrossEncoderReranker(model or RERANK_DEFAULTS["local"])
    if provider == "none":
        return NoReranker()
    raise ValueError(f"RERANK_PROVIDER must be one of {tuple(RERANK_DEFAULTS)}, got {provider!r}")

"""
Embedding and reranking backends for the Postgres store.

    EMBEDDING_PROVIDER=voyage  hosted Voyage AI API (needs VOYAGE_API_KEY)
    EMBEDDING_PROVIDER=local   open-weight voyage-4-nano (Apache 2.0) run on this
                               machine via sentence-transformers; no key, and
                               never a hosted model. Needs `pip install -r
                               requirements-local.txt`.

    RERANK_PROVIDER=voyage     hosted Voyage AI rerank API (needs VOYAGE_API_KEY)
    RERANK_PROVIDER=local      open-weight cross-encoder run on this machine via
                               sentence-transformers (default BAAI/bge-reranker-v2-m3,
                               Apache 2.0). Needs requirements-local.txt.
    RERANK_PROVIDER=none       no reranking: keep the hybrid-search (RRF) order.

Local models are downloaded from Hugging Face on first use and cached there
(by default under ~/.cache/huggingface). After that they are loaded from the
cache at the commit first downloaded (or at EMBED_MODEL_REVISION /
RERANK_MODEL_REVISION), without contacting Hugging Face: voyage-4-nano's repo
ships Python code that runs when the model loads, so a new version must never
be picked up on its own. See load_pinned().
"""
import os
import re
import threading
from typing import List, Optional, Protocol, Sequence

EMBED_DEFAULTS = {"voyage": "voyage-4", "local": "voyage-4-nano"}
RERANK_DEFAULTS = {"voyage": "rerank-3", "local": "BAAI/bge-reranker-v2-m3", "none": ""}
DEFAULT_EMBED_DIM = 1024  # supported by voyage-4 (default) and voyage-4-nano
# voyageai 0.6.0 defaults to max_retries=0, so a single HTTP 429 aborts indexing or search.
# Its SDK counts max_retries as the total number of attempts, so 4 gives three retries.
VOYAGE_MAX_RETRIES = 4
# The embedding models EMBEDDING_PROVIDER=local can run, and their Hugging Face repos. Any other
# name (voyage-4, voyage-4-large...) is a hosted Voyage AI model, which "local" must never call.
LOCAL_EMBED_MODELS = {"voyage-4-nano": "MongoDB/voyage-4-nano"}
_COMMIT = re.compile(r"[0-9a-f]{40}")


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
    """Hosted Voyage AI models (EMBEDDING_PROVIDER=voyage)."""

    def __init__(self, model: str, dim: int = DEFAULT_EMBED_DIM, client=None):
        if model in LOCAL_EMBED_MODELS:
            # The SDK would run it on this machine, from an unpinned download; LocalEmbedder pins it.
            raise ValueError(f"{model} runs on this machine: set EMBEDDING_PROVIDER=local to use it.")
        import voyageai

        self.model = model
        self.dim = dim
        self.client = client or voyageai.Client(max_retries=VOYAGE_MAX_RETRIES)

    def embed(self, texts: Sequence[str], input_type: str) -> List[List[float]]:
        result = self.client.embed(
            list(texts), model=self.model, input_type=input_type, output_dimension=self.dim
        )
        return result.embeddings


class VoyageReranker:
    def __init__(self, model: str, client=None):
        import voyageai

        self.model = model
        self.client = client or voyageai.Client(max_retries=VOYAGE_MAX_RETRIES)

    def rank(self, query: str, documents: Sequence[str], top_k: int) -> List[int]:
        result = self.client.rerank(query, list(documents), model=self.model, top_k=top_k)
        return [r.index for r in result.results]


class LocalEmbedder:
    """
    voyage-4-nano on this machine (EMBEDDING_PROVIDER=local), via sentence-transformers,
    run the way the voyageai SDK's local mode runs it: query/document prompts, a
    32k-token context, unit-length vectors truncated to `dim`. Loaded directly rather
    than through the SDK because the SDK sends any other model name to the hosted API,
    and can't pin the Hugging Face revision of a model whose repo ships code.
    """

    MAX_TOKENS = 32768
    FULL_DIM = 2048
    DIMS = (2048, 1024, 512, 256)

    def __init__(self, model: str = EMBED_DEFAULTS["local"], dim: int = DEFAULT_EMBED_DIM,
                 revision: Optional[str] = None, encoder=None):
        if model not in LOCAL_EMBED_MODELS:
            raise ValueError(
                f"EMBEDDING_PROVIDER=local can only run {', '.join(LOCAL_EMBED_MODELS)} on this machine, not "
                f"EMBED_MODEL={model!r}. Other Voyage models run on Voyage AI's servers: to use one, set "
                "EMBEDDING_PROVIDER=voyage (questions and articles are then sent to Voyage AI)."
            )
        if dim not in self.DIMS:
            raise ValueError(f"EMBED_DIM for {model} must be one of {self.DIMS}, got {dim}")
        self.model = model
        self.dim = dim
        self.revision = revision
        self._encoder = encoder
        # One model per process, shared by every request thread: loading and encoding are
        # serialized (the tokenizer isn't thread-safe; the SDK locks the same way).
        self._lock = threading.Lock()

    def _load(self):
        import torch
        from sentence_transformers import SentenceTransformer

        encoder = load_pinned(SentenceTransformer, LOCAL_EMBED_MODELS[self.model], self.revision,
                              trust_remote_code=True,  # the repo's modeling_*.py; see load_pinned()
                              device="cuda" if torch.cuda.is_available() else "cpu")  # as the SDK picks
        encoder.max_seq_length = self.MAX_TOKENS
        return encoder

    def embed(self, texts: Sequence[str], input_type: str) -> List[List[float]]:
        if input_type not in ("query", "document"):
            raise ValueError(f"input_type must be 'query' or 'document', got {input_type!r}")
        kwargs = {"normalize_embeddings": True}
        if self.dim != self.FULL_DIM:
            kwargs["truncate_dim"] = self.dim
        with self._lock:
            if self._encoder is None:
                self._encoder = self._load()
            encode = self._encoder.encode_query if input_type == "query" else self._encoder.encode_document
            return encode(list(texts), **kwargs).tolist()


class CrossEncoderReranker:
    """Open-weight cross-encoder via sentence-transformers' CrossEncoder.rank()."""

    def __init__(self, model: str, encoder=None, revision: Optional[str] = None):
        self.model = model
        self.revision = revision
        self._encoder = encoder

    @property
    def encoder(self):
        if self._encoder is None:  # load lazily: the model is ~0.5B parameters
            from sentence_transformers import CrossEncoder

            self._encoder = load_pinned(CrossEncoder, self.model, self.revision)
        return self._encoder

    def rank(self, query: str, documents: Sequence[str], top_k: int) -> List[int]:
        ranked = self.encoder.rank(query, list(documents), top_k=top_k)
        return [int(r["corpus_id"]) for r in ranked]


class NoReranker:
    """Keeps the order the hybrid search returned."""

    model = ""

    def rank(self, query: str, documents: Sequence[str], top_k: int) -> List[int]:
        return list(range(min(top_k, len(documents))))


def cached_commit(repo_id: str) -> Optional[str]:
    """The commit of `repo_id` already in the local Hugging Face cache (the one its 'main' pointed to when
    it was downloaded), or None."""
    if os.path.isdir(repo_id):  # a model folder on disk, not a Hub repo
        return None
    try:
        from huggingface_hub import try_to_load_from_cache

        cache_dir = os.getenv("SENTENCE_TRANSFORMERS_HOME") or None  # where sentence-transformers downloads to
        path = try_to_load_from_cache(repo_id, "config.json", cache_dir=cache_dir)
    except Exception:  # not installed, or not a valid repo id
        return None
    if isinstance(path, str):
        commit = os.path.basename(os.path.dirname(path))  # <cache>/models--org--name/snapshots/<commit>/config.json
        if _COMMIT.fullmatch(commit):
            return commit
    return None


def load_pinned(loader, repo_id: str, revision: Optional[str] = None, **kwargs):
    """
    loader(repo_id, revision=..., **kwargs) (SentenceTransformer or CrossEncoder) at a fixed revision.

    Without pinning, every process start re-resolved the repo's 'main' branch on Hugging Face and
    fetched any new version, including new code for a trust_remote_code model, into a process that
    holds every API key and sees journalists' questions. Instead the revision is `revision` when set
    (EMBED_MODEL_REVISION / RERANK_MODEL_REVISION: a commit you reviewed), else the commit already in
    the local cache, so only the very first download takes the current 'main'. A cached commit is
    loaded with local_files_only, which makes no request to Hugging Face at all.
    """
    revision = revision or cached_commit(repo_id)
    if revision and _COMMIT.fullmatch(revision):
        try:
            return loader(repo_id, revision=revision, local_files_only=True, **kwargs)
        except Exception:  # some files of that commit aren't downloaded yet: fetch them, same commit
            pass
    return loader(repo_id, revision=revision, **kwargs)


def make_embedder(provider: str, model: Optional[str] = None, dim: int = DEFAULT_EMBED_DIM,
                  revision: Optional[str] = None) -> Embedder:
    if provider == "voyage":
        return VoyageEmbedder(model or EMBED_DEFAULTS["voyage"], dim)
    if provider == "local":
        return LocalEmbedder(model or EMBED_DEFAULTS["local"], dim, revision=revision)
    raise ValueError(f"EMBEDDING_PROVIDER must be one of {tuple(EMBED_DEFAULTS)}, got {provider!r}")


def make_reranker(provider: str, model: Optional[str] = None, revision: Optional[str] = None) -> Reranker:
    if provider == "voyage":
        return VoyageReranker(model or RERANK_DEFAULTS["voyage"])
    if provider == "local":
        return CrossEncoderReranker(model or RERANK_DEFAULTS["local"], revision=revision)
    if provider == "none":
        return NoReranker()
    raise ValueError(f"RERANK_PROVIDER must be one of {tuple(RERANK_DEFAULTS)}, got {provider!r}")

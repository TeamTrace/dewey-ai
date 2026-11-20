"""
Tests for the local embedding and reranking models (app/providers/embeddings.py):
they load at a fixed Hugging Face revision, and EMBEDDING_PROVIDER=local never
calls the hosted Voyage API. sentence-transformers and torch are replaced by
fakes, so these run without requirements-local.txt and without network; the
Hugging Face cache is a temporary folder laid out the way huggingface_hub
writes it.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import os  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

import numpy as np  # noqa: E402

import providers  # noqa: E402
from providers import embeddings  # noqa: E402

CACHED = "a" * 40      # the commit already downloaded
REVIEWED = "b" * 40    # a commit set in EMBED_MODEL_REVISION


class FakeModel:
    calls = []          # (class name, repo, kwargs) of every load
    fail_offline = False

    def __init__(self, repo, **kwargs):
        type(self).calls.append((type(self).__name__, repo, kwargs))
        if kwargs.get("local_files_only") and type(self).fail_offline:
            raise OSError("not in the cache")
        self.encoded = []

    def encode_query(self, texts, **kwargs):
        self.encoded.append(("query", texts, kwargs))
        return np.ones((len(texts), kwargs.get("truncate_dim") or 2048), dtype=np.float32)

    def encode_document(self, texts, **kwargs):
        self.encoded.append(("document", texts, kwargs))
        return np.zeros((len(texts), kwargs.get("truncate_dim") or 2048), dtype=np.float32)

    def rank(self, query, documents, top_k):
        return [{"corpus_id": i} for i in reversed(range(len(documents)))][:top_k]


class SentenceTransformer(FakeModel):
    pass


class CrossEncoder(FakeModel):
    pass


class LocalModelTests(unittest.TestCase):
    def setUp(self):
        FakeModel.calls = []
        FakeModel.fail_offline = False
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        fake_st = types.ModuleType("sentence_transformers")
        fake_st.SentenceTransformer, fake_st.CrossEncoder = SentenceTransformer, CrossEncoder
        fake_torch = types.ModuleType("torch")
        fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
        patches = [mock.patch.dict(sys.modules, {"sentence_transformers": fake_st, "torch": fake_torch}),
                   mock.patch.dict(os.environ, {"SENTENCE_TRANSFORMERS_HOME": self.cache.name})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def download(self, repo, commit):
        """Lay out <cache>/models--org--name/{refs/main, snapshots/<commit>/config.json} like huggingface_hub."""
        folder = pathlib.Path(self.cache.name, "models--" + repo.replace("/", "--"))
        (folder / "refs").mkdir(parents=True, exist_ok=True)
        (folder / "refs" / "main").write_text(commit)
        (folder / "snapshots" / commit).mkdir(parents=True, exist_ok=True)
        (folder / "snapshots" / commit / "config.json").write_text("{}")

    def test_first_download_then_restarts_stay_on_that_commit_offline(self):
        embedder = embeddings.make_embedder("local")
        vectors = embedder.embed(["Which ATF informant talked to our reporter?"], "query")
        self.assertEqual(np.array(vectors).shape, (1, 1024))
        _, repo, kwargs = FakeModel.calls[0]
        self.assertEqual(repo, "MongoDB/voyage-4-nano")
        self.assertEqual(kwargs, {"revision": None, "trust_remote_code": True, "device": "cpu"})
        self.assertEqual(embedder._encoder.max_seq_length, 32768)
        self.assertEqual(embedder._encoder.encoded[0][2], {"normalize_embeddings": True, "truncate_dim": 1024})

        # Next process start: 'main' on the Hub may have moved, but the commit downloaded first is loaded,
        # from the cache only, so no new code is fetched and Hugging Face isn't contacted.
        self.download("MongoDB/voyage-4-nano", CACHED)
        FakeModel.calls = []
        embeddings.make_embedder("local").embed(["a story"], "document")
        self.assertEqual(FakeModel.calls, [("SentenceTransformer", "MongoDB/voyage-4-nano",
                                            {"revision": CACHED, "local_files_only": True,
                                             "trust_remote_code": True, "device": "cpu"})])

    def test_configured_revision_wins_and_missing_files_are_fetched_at_that_commit(self):
        self.download("MongoDB/voyage-4-nano", CACHED)
        FakeModel.fail_offline = True
        embeddings.make_embedder("local", revision=REVIEWED, dim=2048).embed(["x"], "document")
        self.assertEqual([(c[2]["revision"], c[2].get("local_files_only")) for c in FakeModel.calls],
                         [(REVIEWED, True), (REVIEWED, None)])

    def test_reranker_is_pinned_the_same_way(self):
        self.download("BAAI/bge-reranker-v2-m3", CACHED)
        reranker = embeddings.make_reranker("local")
        self.assertEqual(reranker.rank("q", ["a", "b", "c"], top_k=2), [2, 1])
        self.assertEqual(FakeModel.calls, [("CrossEncoder", "BAAI/bge-reranker-v2-m3",
                                            {"revision": CACHED, "local_files_only": True})])
        FakeModel.calls = []
        embeddings.make_reranker("local", model=self.cache.name).encoder  # a folder on disk: nothing to pin
        self.assertEqual(FakeModel.calls, [("CrossEncoder", self.cache.name, {"revision": None})])

    def test_local_never_sends_text_to_the_hosted_api(self):
        # A newsroom moving from the anthropic stack keeps EMBED_MODEL="voyage-4" and a VOYAGE_API_KEY:
        # the voyageai SDK would have sent every question to api.voyageai.com.
        env = {"DEWEY_STACK": "open", "EMBEDDING_PROVIDER": "local", "RERANK_PROVIDER": "none",
               "EMBED_MODEL": "voyage-4", "VOYAGE_API_KEY": "pa-leftover", "DATABASE_URL": "postgresql://x@y/z"}
        import voyageai

        with mock.patch.dict(os.environ, env), \
                mock.patch.object(voyageai, "Client", side_effect=AssertionError("hosted client created")):
            with self.assertRaisesRegex(providers.StackConfigError, "EMBEDDING_PROVIDER=voyage"):
                providers.build_pgvector_store()
            with self.assertRaises(ValueError):
                embeddings.make_embedder("local", model="voyage-4-large")
            os.environ["EMBED_MODEL"] = ""
            store = providers.build_pgvector_store()
        self.assertIsInstance(store.embedder, embeddings.LocalEmbedder)
        self.assertEqual(store.embedder.model, "voyage-4-nano")  # what index_info records, as before

    def test_unsupported_settings_are_refused_up_front(self):
        with self.assertRaisesRegex(ValueError, "EMBEDDING_PROVIDER=local"):
            embeddings.make_embedder("voyage", model="voyage-4-nano")  # would run unpinned via the SDK
        with self.assertRaisesRegex(ValueError, "EMBED_DIM"):
            embeddings.make_embedder("local", dim=768)
        with self.assertRaises(ValueError):
            embeddings.make_embedder("local").embed(["x"], "classification")


if __name__ == "__main__":
    unittest.main()

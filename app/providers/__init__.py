"""
Stack selection. `DEWEY_STACK` picks which vendors Dewey runs on:

    azure      Azure OpenAI (chat + embeddings) + Azure AI Search   [default]
    anthropic  Claude (chat) + Postgres/pgvector; embeddings and reranking
               default to hosted Voyage AI
    open       open-weight chat model on an OpenAI-compatible local server
               (e.g. Ollama) + Postgres/pgvector; embeddings and reranking
               default to open-weight models run locally

For the two Postgres stacks, EMBEDDING_PROVIDER (voyage | local) and
RERANK_PROVIDER (voyage | local | none) override those defaults; see
embeddings.py.

Vendor SDKs are imported lazily so a deployment only needs the packages for
the stack it uses.
"""
import os
from typing import Tuple

from models import NewsroomConfig

from .base import LLMProvider, Retriever

STACKS = ("azure", "anthropic", "open")

# Default (embedding, rerank) providers per Postgres stack.
RETRIEVAL_DEFAULTS = {"anthropic": ("voyage", "voyage"), "open": ("local", "local")}


class StackConfigError(Exception):
    pass


def _require(*names: str) -> None:
    missing = [n for n in names if not os.getenv(n, "").strip()]
    if missing:
        raise StackConfigError(
            f"This configuration (DEWEY_STACK={os.getenv('DEWEY_STACK', 'azure')}) needs these environment variables: "
            + ", ".join(missing)
        )


def _profile_config_path() -> str:
    """Path to the config file for the profile the SDK would load, or "" if there is none.

    Mirrors the SDK's lookup: $ANTHROPIC_CONFIG_DIR, else ~/.config/anthropic (or
    %APPDATA%\\Anthropic on Windows); then $ANTHROPIC_PROFILE, else the name in
    active_config, else "default".
    """
    config_dir = os.getenv("ANTHROPIC_CONFIG_DIR", "").strip()
    if not config_dir:
        if os.name == "nt":
            config_dir = os.path.join(os.getenv("APPDATA", ""), "Anthropic")
        else:
            config_dir = os.path.expanduser("~/.config/anthropic")

    def config_for(name: str) -> str:
        path = os.path.join(config_dir, "configs", f"{name}.json")
        return path if name and os.path.isfile(path) else ""

    named = os.getenv("ANTHROPIC_PROFILE", "").strip()
    if named:
        # A named profile that doesn't exist is an error for the SDK, not a fall-through.
        return config_for(named)
    try:
        with open(os.path.join(config_dir, "active_config"), encoding="utf-8") as fh:
            active = fh.read().strip()
    except OSError:
        active = ""
    return config_for(active) or config_for("default")


def anthropic_credential_source() -> str:
    """Name of the Claude credential the SDK will use, or "" when it would find none.

    anthropic_llm.py constructs Anthropic() with no arguments, so the SDK resolves the
    credential itself. Dewey only checks that one exists, in the SDK's own precedence
    order, so that a profile from `ant auth login` or a Workload Identity Federation
    setup isn't rejected as a missing API key.
    """
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.getenv(var, "").strip():
            return var
    if (os.getenv("ANTHROPIC_FEDERATION_RULE_ID", "").strip()
            and os.getenv("ANTHROPIC_ORGANIZATION_ID", "").strip()
            and (os.getenv("ANTHROPIC_IDENTITY_TOKEN", "").strip()
                 or os.getenv("ANTHROPIC_IDENTITY_TOKEN_FILE", "").strip())):
        return "workload identity federation"
    if _profile_config_path():
        return "profile"
    return ""


def current_stack() -> str:
    stack = os.getenv("DEWEY_STACK", "azure").strip().lower()
    if stack not in STACKS:
        raise StackConfigError(f"DEWEY_STACK must be one of {STACKS}, got {stack!r}")
    return stack


def build_stack(newsroom: NewsroomConfig) -> Tuple[LLMProvider, Retriever]:
    stack = current_stack()

    if stack == "azure":
        from models import AzureOpenAIConfig, AzureSearchConfig
        from .azure_openai import AzureOpenAILLM, make_client
        from .azure_search import AzureSearchRetriever

        _require(
            "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "EMBEDDING_DEPLOYMENT_NAME",
            "EMBEDDING_MODEL_NAME", "CHATGPT_DEPLOYMENT_NAME", "CHATGPT_MODEL_NAME",
            "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_INDEX_NAME", "AZURE_SEARCH_API_KEY",
        )
        oai_config = AzureOpenAIConfig(
            endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            embedding_deployment=os.environ["EMBEDDING_DEPLOYMENT_NAME"],
            embedding_model=os.environ["EMBEDDING_MODEL_NAME"],
            chat_deployment=os.environ["CHATGPT_DEPLOYMENT_NAME"],
            chat_model=os.environ["CHATGPT_MODEL_NAME"],
        )
        search_config = AzureSearchConfig(
            service_endpoint=os.environ["AZURE_SEARCH_ENDPOINT"],
            index_name=os.environ["AZURE_SEARCH_INDEX_NAME"],
            key=os.environ["AZURE_SEARCH_API_KEY"],
        )
        client = make_client(oai_config)
        return (
            AzureOpenAILLM(client, oai_config, newsroom),
            AzureSearchRetriever(client, oai_config, search_config),
        )

    if stack == "anthropic":
        from .anthropic_llm import DEFAULT_MODEL, AnthropicLLM

        blanked = [v for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
                   if v in os.environ and not os.environ[v].strip()]
        if blanked:
            raise StackConfigError(
                f"{', '.join(blanked)} is set but empty. An empty value still takes precedence over "
                "a profile and over Workload Identity Federation, so the request would be sent with "
                "no credential. Remove the line (don't leave it blank) or give it a real value."
            )
        if not anthropic_credential_source():
            named = os.getenv("ANTHROPIC_PROFILE", "").strip()
            raise StackConfigError(
                "DEWEY_STACK=anthropic needs a Claude credential. Set ANTHROPIC_API_KEY, or run "
                "`ant auth login` to store a profile, or set ANTHROPIC_FEDERATION_RULE_ID, "
                "ANTHROPIC_ORGANIZATION_ID and ANTHROPIC_IDENTITY_TOKEN (or "
                "ANTHROPIC_IDENTITY_TOKEN_FILE) for Workload Identity Federation. Leave "
                "ANTHROPIC_API_KEY unset rather than empty: an empty value still takes "
                "precedence over a profile and over federation."
                + (f" ANTHROPIC_PROFILE={named!r} is set, but that profile has no config file; "
                   "the SDK does not try another source when it is set." if named else "")
            )
        _require("DATABASE_URL")
        retriever = build_pgvector_store()
        llm = AnthropicLLM(
            newsroom,
            model=os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL),
            metadata_effort=os.getenv("ANTHROPIC_METADATA_EFFORT", "low"),
            answer_effort=os.getenv("ANTHROPIC_ANSWER_EFFORT", "medium"),
        )
        return llm, retriever

    # stack == "open"
    from .open_llm import DEFAULT_BASE_URL, OpenLLM

    _require("OPEN_LLM_MODEL", "DATABASE_URL")
    retriever = build_pgvector_store()
    llm = OpenLLM(
        newsroom,
        model=os.environ["OPEN_LLM_MODEL"],
        base_url=os.getenv("OPEN_LLM_BASE_URL", DEFAULT_BASE_URL),
        api_key=os.getenv("OPEN_LLM_API_KEY", ""),
    )
    return llm, retriever


def retrieval_providers() -> Tuple[str, str]:
    """(embedding provider, rerank provider) after applying env overrides."""
    stack = current_stack()
    default_embed, default_rerank = RETRIEVAL_DEFAULTS.get(stack, ("voyage", "voyage"))
    embed = os.getenv("EMBEDDING_PROVIDER", "").strip().lower() or default_embed
    rerank = os.getenv("RERANK_PROVIDER", "").strip().lower() or default_rerank
    return embed, rerank


def build_pgvector_store():
    from .embeddings import DEFAULT_EMBED_DIM, make_embedder, make_reranker
    from .pgvector_store import PgVectorConfig, PgVectorStore

    embed_provider, rerank_provider = retrieval_providers()
    if "voyage" in (embed_provider, rerank_provider):
        _require("VOYAGE_API_KEY")
    _require("DATABASE_URL")

    try:
        embedder = make_embedder(
            embed_provider,
            model=os.getenv("EMBED_MODEL") or None,
            dim=int(os.getenv("EMBED_DIM", DEFAULT_EMBED_DIM)),
            revision=os.getenv("EMBED_MODEL_REVISION", "").strip() or None,
        )
        reranker = make_reranker(rerank_provider, model=os.getenv("RERANK_MODEL") or None,
                                 revision=os.getenv("RERANK_MODEL_REVISION", "").strip() or None)
    except ValueError as e:
        raise StackConfigError(str(e)) from None

    return PgVectorStore(PgVectorConfig(database_url=os.environ["DATABASE_URL"]), embedder, reranker)


__all__ = ["build_stack", "build_pgvector_store", "current_stack", "StackConfigError", "LLMProvider", "Retriever"]

"""
Stack selection. `DEWEY_STACK` picks which vendors Dewey runs on:

    azure      Azure OpenAI (chat + embeddings) + Azure AI Search   [default]
    anthropic  Claude (chat) + Postgres/pgvector; embeddings and reranking
               default to hosted Voyage AI
    open       open-weight chat model on an OpenAI-compatible server (your
               own, e.g. Ollama, or hosted on OpenRouter) + Postgres/pgvector;
               embeddings and reranking default to open-weight models run
               locally. On OpenRouter, OPENROUTER_PRIVACY (strict | standard |
               off, default strict) sets the privacy routing; see open_llm.py.

For the two Postgres stacks, EMBEDDING_PROVIDER (voyage | local) and
RERANK_PROVIDER (voyage | local | none) override those defaults; see
embeddings.py.

Vendor SDKs are imported lazily so a deployment only needs the packages for
the stack it uses.
"""
import os
import sys
from typing import Tuple
from urllib.parse import urlsplit

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

        _require("ANTHROPIC_API_KEY", "DATABASE_URL")
        retriever = build_pgvector_store()
        llm = AnthropicLLM(
            newsroom,
            model=os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL),
            metadata_effort=os.getenv("ANTHROPIC_METADATA_EFFORT", "low"),
            answer_effort=os.getenv("ANTHROPIC_ANSWER_EFFORT", "medium"),
        )
        return llm, retriever

    # stack == "open"
    from .open_llm import DEFAULT_BASE_URL, OpenLLM, is_openrouter_url

    _require("OPEN_LLM_MODEL", "DATABASE_URL")
    # Blank means the default: install.py writes OPEN_LLM_BASE_URL='' when the user keeps local Ollama.
    base_url = os.getenv("OPEN_LLM_BASE_URL", "").strip() or DEFAULT_BASE_URL
    privacy_setting = os.getenv("OPENROUTER_PRIVACY", "").strip()
    if is_openrouter_url(base_url):
        if urlsplit(base_url).scheme != "https":
            raise StackConfigError("OPEN_LLM_BASE_URL points at OpenRouter but doesn't use https://, so the API key "
                                   "and every question would be sent unencrypted. Use https://openrouter.ai/api/v1.")
        if not os.getenv("OPEN_LLM_API_KEY", "").strip():
            raise StackConfigError("OPEN_LLM_BASE_URL points at OpenRouter, so OPEN_LLM_API_KEY must be set "
                                   "to an OpenRouter API key.")
    elif privacy_setting:
        # stderr, not stdout: stdout is the MCP server's JSON-RPC channel.
        print(f"Dewey: warning: OPENROUTER_PRIVACY is set but ignored: OPEN_LLM_BASE_URL ({base_url}) is not on "
              "openrouter.ai, so no OpenRouter privacy preferences are sent. If this server forwards to OpenRouter, "
              "set privacy in your OpenRouter account settings instead.", file=sys.stderr)
    try:
        llm = OpenLLM(
            newsroom,
            model=os.environ["OPEN_LLM_MODEL"],
            base_url=base_url,
            api_key=os.getenv("OPEN_LLM_API_KEY", "").strip(),
            privacy=privacy_setting,
        )
    except ValueError as e:
        raise StackConfigError(str(e)) from None
    retriever = build_pgvector_store()
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
    database_url = os.environ["DATABASE_URL"]
    check_database_url(database_url)

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

    return PgVectorStore(PgVectorConfig(database_url=database_url), embedder, reranker)


def check_database_url(database_url: str) -> None:
    """Parse DATABASE_URL the way psycopg.connect() will, at startup. psycopg's own message
    quotes the part it couldn't parse, which is often the password, so it is not repeated."""
    from psycopg import Error
    from psycopg.conninfo import conninfo_to_dict

    try:
        conninfo_to_dict(database_url)
    except Error:
        raise StackConfigError(
            "DATABASE_URL is not a valid Postgres connection URL (its value is not shown here, because it "
            "contains the password). It should look like postgresql://USER:PASSWORD@HOST:5432/DATABASE, "
            "with no quotes or spaces around it. If the password contains characters such as % @ : / # ? "
            "or spaces, write them percent-encoded (for example %25 for %, %40 for @, %20 for a space)."
        ) from None


__all__ = ["build_stack", "build_pgvector_store", "current_stack", "StackConfigError", "LLMProvider", "Retriever"]

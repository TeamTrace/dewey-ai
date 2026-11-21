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
from .claude_credentials import (
    MISSING, UNCONFIRMED, USABLE, anthropic_credential_status, blank_credential_message,
    blank_credential_variables, no_credential_in_client_message, sdk_refusal_message, startup_error,
    startup_warning, unreadable_address_message,
)

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


def anthropic_credential_source() -> str:
    """Name of the Claude credential Dewey's client will use, or "" when Dewey can't count on one.

    anthropic_llm.py constructs Anthropic() with no arguments, so the SDK resolves the
    credential itself. claude_credentials.py follows the SDK's own order, so that a sign-in
    from `ant auth login` or a Workload Identity Federation setup isn't rejected as a missing
    API key, and one the SDK would refuse isn't counted. "" covers both states that aren't
    `usable`: a missing credential and a sign-in that looks signed out. install.py loads the
    same file, so its anthropic_credential_present() always agrees with this.
    """
    credential = anthropic_credential_status(os.environ)
    return credential.source if credential.state == USABLE else ""


def _unreadable_address(address: str) -> bool:
    """True when the HTTP library under the Claude SDK can't read this web address. The SDK hands
    ANTHROPIC_BASE_URL to the same parser, so this says whether that variable is what stopped it."""
    import httpx2

    try:
        httpx2.URL(address)
    except httpx2.InvalidURL:
        return True
    return False


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
        import anthropic
        import httpx2  # the HTTP library the pinned SDK is built on: its InvalidURL is caught below

        from .anthropic_llm import DEFAULT_MODEL, AnthropicLLM

        # The Claude client is built with no arguments, so the SDK picks the credential. Each way
        # the credential or the client's web address can stop that ends here as one plain message,
        # not as the SDK's traceback. (A proxy or certificate variable the HTTP library refuses for
        # another reason, such as an unknown proxy scheme, still ends as that library's own error.)
        blanked = blank_credential_variables(os.environ)
        if blanked:
            raise StackConfigError(blank_credential_message(blanked))
        credential = anthropic_credential_status(os.environ)
        if credential.state == MISSING:
            # The SDK would stop in its constructor, or build a client with no credential.
            raise StackConfigError(startup_error(credential))
        _require("DATABASE_URL")
        retriever = build_pgvector_store()
        try:
            llm = AnthropicLLM(
                newsroom,
                model=os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL),
                metadata_effort=os.getenv("ANTHROPIC_METADATA_EFFORT", "low"),
                answer_effort=os.getenv("ANTHROPIC_ANSWER_EFFORT", "medium"),
            )
        except (anthropic.AnthropicError, UnicodeDecodeError) as e:
            # Backstop: the check above reads files and doesn't see everything the SDK refuses
            # (file permissions, symlinks, a base URL that isn't https). UnicodeDecodeError is what
            # the SDK lets out for an active_config file that isn't text.
            raise StackConfigError(sdk_refusal_message(e, os.environ, credential)) from None
        except httpx2.InvalidURL:
            # A web address the HTTP library can't read: ANTHROPIC_BASE_URL, "base_url" in the
            # sign-in's settings file, or a proxy variable. Its message quotes the part it stopped
            # at, which can be part of a password, so it is not repeated (as for DATABASE_URL).
            raise StackConfigError(unreadable_address_message(
                os.environ, credential, _unreadable_address(os.environ.get("ANTHROPIC_BASE_URL", "")))) from None
        if all(getattr(llm.client, held, None) is None for held in ("api_key", "auth_token", "credentials")):
            # In the default location the SDK skips a sign-in it can't load and says nothing; the
            # first question would then fail with "Could not resolve authentication method".
            llm.client.close()
            raise StackConfigError(no_credential_in_client_message())
        if credential.state == UNCONFIRMED:
            # A signed-out sign-in: the client exists but its first request will fail. Warn and
            # start, since searches don't call Claude. stderr, not stdout: stdout is the MCP
            # server's JSON-RPC channel.
            print(startup_warning(credential), file=sys.stderr)
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

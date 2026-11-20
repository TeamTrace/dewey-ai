"""
Stack selection. `DEWEY_STACK` picks which vendors Dewey runs on:

    azure      Azure OpenAI (chat + embeddings) + Azure AI Search   [default]
    anthropic  Claude (chat) + Voyage AI (embeddings, rerank) + Postgres/pgvector

Vendor SDKs are imported lazily so a deployment only needs the packages for
the stack it uses.
"""
import os
from typing import Tuple

from models import NewsroomConfig

from .base import LLMProvider, Retriever

STACKS = ("azure", "anthropic")


class StackConfigError(Exception):
    pass


def _require(*names: str) -> None:
    missing = [n for n in names if not os.getenv(n, "").strip()]
    if missing:
        raise StackConfigError(
            f"DEWEY_STACK={os.getenv('DEWEY_STACK', 'azure')} needs these environment variables: "
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

    # stack == "anthropic"
    from .anthropic_llm import DEFAULT_MODEL, AnthropicLLM

    _require("ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "DATABASE_URL")
    llm = AnthropicLLM(
        newsroom,
        model=os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL),
        metadata_effort=os.getenv("ANTHROPIC_METADATA_EFFORT", "low"),
        answer_effort=os.getenv("ANTHROPIC_ANSWER_EFFORT", "medium"),
    )
    return llm, build_pgvector_store()


def build_pgvector_store():
    from .pgvector_store import (
        DEFAULT_EMBED_DIM, DEFAULT_EMBED_MODEL, DEFAULT_RERANK_MODEL,
        PgVectorConfig, PgVectorStore,
    )

    return PgVectorStore(PgVectorConfig(
        database_url=os.environ["DATABASE_URL"],
        embed_model=os.getenv("VOYAGE_EMBED_MODEL", DEFAULT_EMBED_MODEL),
        rerank_model=os.getenv("VOYAGE_RERANK_MODEL", DEFAULT_RERANK_MODEL),
        embed_dim=int(os.getenv("VOYAGE_EMBED_DIM", DEFAULT_EMBED_DIM)),
    ))


__all__ = ["build_stack", "build_pgvector_store", "current_stack", "StackConfigError", "LLMProvider", "Retriever"]

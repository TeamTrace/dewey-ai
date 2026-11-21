"""Azure AI Search retriever (original Dewey behavior, moved out of dewey.py)."""
import json
import sys
from datetime import date
from typing import Any, Dict, List, Optional

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import QueryType, VectorizedQuery, VectorQuery
from dateutil.parser import parse
from openai import AzureOpenAI

from models import AzureOpenAIConfig, AzureSearchConfig

from .base import author_names


def _odata_str(value: str) -> str:
    """Quote a string literal for an OData filter (single quotes are doubled)."""
    return "'" + value.replace("'", "''") + "'"


def _iso_date(value: Any) -> Optional[str]:
    """Return value if it is a YYYY-MM-DD date, else None (model output is not trusted)."""
    try:
        return date.fromisoformat(value).isoformat() if value else None
    except (TypeError, ValueError):
        print(f"Ignoring unparseable date: {value!r}", file=sys.stderr)
        return None


def build_filter(metadata: Dict[str, Any]) -> Optional[str]:
    filters = []

    # Date range filter
    start_date = _iso_date(metadata["date_range"]["start_date"])
    end_date = _iso_date(metadata["date_range"]["end_date"])
    if start_date:
        filters.append(f"publish_date ge {start_date}T00:00:00Z")

    if end_date:
        filters.append(f"publish_date le {end_date}T23:59:59Z")

    # Authors filter. author_names() (base.py) refuses malformed entries (MetadataError) and
    # skips blank names, the same as the Postgres store; names are matched exactly as given.
    # The clause is only added when a name is left: `authors/any(a: )` is an invalid filter.
    author_filters = [f"a eq {_odata_str(name)}" for name in author_names(metadata)]
    if author_filters:
        filters.append(f"authors/any(a: {' or '.join(author_filters)})")

    return " and ".join(filters) if filters else None


class AzureSearchRetriever:
    def __init__(self, oai_client: AzureOpenAI, oai_config: AzureOpenAIConfig, search_config: AzureSearchConfig):
        self.oai_client = oai_client
        self.oai_config = oai_config
        self.search_client = SearchClient(
            search_config.service_endpoint,
            search_config.index_name,
            AzureKeyCredential(search_config.key),
        )

    def retrieve(self, metadata: Dict[str, Any]) -> List[str]:
        # First, so a malformed author entry (MetadataError) stops the search before any Azure call.
        filter = build_filter(metadata)
        print(filter, file=sys.stderr)

        embedding = self.oai_client.embeddings.create(
            model=self.oai_config.embedding_deployment,
            input=metadata["question"],
        )
        vectors: List[VectorQuery] = [
            VectorizedQuery(vector=embedding.data[0].embedding, k_nearest_neighbors=50, fields="content_vector")
        ]

        results = self.search_client.search(
            search_text=metadata["question"],
            filter=filter,
            top=10,
            vector_queries=vectors,
            query_type=QueryType.SEMANTIC,
            semantic_configuration_name="default",
            semantic_query=metadata["question"],
            select=["url", "headline", "publish_date", "content", "authors"],
        )

        sources = []
        for page in results:
            sources.append(json.dumps({
                "url": page["url"],
                "publish_date": f"{parse(page['publish_date']).date().isoformat()}",
                "authors": page["authors"],
                "headline": page["headline"],
                "content": page["content"].replace("\n", " ").replace("\r", " "),
            }))
        return sources

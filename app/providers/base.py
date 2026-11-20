"""
Interfaces Dewey's pipeline depends on. Each stack (Azure, Anthropic) supplies
one implementation of each, so `Dewey` never imports a vendor SDK directly.

Metadata dict shape (shared by every LLM provider and retriever):
    {"question": str,
     "date_range": {"start_date": str | None, "end_date": str | None},
     "authors": [{"name": str}, ...]}

Sources are JSON strings with keys: url, publish_date, authors, headline, content.
"""
from typing import Any, Dict, Iterator, List, Protocol


class LLMProvider(Protocol):
    def generate_metadata(self, messages: List[Dict[str, Any]], current_date: str) -> Dict[str, Any]:
        """Turn the conversation into search metadata (see module docstring)."""
        ...

    def stream_answer(self, messages: List[Dict[str, Any]], current_date: str) -> Iterator[str]:
        """Yield text deltas of the cited answer."""
        ...


class Retriever(Protocol):
    def retrieve(self, metadata: Dict[str, Any]) -> List[str]:
        """Return up to N sources as JSON strings (see module docstring)."""
        ...

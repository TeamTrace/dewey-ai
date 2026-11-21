"""
Interfaces Dewey's pipeline depends on. Each stack (Azure, Anthropic) supplies
one implementation of each, so `Dewey` never imports a vendor SDK directly.

Metadata dict shape (shared by every LLM provider and retriever):
    {"question": str,
     "date_range": {"start_date": str | None, "end_date": str | None},
     "authors": [{"name": str}, ...]}

Author entries. Every retriever reads "authors" through author_names() below, so
every stack uses, skips and refuses the same entries:
  - Used: a dict with non-blank text under "name", such as {"name": "Jane Doe"}.
    Other keys in the dict are ignored.
  - Skipped: a dict whose "name" is blank (None, empty or only whitespace). With
    no name left, or with "authors" missing, None or empty, there is no author
    filter.
  - Refused with MetadataError, before the search or its embedding call:
    everything else. That is a plain string such as "Jane Doe" (tips.py and the
    search-planning schemas assume dicts), any other entry that isn't a dict, a
    dict with no "name", a name that isn't text, and an "authors" value that
    isn't a list or tuple. One such entry refuses the whole list. So an entry
    that names someone is never left out silently, which would turn a byline
    search into a search of every writer's stories.
Bylines are plain strings elsewhere, and none of these is this dict: the MCP
tool search_archive takes them and wraps each one as {"name": ...}
(mcp_server.build_metadata), article files list them under "authors", and so do
the sources a retriever returns.

Sources are JSON strings with keys: url, publish_date, authors, headline, content.
"""
from typing import Any, Dict, Iterator, List, Protocol


class UserFacingError(Exception):
    """A provider error whose message says what to fix and holds no secrets (no API keys,
    passwords, connection strings or upstream response bodies with them). The MCP server
    passes these messages on to its clients; it replaces every other exception's message
    with a generic one, because exception text from database drivers and SDKs can quote
    DATABASE_URL, password included."""


class MetadataError(ValueError):
    """The metadata dict given to a retriever doesn't have the shape described at the top of
    this module. Raised before the search or its embedding call. The message gives an entry's
    position and kind, never its value, which can be text a model wrote.

    Deliberately not a UserFacingError. Through the MCP server only ask_archive can reach
    this, when the search plan from Dewey's own model is malformed, and a client can't fix
    that: it gets the server's generic message with a log reference, and whoever runs Dewey
    finds this message in the log."""


_AUTHOR_ENTRY = '{"name": "Jane Doe"}'
_SEARCH_NOT_RUN = ("Dewey did not run the search, because without the author filter the results "
                   "could include stories by any writer.")


def _kind(value: Any) -> str:
    """How an error message describes a value: by its type, never by its content."""
    if value is None:
        return "None"
    if isinstance(value, str):
        return "a plain string"
    return f"of type {type(value).__name__}"


def author_names(metadata: Dict[str, Any]) -> List[str]:
    """The names an author filter must match: the non-blank "name" of each entry of
    metadata["authors"], in order and exactly as given (each retriever compares them in its
    own way). Every retriever calls this instead of reading "authors" itself, so all stacks
    use, skip and refuse the same entries; the rules are in the module docstring. Raises
    MetadataError for an entry it can't use, so that entry is never silently left out."""
    authors = metadata.get("authors")
    if authors is None:
        return []
    if not isinstance(authors, (list, tuple)):
        raise MetadataError(
            f'"authors" in the search metadata must be a list of entries that look like {_AUTHOR_ENTRY}, '
            f"but it is {_kind(authors)}. {_SEARCH_NOT_RUN}"
        )
    names = []
    for position, entry in enumerate(authors, 1):
        where = f"Author entry {position} of {len(authors)} in the search metadata"
        if not isinstance(entry, dict):
            raise MetadataError(
                f"{where} is {_kind(entry)}, but each entry must look like {_AUTHOR_ENTRY}. {_SEARCH_NOT_RUN}"
            )
        if "name" not in entry:
            raise MetadataError(
                f'{where} has no "name". Each entry must look like {_AUTHOR_ENTRY}. {_SEARCH_NOT_RUN}'
            )
        name = entry["name"]
        if name is None:
            continue  # no name given: it names nobody, like a blank string
        if not isinstance(name, str):
            raise MetadataError(
                f'{where} has a "name" that is {_kind(name)}, not text. Each entry must look like '
                f"{_AUTHOR_ENTRY}. {_SEARCH_NOT_RUN}"
            )
        if name.strip():  # str.strip() also strips no-break and other Unicode spaces
            names.append(name)
    return names


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

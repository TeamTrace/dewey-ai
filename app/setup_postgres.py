"""
Index the JSON articles in data/ into Postgres + pgvector
(DEWEY_STACK=anthropic or DEWEY_STACK=open).

    python app/setup_postgres.py

Uses the same EMBEDDING_PROVIDER / EMBED_MODEL / EMBED_DIM settings as the
app, so run it with the .env you will run Dewey with. Requires DATABASE_URL,
plus VOYAGE_API_KEY when embeddings come from hosted Voyage AI. Safe to
re-run: articles are upserted by id (or by a hash of their URL when no id is
given) and their chunks are replaced.

This is the counterpart of app/setup.py, which builds the Azure AI Search
index, skillset and indexer. Here, chunking and embedding run locally in this
script instead of inside an Azure indexer.
"""
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlsplit

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).parent.parent
DATA_FOLDER = REPO_ROOT / "data"
REQUIRED_FIELDS = ["headline", "content", "url", "authors", "publish_date"]


def own_publisher() -> str:
    """
    The newsroom whose archive this is, for documents that name no publisher.
    Raises NewsroomConfigError when NEWSROOM_NAME is unset or blank: there is no default name.
    """
    from models import NewsroomConfig
    return NewsroomConfig.from_env().publisher


class ConflictingIdsError(Exception):
    """Two input documents with the same id but on different sites: indexing would silently keep only one."""


def _site(url: str) -> str:
    try:
        host = (urlsplit(str(url).strip()).hostname or "").lower()
    except ValueError:
        return str(url)
    return host[4:] if host.startswith("www.") else host


def load_documents(data_folder: Path = DATA_FOLDER) -> List[Dict[str, Any]]:
    """
    Same validation rules as app/setup.py: skip documents missing a required field.

    Raises ConflictingIdsError when two documents share an id (the key articles are
    upserted by) but their URLs are on different sites. Ids come from the source
    (a feed's guid, a CMS post id), so another source, such as a partner's or a
    compromised feed, can reuse them; the document indexed last would silently
    replace the other one.

    Raises NewsroomConfigError when a document names no publisher and NEWSROOM_NAME
    isn't set: it would otherwise be labelled with a name nobody chose.
    """
    from providers.pgvector_store import article_id

    documents = []
    seen: Dict[str, Tuple[str, str]] = {}  # id -> (file, url) of its first document
    conflicts = []
    own = None  # looked up only when a document needs it
    json_files = sorted(data_folder.glob("*.json"))
    if not json_files:
        print(f"📁 No JSON documents found. Place them in: {data_folder}")
        return documents

    for path in json_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"❌ Error loading {path.name}: {e}")
            continue
        file_documents = data if isinstance(data, list) else [data]
        valid = 0
        for i, doc in enumerate(file_documents):
            # `authors` may be an empty list (no byline in the source); it must still be a list.
            missing = [f for f in REQUIRED_FIELDS if f != "authors" and not doc.get(f)]
            if not isinstance(doc.get("authors"), list):
                missing.append("authors")
            if missing:
                print(f"⚠️  Skipping document {i} in {path.name}: missing required fields: {', '.join(missing)}")
                continue
            aid = article_id(doc)
            first = seen.setdefault(aid, (path.name, doc["url"]))
            if _site(first[1]) != _site(doc["url"]):
                conflicts.append(f"  id {aid}: {first[0]} ({first[1]}) and {path.name} ({doc['url']})")
            # A document that doesn't say where it came from is the newsroom's own.
            # Naming it here is what lets an answer distinguish your reporting from
            # material collected elsewhere.
            if not str(doc.get("publisher") or "").strip():
                own = own or own_publisher()
                doc["publisher"] = own
            documents.append(doc)
            valid += 1
        print(f"✅ Loaded {valid} valid document(s) from {path.name}")
    if conflicts:
        raise ConflictingIdsError(
            f"{len(conflicts)} article id(s) appear in more than one document, on different sites, so one "
            "would silently replace the other in the index:\n" + "\n".join(conflicts[:20])
            + ("\n  ..." if len(conflicts) > 20 else "")
            + "\nCheck which source is right, remove or re-import the other file, then run this again."
        )
    return documents


def main() -> None:
    load_dotenv(REPO_ROOT / ".env", interpolate=False)  # values are literal
    from models import NewsroomConfigError
    from providers import StackConfigError, build_pgvector_store, current_stack, retrieval_providers

    try:
        if current_stack() == "azure":
            print("❌ DEWEY_STACK is 'azure' (or unset). This script indexes into Postgres for "
                  "DEWEY_STACK=anthropic or open; for Azure, run app/setup.py.")
            sys.exit(1)
        store = build_pgvector_store()
    except StackConfigError as e:
        print(f"❌ {e}")
        sys.exit(1)

    embed_provider, rerank_provider = retrieval_providers()
    print(f"🔧 Embeddings: {embed_provider} ({store.embedder.model}, {store.embedder.dim} dims); "
          f"reranking: {rerank_provider}")
    print("🔧 Creating schema (extension, tables, indexes) if needed...")
    try:
        store.create_schema()
    except Exception as e:  # IndexMismatchError and connection errors alike
        print(f"❌ {e}")
        sys.exit(1)

    try:
        documents = load_documents(DATA_FOLDER)
    except ConflictingIdsError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except NewsroomConfigError as e:
        print(f"❌ Articles that name no publisher are labelled with your newsroom's name, and none is "
              f"set, so nothing was indexed. {e}")
        sys.exit(1)
    if not documents:
        return

    print(f"🔄 Chunking and embedding {len(documents)} document(s) with {store.embedder.model}...")
    chunks = store.index_documents(documents)
    print(f"🎉 Indexed {len(documents)} article(s) as {chunks} chunk(s).")


if __name__ == "__main__":
    main()

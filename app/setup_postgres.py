"""
Index the JSON articles in data/ into Postgres + pgvector (DEWEY_STACK=anthropic).

    python app/setup_postgres.py

Requires DATABASE_URL and VOYAGE_API_KEY. Safe to re-run: articles are
upserted by id (or by a hash of their URL when no id is given) and their
chunks are replaced.

This is the counterpart of app/setup.py, which builds the Azure AI Search
index, skillset and indexer. Here, chunking and embedding run locally in this
script instead of inside an Azure indexer.
"""
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).parent.parent
DATA_FOLDER = REPO_ROOT / "data"
REQUIRED_FIELDS = ["headline", "content", "url", "authors", "publish_date"]


def load_documents(data_folder: Path = DATA_FOLDER) -> List[Dict[str, Any]]:
    """Same validation rules as app/setup.py: skip documents missing a required field."""
    documents = []
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
            missing = [f for f in REQUIRED_FIELDS if not doc.get(f)]
            if missing:
                print(f"⚠️  Skipping document {i} in {path.name}: missing required fields: {', '.join(missing)}")
                continue
            documents.append(doc)
            valid += 1
        print(f"✅ Loaded {valid} valid document(s) from {path.name}")
    return documents


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    missing = [v for v in ("DATABASE_URL", "VOYAGE_API_KEY") if not os.getenv(v, "").strip()]
    if missing:
        print(f"❌ Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    from providers import build_pgvector_store

    store = build_pgvector_store()
    print("🔧 Creating schema (extension, tables, indexes) if needed...")
    store.create_schema()

    documents = load_documents()
    if not documents:
        return

    print(f"🔄 Chunking and embedding {len(documents)} document(s) with {store.config.embed_model}...")
    chunks = store.index_documents(documents)
    print(f"🎉 Indexed {len(documents)} article(s) as {chunks} chunk(s).")


if __name__ == "__main__":
    main()

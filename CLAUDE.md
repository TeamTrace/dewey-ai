# Dewey

Gradio chat app that answers journalists' questions from a newsroom article archive, with
`[SRCn]` citations linked to article URLs. Forked from `phillymedia/dewey-ai` (The
Philadelphia Inquirer); this fork adds a non-Azure stack.

## Layout

- `app/main.py`: Gradio UI. Builds the stack with `providers.build_stack()` and runs `Dewey`.
- `app/dewey.py`: vendor-neutral pipeline. `process()` runs three steps:
  1. `llm.generate_metadata()` → `{"question", "date_range": {"start_date", "end_date"}, "authors": [{"name"}]}`
  2. `retriever.retrieve(metadata)` → list of JSON strings (`url`, `publish_date`, `authors`, `headline`, `content`)
  3. `llm.stream_answer()` → text deltas. `[SRCn]` is rewritten to a markdown link using the n-th source's URL.
- `app/providers/`: one implementation per vendor; interfaces are in `base.py`.
  - `DEWEY_STACK=azure` (default): `azure_openai.py` + `azure_search.py`
  - `DEWEY_STACK=anthropic`: `anthropic_llm.py` (Claude) + `pgvector_store.py` (Voyage AI embeddings and rerank, Postgres/pgvector)
- `app/tools/`: prompts (`load_search_prompt`, `load_answer_prompt`) and the OpenAI `search_archive` tool schema.
- `app/models/`: config dataclasses (`NewsroomConfig` reads `NEWSROOM_NAME`, `ARCHIVE_START_DATE`, `DEFAULT_REGION`) and the Pydantic `SearchMetadata` schema used for Claude structured outputs.
- `app/setup.py` + `app/setup/`: Azure index, skillset and indexer setup. `app/setup_postgres.py`: Postgres schema and indexing.
- `data/*.json`: articles to index (see README for the format).

## Conventions

- Scripts run from the repo root with `app/` as the import root (`python app/main.py`); imports are top-level (`from models import ...`), not `app.models`.
- Keep `dewey.py` free of vendor SDK imports. New vendors go in `providers/` and are wired up in `providers/__init__.py`, imported lazily.
- All providers must return the same metadata/source shapes; `tips.py` and the citation linker depend on them.
- The metadata step's output is model-generated: validate it before it reaches a query. Dates are checked with `date.fromisoformat`, OData strings are escaped, and SQL values are always bound parameters.
- Claude calls use `client.beta.messages.*` because they opt into server-side refusal fallbacks (`fallbacks="default"`, beta `server-side-fallback-2026-07-01`). Check `stop_reason == "refusal"` before using output. Don't use forced `tool_choice` with current Claude models (400); use structured outputs.
- The embedding model/dimension is fixed per index. Changing `VOYAGE_EMBED_MODEL` or `VOYAGE_EMBED_DIM` requires a fresh database and a re-index.

## Checks

There is no test suite yet. Before committing:

```bash
ruff check app/ --select F
python -c "import sys; sys.path.insert(0, 'app'); import dewey, providers"
```

The `anthropic` stack can be exercised locally against a throwaway Postgres with pgvector by running `python app/setup_postgres.py` and then `python app/main.py`. This needs real `ANTHROPIC_API_KEY` and `VOYAGE_API_KEY` values.

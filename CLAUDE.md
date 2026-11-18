# Dewey

Gradio chat app that answers journalists' questions from a newsroom article archive, with
`[SRCn]` citations linked to article URLs. Forked from `phillymedia/dewey-ai` (The
Philadelphia Inquirer); this fork adds two non-Azure stacks, one of them fully open source.

## Layout

- `app/main.py`: Gradio UI. Builds the stack with `providers.build_stack()` and runs `Dewey`.
- `app/dewey.py`: vendor-neutral pipeline. `process()` runs three steps:
  1. `llm.generate_metadata()` → `{"question", "date_range": {"start_date", "end_date"}, "authors": [{"name"}]}`
  2. `retriever.retrieve(metadata)` → list of JSON strings (`url`, `publish_date`, `authors`, `headline`, `content`)
  3. `llm.stream_answer()` → text deltas. `[SRCn]` is rewritten to a markdown link using the n-th source's URL.
- `app/providers/`: one implementation per vendor; interfaces are in `base.py`.
  - `DEWEY_STACK=azure` (default): `azure_openai.py` + `azure_search.py`
  - `DEWEY_STACK=anthropic`: `anthropic_llm.py` (Claude) + `pgvector_store.py` (Postgres/pgvector)
  - `DEWEY_STACK=open`: `open_llm.py` (any OpenAI-compatible server, e.g. Ollama) + `pgvector_store.py`
  - `embeddings.py`: embedders/rerankers for the Postgres store, chosen by `EMBEDDING_PROVIDER` (`voyage` | `local`) and `RERANK_PROVIDER` (`voyage` | `local` | `none`). Defaults: `voyage` for `anthropic`, `local` for `open`. Local models need `requirements-local.txt`.
- `app/tools/`: prompts (`load_search_prompt`, `load_answer_prompt`) and the OpenAI `search_archive` tool schema.
- `app/models/`: config dataclasses (`NewsroomConfig` reads `NEWSROOM_NAME`, `ARCHIVE_START_DATE`, `DEFAULT_REGION`) and the Pydantic `SearchMetadata` schema used for Claude structured outputs.
- `app/setup.py` + `app/setup/`: Azure index, skillset and indexer setup. `app/setup_postgres.py`: Postgres schema and indexing (upserts only; never deletes articles).
- `app/import_archive.py` + `app/importers/`: archive importers, standard library only, writing article JSON to `data/`. One module per source, each with `NAME`, `HELP`, `add_arguments`, `default_prefix`, `iter_articles` (listed in `importers/__init__.py`): `wordpress_api`, `wordpress_export`, `feed`, `csv_import`, `drupal` (beta), `ghost` (beta). `common.py` has normalization, `ArticleWriter` (atomic replace of `<prefix>-NNNN.json`; `--limit` runs write `<prefix>-sample-*`), the `ImportReport` and credential-safe HTTP.
- `data/*.json`: articles to index (see README for the format).
- `tests/`: stdlib `unittest`. `tests/importers/` (fake sites on 127.0.0.1, fixtures in `tests/importers/fixtures/`), `tests/install/` (drives `install.py` through a pty with pexpect, against a temp copy of the repo; skipped without pexpect), `tests/providers/` (`build_where`; the real-Postgres tests run only when `DEWEY_TEST_DATABASE_URL` is set, building the real `schema_sql()` in a private schema they drop afterwards).
- `.github/workflows/tests.yml`: CI on pull requests and pushes to main, Python 3.11 and 3.12: ruff (F rules) + the unittest suite. No secrets, network or Postgres needed.
- `install.py` (repo root): interactive setup wizard that writes `.env`. Standard library only, because it runs before dependencies are installed. Its `SECTIONS` list must stay in sync with the settings read in `app/providers/__init__.py` and `app/models/core.py`. On the Postgres stacks its "Get your archive into Dewey" step runs an importer as a subprocess (`sys.executable app/import_archive.py ...`), a `--limit 20` trial first; `--dry-run` only prints the commands.

## Conventions

- Scripts run from the repo root with `app/` as the import root (`python app/main.py`); imports are top-level (`from models import ...`), not `app.models`.
- Keep `dewey.py` free of vendor SDK imports. New vendors go in `providers/` and are wired up in `providers/__init__.py`, imported lazily.
- All providers must return the same metadata/source shapes; `tips.py` and the citation linker depend on them.
- The metadata step's output is model-generated: validate it before it reaches a query. Dates are checked with `date.fromisoformat`, OData strings are escaped, and SQL values are always bound parameters.
- Author filters on the Postgres store (`AUTHOR_MATCH_SQL` in `pgvector_store.py`) ignore case and whitespace runs on both sides by applying the IMMUTABLE SQL function `dewey_byline_keys(text[])` to the stored and requested names, and must stay indexable (GIN index `articles_byline_keys_idx` on `dewey_byline_keys(authors)`): a filter the planner can only check after the HNSW scan loses a rare author's stories from vector search (`PostgresRecallTests`). Whole names only. Changing the function's body requires re-creating that index. The Azure path still matches exactly.
- Claude calls use `client.beta.messages.*` because they opt into server-side refusal fallbacks (`fallbacks="default"`, beta `server-side-fallback-2026-07-01`). Check `stop_reason == "refusal"` before using output. Don't use forced `tool_choice` with current Claude models (400); use structured outputs.
- The embedding model/dimension is fixed per index and recorded in the `index_info` table; `PgVectorStore.check_index()` refuses a mismatch. Changing `EMBEDDING_PROVIDER`, `EMBED_MODEL` or `EMBED_DIM` requires a fresh database and a re-index.
- `open_llm.py` must not assume the server enforces JSON schemas: it tries `json_schema`, falls back to `json_object`, strips `<think>` blocks and code fences, then validates with Pydantic.
- `.env` is loaded with `load_dotenv(..., interpolate=False)` everywhere, so values are literal; `install.py` writes single-quoted values that python-dotenv reads back verbatim (requires python-dotenv >= 1.2 for values ending in a backslash).
- Keep heavy local-model dependencies (torch, sentence-transformers) out of `requirements.txt`; they belong in `requirements-local.txt` and are imported lazily.
- Importers never invent data (no placeholder bylines or dates; skipped items are counted in the report) and never put secrets in argv. Logins and keys come from `WORDPRESS_USER`/`WORDPRESS_APP_PASSWORD`, `DRUPAL_USER`/`DRUPAL_PASSWORD`, `GHOST_CONTENT_API_KEY`; `install.py` reads them with getpass and passes them in the subprocess environment (`IMPORTER_ENV`), and printed commands show `read -rs NAME` placeholders. A new secret flag must be added to `install.SECRET_FLAGS` (a test checks this).

## Checks

Before committing (CI runs the first two):

```bash
ruff check app/ install.py tests/ --select F
python -m unittest discover -s tests -t .
python -c "import sys; sys.path.insert(0, 'app'); import dewey, providers"
```

Tests must not need network access, API keys or Postgres: use fake servers on 127.0.0.1, and gate anything needing a real database on `DEWEY_TEST_DATABASE_URL` (skip with a reason otherwise).

The Postgres stacks can be exercised locally against a throwaway Postgres with pgvector by running `python app/setup_postgres.py` and then `python app/main.py`. The `anthropic` stack needs a real `ANTHROPIC_API_KEY` (and `VOYAGE_API_KEY` unless both providers are `local`/`none`); the `open` stack needs a running model server and Hugging Face access for the first model download.

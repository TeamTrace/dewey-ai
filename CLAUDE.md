# Dewey

Newsroom archive engine: CMS importers, metadata-aware search (Postgres/pgvector or Azure AI
Search, with date and byline filters) and an MCP server that agent harnesses (Hermes Agent,
DeepSeek Harness, Claude Code) and chat UIs (Open WebUI, LibreChat) connect to. Forked from
`phillymedia/dewey-ai` (The Philadelphia Inquirer), a Gradio chat app with `[SRCn]` citations;
this fork adds two non-Azure stacks (one fully open source) and makes MCP the main interface.
Dewey has no login system, GUI setup or chat product of its own; the Gradio page is a localhost
demo. Docs are for newsrooms of any technical level: plain, and honest about limits.

## Layout

- `app/mcp_server.py`: the main interface. MCP server (official `mcp` SDK, FastMCP) for agents and chat apps; see `docs/MCP.md`. Tools: `search_archive` (retriever only, no LLM; the recommended tool, since clients bring their own model) and `ask_archive` (`Dewey.process(..., show_steps=False)`). stdio by default; `--http` serves Streamable HTTP and requires `DEWEY_MCP_TOKEN` (`--no-auth` only on loopback and never with `--public-url`). Host/Origin checks (DNS rebinding) apply only to loopback binds; on `0.0.0.0` (the Docker image) the token is the only guard. `DEWEY_MCP_TEST_STACK=1` is a test-only hook that loads `tests/mcp/fake_stack.py`.
- `app/import_archive.py` + `app/importers/` (standard library only): CMS importers (WordPress, RSS/Atom, CSV, Drupal, Ghost) that write `data/*.json`.
- `app/main.py`: Gradio demo page with no login. Builds the stack with `providers.build_stack()` and runs `Dewey`. Launch settings come from `build_launch_plan(env)`, which starts nothing (tests call it): telemetry, API page, `/monitoring`, Gradio's own MCP server and share links off; `queue(api_open=False, default_concurrency_limit=DEWEY_CONCURRENCY or 4)`; 1 MB upload cap; host/port from `GRADIO_SERVER_NAME`/`GRADIO_SERVER_PORT`, then `PORT`. `check_exposure()` makes `main()` exit 2 on a non-loopback address unless `DEWEY_DEMO_BEHIND_AUTH_PROXY=true` (a sign-in proxy in front) or `DEWEY_DEMO_PUBLISHED_ON_LOOPBACK=true` (a container whose port is published on the host's 127.0.0.1 only; set by the Compose `demo` service and deliberately not offered in the refusal message, since on a bare host it exposes the page: the Host check stops browsers, not direct requests). `RequestGuard` (ASGI middleware passed via `launch(app_kwargs=...)`) 404s every `/monitoring` path (Gradio 6.16.0 serves `/monitoring/summary` even with monitoring off), refuses non-loopback `Host` headers (DNS rebinding) and any `Origin` other than the page's own (Gradio's CORS check covers only the exact names localhost/127.0.0.1) unless `DEWEY_DEMO_BEHIND_AUTH_PROXY=true`, and sends a `script-src` CSP on every response. Gradio 6: no `show_api` (use `footer_links` and per-event `api_visibility="private"`), and `Chatbot` history arrives as lists of parts, flattened by `dewey.message_text()`.
- `app/dewey.py`: vendor-neutral pipeline. `process()` runs three steps:
  1. `llm.generate_metadata()` → `{"question", "date_range": {"start_date", "end_date"}, "authors": [{"name"}]}`
  2. `retriever.retrieve(metadata)` → list of JSON strings (`url`, `publish_date`, `authors`, `headline`, `content`)
  3. `llm.stream_answer()` → text deltas, shown through `render_answer()`: `[SRCn]` becomes a markdown link to the n-th source's URL (made safe by `citation_href()`), and every other `[ ] < \ ://` the model wrote is turned into an HTML character reference.
- `app/providers/`: one implementation per vendor; interfaces are in `base.py`.
  - `DEWEY_STACK=azure` (default): `azure_openai.py` + `azure_search.py`
  - `DEWEY_STACK=anthropic`: `anthropic_llm.py` (Claude) + `pgvector_store.py` (Postgres/pgvector)
  - `DEWEY_STACK=open`: `open_llm.py` (any OpenAI-compatible server, e.g. Ollama, vLLM or hosted OpenRouter) + `pgvector_store.py`
  - `embeddings.py`: embedders/rerankers for the Postgres store, chosen by `EMBEDDING_PROVIDER` (`voyage` | `local`) and `RERANK_PROVIDER` (`voyage` | `local` | `none`). Defaults: `voyage` for `anthropic`, `local` for `open`. Local models need `requirements-local.txt`.
- `app/tools/`: prompts (`load_search_prompt`, `load_answer_prompt`) and the OpenAI `search_archive` tool schema.
- `app/models/`: config dataclasses (`NewsroomConfig` reads `NEWSROOM_NAME`, `ARCHIVE_START_DATE`, `DEFAULT_REGION`) and the Pydantic `SearchMetadata` schema used for Claude structured outputs.
- `app/setup.py` + `app/setup/`: Azure index, skillset and indexer setup. `app/setup_postgres.py`: Postgres schema and indexing (upserts only; never deletes articles).
- `app/import_archive.py` + `app/importers/`: archive importers, standard library only, writing article JSON to `data/`. One module per source, each with `NAME`, `HELP`, `add_arguments`, `default_prefix`, `iter_articles` (listed in `importers/__init__.py`): `wordpress_api`, `wordpress_export`, `feed`, `csv_import`, `drupal` (beta), `ghost` (beta). `common.py` has normalization, `ArticleWriter` (atomic replace of `<prefix>-NNNN.json`; `--limit` runs write `<prefix>-sample-*`), the `ImportReport` and credential-safe HTTP.
- `data/*.json`: articles to index (format: docs/IMPORTING.md).
- `tests/`: stdlib `unittest`. `tests/importers/` (fake sites on 127.0.0.1, fixtures in `tests/importers/fixtures/`), `tests/install/` (drives `install.py` through a pty with pexpect, against a temp copy of the repo; skipped without pexpect), `tests/providers/` (`build_where`; the real-Postgres tests run only when `DEWEY_TEST_DATABASE_URL` is set, building the real `schema_sql()` in a private schema they drop afterwards), `tests/docs/` (link, anchor and paste-safety checks for `README.md` and `docs/*.md`, and for commands `install.py` and the importers print).
- `.github/workflows/tests.yml`: CI on pull requests and pushes to main, Python 3.11, 3.12 and 3.13: ruff (F rules) + the unittest suite. No secrets, network or Postgres needed.
- `install.py` (repo root): interactive setup wizard that writes `.env`. Standard library only, because it runs before dependencies are installed. Its `SECTIONS` list must stay in sync with the settings read in `app/providers/__init__.py` and `app/models/core.py`. On the Postgres stacks its "Get your archive into Dewey" step runs an importer as a subprocess (`sys.executable app/import_archive.py ...`), a `--limit 20` trial first; `--dry-run` only prints the commands.
- Deployment: `Dockerfile` (python:3.12-slim, non-root uid 1000, `INSTALL_LOCAL_MODELS` build arg; default command is the MCP server on `0.0.0.0:8765`, TCP healthcheck), `docker-compose.yml` (`dewey` = MCP server, published on `127.0.0.1:8765` only, `DEWEY_MCP_TOKEN` required via `${...:?}`, alias `dewey-mcp` on the fixed-name `dewey-mcp-clients` network for chat-UI containers; `db` = `pgvector/pgvector:pg18`, never published and not on that network; `demo` profile = Gradio page on `127.0.0.1:7860`; `ollama` profile; the indexer is `docker compose run --rm -v ./data:/app/data:ro dewey python app/setup_postgres.py`), `deploy/fly.toml.example` and `deploy/render.yaml.example` (MCP server + database only; TCP health checks because `/mcp` answers 401 without the token), guides in `docs/DEPLOYMENT.md` and `docs/MCP.md`. Platform and client claims in those guides link to the source they were checked against (GitHub-hosted docs or source), or are marked unverified. Tests: `tests/deploy/` (runs `docker compose config` when the docker CLI is installed; no daemon needed).
- Docs: `README.md` is the front door; the guides are `docs/INSTALL.md` (install and first run on macOS, Windows and Linux), `docs/STACKS.md` (the five setups side by side, described rather than recommended), `docs/IMPORTING.md` (importers, the article file format, indexing), `docs/AZURE.md` (the `azure` stack), `docs/MCP.md` (connecting AI tools), `docs/DEPLOYMENT.md` (servers and hosting), `docs/MODELS.md` (chat models on `open`) and `docs/GLOSSARY.md` (terms, linked on first use). Facts about Dewey in them come from the code; claims about other products link to their source or are marked unverified.

## Writing issues, PRs and notes

- Tight, AP style: short sentences, facts first, no filler or praise. Cite the source of each claim (a link, a file and line, or a command you ran) and mark anything you couldn't check as unverified.
- Every issue opens with a short summary block, then `---`, then the detail:

  ```
  **Summary:** what's wrong and why it matters, in two sentences at most.
  **Recommendation:** what to do, in one sentence.
  **Gotchas:** traps for whoever picks it up, in two short items at most.
  ```
- To hand an issue to Claude, add the `claude` label (`.github/workflows/claude.yml`); Claude isn't a GitHub user, so it can't be assigned.

## Conventions

- Scripts run from the repo root with `app/` as the import root (`python app/main.py`); imports are top-level (`from models import ...`), not `app.models`.
- Keep `dewey.py` free of vendor SDK imports. New vendors go in `providers/` and are wired up in `providers/__init__.py`, imported lazily.
- All providers must return the same metadata/source shapes; `tips.py` and the citation linker depend on them.
- The metadata step's output is model-generated: validate it before it reaches a query. Dates are checked with `date.fromisoformat`, OData strings are escaped, and SQL values are always bound parameters.
- Author filters on the Postgres store (`AUTHOR_MATCH_SQL` in `pgvector_store.py`) ignore case and whitespace runs on both sides by applying the IMMUTABLE SQL function `dewey_byline_keys(text[])` to the stored and requested names, and must stay indexable (GIN index `articles_byline_keys_idx` on `dewey_byline_keys(authors)`): a filter the planner can only check after the HNSW scan loses a rare author's stories from vector search (`PostgresRecallTests`). Whole names only. Changing the function's body requires re-creating that index. The Azure path still matches exactly.
- Archive text and model output are untrusted. Sources are sent after `SOURCES_PREAMBLE` (data, not instructions), and answers reach users only through `render_answer()`, so injected article text can't make the chat load an image or link that carries a question elsewhere. Don't bypass it or put raw source URLs into markdown.
- Claude calls use `client.beta.messages.*` because they opt into server-side refusal fallbacks (`fallbacks="default"`, beta `server-side-fallback-2026-07-01`). Check `stop_reason == "refusal"` before using output. Don't use forced `tool_choice` with current Claude models (400); use structured outputs.
- The embedding model/dimension is fixed per index and recorded in the `index_info` table; `PgVectorStore.check_index()` refuses a mismatch. Changing `EMBEDDING_PROVIDER`, `EMBED_MODEL` or `EMBED_DIM` requires a fresh database and a re-index.
- `open_llm.py` must not assume the server enforces JSON schemas: it tries `json_schema`, falls back to `json_object`, strips `<think>` blocks and code fences, then validates with Pydantic.
- On OpenRouter (base URL host `openrouter.ai` or a subdomain), `open_llm.py` sends `extra_body={"provider": ...}` on every request: `OPENROUTER_PRIVACY` `strict` (default: `data_collection: "deny"` + `zdr: true`), `standard` (deny only) or `off` (none), plus `require_parameters: true` on search planning only; other servers get no `provider` object (`build_stack` warns on stderr if `OPENROUTER_PRIVACY` is set for them, refuses a non-https OpenRouter URL, and treats a blank `OPEN_LLM_BASE_URL` as the Ollama default). Routing failures raise `OpenRouterRoutingError` and must never reach the `json_object` fallback (tests: `tests/open_llm/`; user docs: `docs/MODELS.md`).
- `.env` is loaded with `load_dotenv(..., interpolate=False)` everywhere, so values are literal; `install.py` writes single-quoted values that python-dotenv reads back verbatim (requires python-dotenv >= 1.2 for values ending in a backslash).
- Keep heavy local-model dependencies (torch, sentence-transformers) out of `requirements.txt`; they belong in `requirements-local.txt` and are imported lazily.
- No login system. Don't add accounts or auth to the demo page or the MCP server beyond the bearer token: access control is `DEWEY_MCP_TOKEN` + TLS in front + network placement (never publish Postgres; Compose publishes ports on 127.0.0.1 only), and chat apps decide which of their users get the tool. The demo page stays on loopback unless one of the two `DEWEY_DEMO_*` flags says otherwise. Never print or log the token.
- Never write to stdout from `dewey.py`, `providers/`, `tools/` or `models/`: in the MCP server's stdio mode, stdout is the JSON-RPC channel and one stray line breaks the client. Use `print(..., file=sys.stderr)` or `logging`. `mcp_server.py` also runs tool calls with stdout redirected to stderr as a backstop; `tests/mcp/test_stdio.py` checks that every stdout line is JSON-RPC.
- Importers never invent data (no placeholder bylines or dates; skipped items are counted in the report) and never put secrets in argv. Logins and keys come from `WORDPRESS_USER`/`WORDPRESS_APP_PASSWORD`, `DRUPAL_USER`/`DRUPAL_PASSWORD`, `GHOST_CONTENT_API_KEY`; `install.py` reads them with getpass and passes them in the subprocess environment (`IMPORTER_ENV`), and printed commands show `read -rs NAME` placeholders. A new secret flag must be added to `install.SECRET_FLAGS` (a test checks this).
- Shell commands in the docs, and commands Dewey prints for people to copy (`install.py`, the importers' `--help`), must paste safely into an interactive zsh (macOS's default shell): no `#` comments (zsh passes them along as words, and an apostrophe in one opens a quote), CAPITALS for placeholders, quotes around anything with `[ ]` or `?` (single quotes if it has `!`), and explanations on a line of their own. `tests/docs/test_docs.py` checks this.

## Checks

Before committing (CI runs the lint and test lines). Keep `-t .` on the test line: without it,
`tests/importers` and `tests/mcp` shadow `app/importers` and the `mcp` SDK. Run the Compose line
with `POSTGRES_PASSWORD` and `DEWEY_MCP_TOKEN` set, and never without `--quiet`, which stops it
printing `.env`'s secrets (`tests/deploy` also runs it against a fake `.env`).

```bash
ruff check app/ install.py tests/ --select F
python -m unittest discover -s tests -t .
python -c "import sys; sys.path.insert(0, 'app'); import dewey, providers, mcp_server"
docker compose config --quiet
```

Tests must not need network access, API keys or Postgres: use fake servers on 127.0.0.1, and gate anything needing a real database on `DEWEY_TEST_DATABASE_URL` (skip with a reason otherwise).

The Postgres stacks can be exercised locally against a throwaway Postgres with pgvector by running `python app/setup_postgres.py` and then `python app/main.py` (demo page) or `python app/mcp_server.py --http --no-auth` (MCP, loopback only). The `anthropic` stack needs a real `ANTHROPIC_API_KEY` (and `VOYAGE_API_KEY` unless both providers are `local`/`none`); the `open` stack needs a running model server and Hugging Face access for the first model download.

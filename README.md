# Dewey
[![Python Version](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Azure OpenAI](https://img.shields.io/badge/Azure%20OpenAI-Powered-green.svg)](https://azure.microsoft.com/)
[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](https://choosealicense.com/licenses/mit/)

Dewey is an AI-powered librarian designed to help newsrooms make their archives easy to search, making use of LLMs to provide cited responses. 

Archival research methods are cumbersome. Often times, they rely on keyword searches and date range filtering, making it difficult to surface topics without specific preexisting knowledge. Moreover, archives can live across disparate source systems/databases because of how content management systems evolve over time. Unifying these systems with a state-of-the-art search engine hopes to make archive research easier and more efficient for reporters.


## Acknowledgements
Special thank you to the [Lenfest Institute AI Collaborative and Fellowship Program](https://www.lenfestinstitute.org/institute-news/lenfest-institute-openai-microsoft-ai-collaborative-fellowship/) for making this project happen.
- Patrick Kerkstra, Ross Maghielse - newsroom guidance, support, and tester recruiting
- Tommy Rowan, Nick Vidala, Jennifer Friedman-Perez - Alpha testing users
- Lenfest Institute - for providing and securing the grant that made this project possible
- Microsoft - for co-funding the grant and jumpstarting our progress
- OpenAI - for co-funding the grant and providing technical support


## Installation

1. Clone the repository
```bash
git clone https://github.com/phillymedia/dewey-ai.git
cd dewey-ai
```

2. Create virtual environment
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows:
.venv\Scripts\activate
```

3. Run the setup wizard
```bash
python install.py
```
The wizard asks which stack you want (see [Choosing a stack](#choosing-a-stack)), tells you where to create any account or key that stack needs (with an option to open each link), and asks for the credentials. Secrets are typed hidden. It then writes `.env` (readable only by you, with a backup of any previous one), and can optionally:
- install the Python dependencies
- check that your keys, database and model server work
- on the `anthropic` and `open` stacks, get your archive into `data/` with one of the [importers](#importing-your-archive): it asks only what that importer needs, runs a 20-article trial first, shows the importer's report, then offers the full import and prints the command to run it again later
- index the articles in `data/`

Re-run it any time to change a setting; your saved values are offered as defaults. `python install.py --dry-run` asks the same questions but only prints the result, with secrets masked: it writes no `.env` and imports nothing (it prints the import commands instead).

Prefer to configure by hand? Install dependencies with `pip install -r requirements.txt`, copy `.env.template` to `.env`, and follow the sections below.


## Choosing a stack

Dewey runs on one of three interchangeable stacks, selected with `DEWEY_STACK` in `.env`:

| Step | `azure` (default) | `anthropic` | `open` |
|---|---|---|---|
| Search planning (question, dates, authors) | Azure OpenAI, forced tool call | Claude, structured outputs | Open-weight model on your own server, JSON output validated against a schema |
| Cited answer | Azure OpenAI, streamed | Claude, streamed | Open-weight model, streamed |
| Embeddings | Azure OpenAI `text-embedding-3-large` | Voyage AI `voyage-4` (hosted) | `voyage-4-nano` (open-weight, runs locally) |
| Search engine | Azure AI Search (hybrid + semantic ranker) | Postgres + pgvector (keyword + vector, fused with RRF) | same as `anthropic` |
| Reranking | Azure semantic ranker | Voyage AI `rerank-3` (hosted) | `BAAI/bge-reranker-v2-m3` (open-weight, runs locally) |
| Indexing | `app/setup.py` (Blob Storage + Azure indexer) | `app/setup_postgres.py` (runs locally) | same as `anthropic` |
| Paid accounts needed | Azure | Anthropic, Voyage AI | none |

On the two Postgres stacks, embeddings and reranking can be mixed and matched with `EMBEDDING_PROVIDER` (`voyage` | `local`) and `RERANK_PROVIDER` (`voyage` | `local` | `none`). For example, `DEWEY_STACK="anthropic"` with both set to `local` uses Claude for answers and no Voyage account.

Anthropic does not offer an embedding model; its documentation points to Voyage AI ([Anthropic embeddings guide](https://platform.claude.com/docs/en/build-with-claude/embeddings)). Indexes are **not** portable between embedding models: vectors from different models can't be compared, so changing `EMBEDDING_PROVIDER`, `EMBED_MODEL` or `EMBED_DIM` means indexing into a fresh database. Dewey records which model built each database and refuses to search it with a different one.

### Common setup for the Postgres stacks (`anthropic` and `open`)

1. `DATABASE_URL`: a Postgres database with the [pgvector](https://github.com/pgvector/pgvector) extension available (self-hosted, or a managed provider that supports pgvector). pgvector 0.8+ is recommended; with older versions, very selective date/author filters can return fewer vector matches.
2. Put your articles in `data/`, either with an [importer](#importing-your-archive) or as JSON files you make yourself (format under [Deployment](#deployment-azure-stack)), and index them with the same `.env` you'll run Dewey with:
   ```bash
   python app/setup_postgres.py
   ```
   This creates the tables and indexes, splits each article into ~380-word chunks with ~70-word overlap (an approximation of the Azure path's 512-token/96-token split), embeds them, and stores them. Re-running it updates articles in place (and embeds every article in `data/` again; it never removes articles from the database).
3. Launch Dewey: `python app/main.py`.

### Running on the `anthropic` stack

1. Get credentials:
   - `ANTHROPIC_API_KEY`: from the Claude Console.
   - `VOYAGE_API_KEY`: a model API key from MongoDB Atlas → AI Model APIs (Voyage AI is part of MongoDB). Not needed if you set `EMBEDDING_PROVIDER` and `RERANK_PROVIDER` to `local` or `none` (see the next section for the local setup).
2. In `.env`, set `DEWEY_STACK="anthropic"`, `DATABASE_URL`, and the keys above. Optionally set `NEWSROOM_NAME`, `ARCHIVE_START_DATE` and `DEFAULT_REGION`.
3. Follow the common setup above.

Model and cost notes: both Claude steps default to `claude-opus-5-5`. The search-planning step runs at `low` effort and the answer at `medium`; override them with `ANTHROPIC_MODEL`, `ANTHROPIC_METADATA_EFFORT` and `ANTHROPIC_ANSWER_EFFORT`. Requests opt into Anthropic's server-side refusal fallback: if a safety classifier declines a request, the API retries it on a fallback model in the same call. If the whole chain declines, Dewey says so in the answer instead of failing silently.

### Running fully open source (`open` stack)

Every component runs on hardware you control, with no accounts or API keys:

| Component | What it is | License |
|---|---|---|
| Chat model | Any open-weight model on an OpenAI-compatible server. Examples: [Qwen3-8B](https://hf.co/Qwen/Qwen3-8B) (8B parameters), [gpt-oss-20b](https://hf.co/openai/gpt-oss-20b) (21B parameters) | Apache 2.0 (both examples) |
| Model server | [Ollama](https://github.com/ollama/ollama) (default), or vLLM / llama.cpp's server | MIT (Ollama) |
| Embeddings | [voyage-4-nano](https://hf.co/MongoDB/voyage-4-nano), 347M parameters, run via the `voyageai` SDK's local mode | Apache 2.0 |
| Reranker | [bge-reranker-v2-m3](https://hf.co/BAAI/bge-reranker-v2-m3), 568M parameters, run via sentence-transformers | Apache 2.0 |
| Database | PostgreSQL + [pgvector](https://github.com/pgvector/pgvector) | PostgreSQL License (Postgres); see the pgvector repo |
| Python libraries | gradio, openai (client only), voyageai, sentence-transformers, psycopg, pgvector, pydantic | Apache 2.0 / MIT / BSD, except psycopg (LGPL-3.0) |

"Open-weight" means the trained model is free to download, run and modify under the license above; it does not mean the training data is published.

Steps:

1. **Install the local-model dependencies** on top of `requirements.txt`:
   ```bash
   pip install -r requirements-local.txt
   ```
   This installs PyTorch. On a machine without an NVIDIA GPU, installing the CPU-only PyTorch build first avoids several GB of GPU libraries:
   ```bash
   pip install torch --index-url https://download.pytorch.org/whl/cpu
   pip install -r requirements-local.txt
   ```
2. **Run a chat model.** With [Ollama](https://ollama.com) installed:
   ```bash
   ollama pull qwen3:8b        # or another model; larger models need more RAM/VRAM
   ```
   Ollama serves an OpenAI-compatible API at `http://localhost:11434/v1`, which is Dewey's default. For another server, set `OPEN_LLM_BASE_URL` (and `OPEN_LLM_API_KEY` if it needs one).
3. **Configure `.env`:**
   ```bash
   DEWEY_STACK="open"
   OPEN_LLM_MODEL="qwen3:8b"
   DATABASE_URL="postgresql://user:password@localhost:5432/dewey"
   NEWSROOM_NAME="Your Newsroom"      # optional, see .env.template
   ```
4. **Follow the common setup above.** The first run of `app/setup_postgres.py` downloads the embedding model (and the first question downloads the reranker) from Hugging Face into `~/.cache/huggingface`. After that, everything runs offline except for reaching your own Postgres and model server.

What to expect, honestly:
- **Answer quality** depends mostly on the chat model. Small local models are more likely than Claude or GPT-class models to miss a citation, cite the wrong source, or ignore the "say you can't answer" rule. Check citations against the linked articles before relying on an answer.
- **Search planning:** Dewey asks the server for JSON that matches a schema. If the server can't enforce schemas, Dewey falls back to plain JSON mode and validates the reply. If the reply still doesn't fit, Dewey searches the question as typed, with no date/author filters (the log says when this happens).
- **Speed:** indexing a large archive on CPU can take hours; a GPU makes embedding much faster. Reranking adds noticeable latency per question on CPU. Set `RERANK_PROVIDER="none"` to skip it and keep the hybrid-search order.
- Models that write their reasoning inside `<think>…</think>` tags (e.g. Qwen3) have that text removed before it reaches the journalist.

## Importing your archive

Dewey searches article files (JSON) in `data/`. The importers make those files from your CMS, a feed or a spreadsheet. `python install.py` walks you through one (a trial run first), or run them yourself from the Dewey folder:

```bash
python app/import_archive.py --help            # list the sources
python app/import_archive.py csv --help        # every option for one source, and where to find each value
```

Importers only read: they never change anything on your site. They keep published, public stories only: drafts, scheduled, private and password-protected posts are left out, and so are members-only posts where the importer can tell (the report counts what was skipped). They never invent data: a story without a byline is imported with no author rather than a guessed one. They use only the Python standard library, so they work before you install `requirements.txt`. Index the files with `python app/setup_postgres.py` (`anthropic` and `open` stacks). `python app/setup.py` (`azure`) reads the same files, but currently skips articles that have no byline.

| Source | Example | What you need, and where to find it |
|---|---|---|
| WordPress site (REST API) | `python app/import_archive.py wordpress-api --site https://www.example.com --limit 20` | Nothing for public posts. Check `<site>/wp-json/` shows JSON in a browser. A login is only needed if the site closes its API to the public (or for `--include-private`): an **Application Password**, from wp-admin > Users > Profile > Application Passwords (not your normal password). |
| WordPress export file | `python app/import_archive.py wordpress-export --file example.WordPress.2024-05-01.xml --limit 20` | The file from wp-admin > Tools > Export > "All content" > Download Export File. `.xml`, `.xml.gz`, `.zip` or a folder of them. No password: works offline, for sites whose API is closed or that no longer exist. |
| RSS or Atom feed | `python app/import_archive.py feed --url https://www.example.com/feed/ --limit 20` | The feed address (WordPress `/feed/`, Drupal `/rss.xml`, Ghost `/rss/`), or your homepage if it advertises one, or saved feed files with `--file`. Public feeds only. Feeds usually list just the latest stories; the importer follows links to older pages when the feed has them. |
| CSV or spreadsheet | `python app/import_archive.py csv --file articles.csv --limit 20` | A table with a header row and one row per article (save or download a spreadsheet as CSV, choosing UTF-8 if your program offers it, or use a database export). Columns are found by name and the mapping is printed first. Dates like 05/01/2024 are never guessed: the import stops until you add `--date-format`. |
| Drupal (beta) | `python app/import_archive.py drupal --site https://www.example.com --limit 20` | Drupal 8.7+ with the core JSON:API module on (`<site>/jsonapi` shows JSON). A login only if JSON:API refuses anonymous visitors: an account with no extra roles, through the core HTTP Basic Authentication module. |
| Ghost (beta) | `python app/import_archive.py ghost --url https://example.ghost.io --limit 20` | A **Content API key**: Ghost Admin > Settings > Integrations > Add custom integration (name it Dewey); copy its API URL and Content API key (not the Admin API key). |

Where these menu names and addresses come from (source code read on GitHub, November 2026): WordPress [`user-edit.php`](https://github.com/WordPress/wordpress-develop/blob/trunk/src/wp-admin/user-edit.php) (Application Passwords on the profile page; the button is "Add Application Password" since 6.8, "Add New Application Password" before), [`menu.php`](https://github.com/WordPress/wordpress-develop/blob/trunk/src/wp-admin/menu.php) and [`export.php`](https://github.com/WordPress/wordpress-develop/blob/trunk/src/wp-admin/export.php) (Tools > Export, "All content", "Download Export File"), [`rest-api.php`](https://github.com/WordPress/wordpress-develop/blob/trunk/src/wp-includes/rest-api.php) (`wp-json`) and [`class-wp-rewrite.php`](https://github.com/WordPress/wordpress-develop/blob/trunk/src/wp-includes/class-wp-rewrite.php) (`/feed/`); Drupal core's [`jsonapi`](https://github.com/drupal/drupal/tree/8.7.x/core/modules/jsonapi) module (first in 8.7.x, absent from 8.6.x), [`basic_auth.info.yml`](https://github.com/drupal/drupal/blob/11.x/core/modules/basic_auth/basic_auth.info.yml) ("HTTP Basic Authentication") and the front-page view's [`rss.xml`](https://github.com/drupal/drupal/blob/10.3.x/core/modules/node/config/optional/views.view.frontpage.yml) path; Ghost 6.0.0's [Integrations settings](https://github.com/TryGhost/Ghost/blob/v6.0.0/apps/admin-x-settings/src/components/settings/advanced/integrations/CustomIntegrationModal.tsx) ("Add custom integration", "Content API key", "API URL", "Admin API key", under Advanced), [`RSSRouter.js`](https://github.com/TryGhost/Ghost/blob/v6.0.0/ghost/core/core/frontend/services/routing/RSSRouter.js) (`/rss/`) and [`secret.ts`](https://github.com/TryGhost/framework/blob/main/packages/security/lib/secret.ts) (Content API keys are 26 hex characters). Other versions may differ; spreadsheet menus are not named here because they weren't checked.

**Beta** means the Drupal and Ghost importers were tested against sample exports only (fixtures shaped like those systems' API responses), not yet against a live newsroom site. Check their trial run closely, and tell us what you find.

**Passwords and keys go in environment variables, not on the command line** (commands are saved in your shell history and can show up in the list of running processes). The importers read `WORDPRESS_USER` and `WORDPRESS_APP_PASSWORD`, `DRUPAL_USER` and `DRUPAL_PASSWORD`, and `GHOST_CONTENT_API_KEY`. In bash, as each importer's `--help` shows:

```bash
read -rs GHOST_CONTENT_API_KEY && export GHOST_CONTENT_API_KEY   # paste it, press Enter; it isn't shown or saved
python app/import_archive.py ghost --url https://example.ghost.io --limit 20
```

`install.py` asks for these hidden and passes them to the importer the same way; the command it prints for later uses the `read -rs` line in place of the value. They are not written to `.env`.

**Start with a trial run.** `--limit 20` stops after 20 articles and writes them to `<name>-sample-0001.json` (unless you also pass `--prefix`), so a trial never replaces an earlier full import. Open the file and check a few stories (headline, text, byline, date, link) before importing everything. Delete the `-sample` files once the full import is done: indexing them too doesn't duplicate articles (same ids), but it embeds them again, which costs money with hosted Voyage AI.

**Read the report.** Every run ends with a report of where the articles came from and what was left out: how many were imported, their publish-date range, how many have no byline (they are searchable, but author filters can't find them), duplicates, every skipped story counted by reason (draft, members-only, no date, no URL, ...), and the files written. The CSV importer also prints its column mapping before it starts. If the numbers look wrong, fix the settings (each importer's `--help` lists them) and run it again.

Where files go: `data/<name>-0001.json`, `-0002.json`, ... (1,000 articles per file; `<name>` comes from the source, e.g. `wordpress-example-com`). Re-running the same import replaces its earlier files, but only once the new run has finished; a failed or empty run leaves them as they were. Large WordPress, Drupal and Ghost sites can be imported a year at a time with `--after` and `--before`.

**Not handled yet:**
- **Incremental re-sync.** There is no "fetch only what changed since last time". To pick up new stories, run the import again (it reads the whole source again) and then the indexer (which embeds every article in `data/` again). Nothing runs on a schedule.
- **Stories deleted or unpublished in the CMS.** Re-running the import drops them from `data/`, but not from Dewey's database: indexing only adds and updates articles. To remove them, index into a fresh database, or delete them by id in Postgres (`DELETE FROM articles WHERE id = '...'`; their chunks go with them).
- **Text the public can't see.** The importers get what the site gives an anonymous visitor (unless you give a login), so text a paywall or membership plugin hides from the WordPress API is missing, while a paywall that only works in the reader's browser hides nothing from it. Ghost members-only posts are skipped by default, and many feeds carry only excerpts (`--require-full-text` skips those). Check a few paywalled stories in the trial run.
- **Other CMSs** (Arc XP, Brightspot, ...): no dedicated importer yet. Use an RSS/Atom feed or a CSV export, if the CMS offers one.

## Configuration (`azure` stack)

To run this project on the default `azure` stack, you will need to add the following environment variables to your .env file

1. Copy environment template
```
cp .env.template .env
```

2. Configure your `.env` file
*Note: While most environment variables must specify preexisting resources and deployments,`AZURE_SEARCH_INDEX_NAME` represents the desired name for your search index.*
```
# Azure OpenAI Configuration
AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com"
AZURE_OPENAI_API_KEY="your-azure-openai-api-key"
EMBEDDING_DEPLOYMENT_NAME="your-embedding-deployment-name"
EMBEDDING_MODEL_NAME="text-embedding-3-large"
CHATGPT_DEPLOYMENT_NAME="your-chatgpt-deployment-name"
CHATGPT_MODEL_NAME="gpt-5"

# Azure AI Search Configuration
AZURE_SEARCH_ENDPOINT="https://your-search-service.search.windows.net"
AZURE_SEARCH_API_KEY="your-azure-search-api-key"
AZURE_SEARCH_INDEX_NAME="your-search-index-name"  

# Azure Blob Storage Configuration
AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=..."
AZURE_STORAGE_CONTAINER_NAME="your-storage-container-name"
```

## Deployment (`azure` stack)
1. Prepare Your Documents

Any articles you wish to upload should be in JSON format in the `data/` folder. Each document should follow this structure:

```json
{
    "id": "unique-document-id",
    "headline": "Article Title",
    "content": "Full article content...",
    "url": "https://example.com/article",
    "authors": ["Author 1", "Author 2", ...],
    "publish_date": "2025-09-09T12:00:00Z",
}
```

- `id` (optional) - A unique ID for your article. Will be autopopulated if you do not provide
- `headline` - A title to your article
- `content` - The full text to your article. It is up to you how to format this. **This will end up being your searchable field**
- `url` - A URL facing your article. This is critical for citation functionality
- `authors` - A list of author names from the article's byline
- `publish_date` - The date in which your article as published ([ISO 8601 format](https://en.wikipedia.org/wiki/ISO_8601))

2. Run Setup Script

```bash
python app/setup.py
```

This will:
- Validate your configuration
- Create Azure AI Search index with proper schema
- Set up skillsets for document processing (chunking and embedding)
- Create indexer for automated processing
- Upload documents to blob storage
- Process documents through AI Search pipeline

3. Launch Dewey
```bash
python main.py
```
The application will be available at `http://localhost:7860`. This project uses Gradio to create a user-friendly web interface for our machine learning model. You can learn more about Gradio at https://www.gradio.app/.


## Usage

1. **Open your browser** to `http://localhost:7860`
2. **Ask questions** in natural language:
    - "What articles did *author X* about the election last month?"
    - "Summarize the last decade of coverage on *topic X*."
    - "Find me the best restaurants mentioned in 2024."
3. **Expand Dewey's though process** to see *what was searched* and *how many articles* were retrieved

Bylines: on the `anthropic` and `open` stacks, an author filter ignores capitals and extra spaces ("jane doe" finds "Jane Doe"), but it needs the whole name as the byline has it ("Doe" alone finds nothing). In a Postgres database created with the plain `C` locale, only the letters A-Z are matched regardless of case, so "JOSÉ" won't find "José" there. The `azure` stack matches names exactly. Articles imported without a byline are never found by an author filter.


## Contributing

All contributions are welcome! More details to come. In the meantime, please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

### Running the tests

The same checks run on every pull request (`.github/workflows/tests.yml`, Python 3.11 and 3.12). They need no API keys, network access or database:

```bash
pip install -r requirements.txt pexpect   # pexpect drives the setup wizard in tests/install/
ruff check app/ install.py tests/ --select F
python -m unittest discover -s tests -t .
```

Without pexpect, the wizard tests are skipped. The Postgres filter tests in `tests/providers/` are skipped unless `DEWEY_TEST_DATABASE_URL` points at a throwaway Postgres with pgvector; they build Dewey's tables in a new schema of their own and drop it afterwards (the `vector` extension, if the database lacks it, is created there too), so they don't touch an existing index:

```bash
DEWEY_TEST_DATABASE_URL="postgresql://postgres:dewey@localhost:5432/postgres" python -m unittest tests.providers.test_pgvector_store
```

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
- index the articles in `data/`

Re-run it any time to change a setting; your saved values are offered as defaults. `python install.py --dry-run` asks the same questions but only prints the result, with secrets masked.

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
2. Put your articles in `data/` (same JSON format as below) and index them with the same `.env` you'll run Dewey with:
   ```bash
   python app/setup_postgres.py
   ```
   This creates the tables and indexes, splits each article into ~380-word chunks with ~70-word overlap (an approximation of the Azure path's 512-token/96-token split), embeds them, and stores them. Re-running it updates articles in place.
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


## Contributing

All contributions are welcome! More details to come. In the meantime, please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

# Glossary

**In short:** what each word in the Dewey guides means, what it means in Dewey, and where to read more.

**Who this is for:** anyone who meets an unfamiliar word in the guides; no technical background needed. Commands named here run from the [Dewey folder](#dewey-folder), with its [virtual environment](#virtual-environment) turned on.

> [!NOTE]
> Facts about other products were checked in November 2026 against the linked pages. If a page now says otherwise, trust the page.

**On this page:**

- [Dewey's own words](#deweys-own-words)
- [Your archive](#your-archive)
- [AI and search](#ai-and-search)
- [Your computer](#your-computer)
- [Servers, databases and security](#servers-databases-and-security)

<details><summary>Every term, A to Z</summary>

- **A:** [Agent harness](#agent-harness) · [Anthropic API](#anthropic-api) · [API key](#api-key) · [`ask_archive`](#ask_archive) · [Azure AI Search](#azure-ai-search) · [Azure OpenAI](#azure-openai)
- **B:** [bash](#shell) (see Shell) · [Bearer token](#bearer-token) · [Byline filter](#byline-filter)
- **C:** [Chat model](#chat-model) · [Chunk](#chunk) · [Citation](#citation) · [Claude](#anthropic-api) (see Anthropic API) · [CMS](#cms) · [Context window](#context-window) · [CSV](#csv)
- **D:** [Demo page](#demo-page) · [Dewey folder](#dewey-folder) · [`DEWEY_MCP_TOKEN`](#bearer-token) (see Bearer token) · [`DEWEY_STACK`](#stack) (see Stack) · [DNS rebinding](#dns-rebinding) · [Docker](#docker) · [Docker Compose](#docker-compose) · [Docker Desktop](#docker) (see Docker) · [Dry run](#dry-run)
- **E:** [Embedding](#embedding) · [`.env` file](#env-file) · [Environment variable](#environment-variable) · [Excerpt](#excerpt)
- **F:** [Fork](#fork)
- **G:** [Git](#git) · [GPU](#gpu)
- **H:** [Homebrew](#homebrew) · [Hugging Face](#hugging-face) · [Hybrid search](#hybrid-search)
- **I:** [Importer](#importer) · [Indexing](#indexing) · [ISO 8601 date](#iso-8601-date)
- **J:** [JSON](#json)
- **L:** [LLM](#chat-model) (see Chat model) · [localhost](#localhost) · [loopback](#localhost) (see localhost)
- **M:** [MCP](#mcp) · [MCP client](#mcp-client) · [MCP server](#mcp-server) · [MongoDB Atlas](#voyage-ai) (see Voyage AI)
- **N:** [`NEWSROOM_NAME`](#publisher) (see Publisher)
- **O:** [Ollama](#ollama) · [Open source](#open-source) · [Open-weight model](#open-weight-model) · [OpenAI-compatible API](#openai-compatible-api) · [OpenRouter](#openrouter)
- **P:** [Path](#path) · [pgvector](#pgvector) · [pip](#pip) · [Port](#port) · [Postgres](#postgres) · [PowerShell](#powershell) · [Prompt injection](#prompt-injection) · [Publisher](#publisher) · [Python](#python) · [PyTorch](#pytorch)
- **R:** [Repository](#repository) · [Reranking](#reranking) · [Reverse proxy](#reverse-proxy) · [RRF](#hybrid-search) (see Hybrid search) · [RSS and Atom feeds](#rss-and-atom-feeds)
- **S:** [`search_archive`](#search_archive) · [Search models](#search-models) · [Search planning](#search-planning) · [Setup](#setup) · [Setup wizard](#setup-wizard) · [Shell](#shell) · [Stack](#stack) · [stdio](#stdio) · [Streamable HTTP](#streamable-http) · [Structured outputs](#structured-outputs)
- **T:** [Terminal](#terminal) · [TLS and HTTPS](#tls-and-https) · [Token (billing)](#token-billing) · [Trial run](#trial-run)
- **V:** [Vector](#vector) · [venv](#virtual-environment) (see Virtual environment) · [Virtual environment](#virtual-environment) · [vLLM](#vllm) · [Voyage AI](#voyage-ai)
- **W:** [WSL](#wsl)
- **Z:** [ZDR](#zero-data-retention) (see Zero Data Retention) · [Zero Data Retention](#zero-data-retention) · [zsh](#shell) (see Shell)

</details>

## Dewey's own words

### `ask_archive`

A tool Dewey's [MCP server](#mcp-server) offers: Dewey's own [chat model](#chat-model) writes an answer with [citations](#citation).

**In Dewey:** slower than [`search_archive`](#search_archive). On paid [setups](#setup) each call costs money, and Dewey sets no budget or rate limit.

**More:** [MCP.md: The two tools](MCP.md#the-two-tools)

### Citation

A numbered link to the article a statement came from. Models sometimes cite the wrong article or none, so check a few.

**In Dewey:** the model writes `[SRC2]`; Dewey shows `[2]`, linking the second source.

**More:** [INSTALL.md: Try the demo page](INSTALL.md#try-the-demo-page)

### Demo page

A chat page for trying Dewey: `python app/main.py` starts it, usually at http://127.0.0.1:7860. Each question costs about as much as an [`ask_archive`](#ask_archive) call.

**In Dewey:** it has no login, so it only answers on this computer.

**More:** [INSTALL.md: Try the demo page](INSTALL.md#try-the-demo-page) · security: [DEPLOYMENT.md: The demo page](DEPLOYMENT.md#the-demo-page)

### Dewey folder

The folder `git clone` creates, with Dewey's code, your [`.env` file](#env-file) and articles. The guides assume `/Users/you/dewey-ai` (Mac), `/home/you/dewey-ai` (Linux) or `C:\Users\you\dewey-ai` (Windows), with your user name for `you`.

**In Dewey:** run Dewey's commands from this folder.

**More:** [INSTALL.md: Get the Dewey code](INSTALL.md#get-the-dewey-code)

### Dry run

A practice run that shows what would happen but changes nothing.

**In Dewey:** `python install.py --dry-run` runs the [setup wizard](#setup-wizard) but only prints the `.env` (secrets masked) and import commands. An Ollama download you accept still happens.

**More:** [INSTALL.md: Run the setup wizard](INSTALL.md#run-the-setup-wizard)

### Excerpt

A passage from an article, which can miss context the full article gives.

**In Dewey:** each excerpt is one [chunk](#chunk) of an article.

**More:** [MCP.md: The two tools](MCP.md#the-two-tools)

### Importer

A program that copies articles out of a website, feed or file.

**In Dewey:** there are six, for WordPress (site or export), [feeds](#rss-and-atom-feeds), [CSV](#csv), Drupal and Ghost (both beta). None invents a missing byline or date.

**More:** [IMPORTING.md: Choose an importer](IMPORTING.md#choose-an-importer)

### Indexing

Preparing articles so a search can find them fast.

**In Dewey:** `python app/setup_postgres.py` stores an [embedding](#embedding) of each [chunk](#chunk). Each run re-embeds all of `data/`, which costs money with Voyage AI, and never removes articles.

**More:** [IMPORTING.md: Index your articles](IMPORTING.md#index-your-articles) · Azure: [AZURE.md: Build the search index](AZURE.md#build-the-search-index)

### `search_archive`

The recommended tool Dewey's MCP server offers: up to 10 matching excerpts, filterable by date and [byline](#byline-filter).

**In Dewey:** it calls no chat model, but the question still goes to your [search models](#search-models), which may be hosted.

**More:** [MCP.md: The two tools](MCP.md#the-two-tools)

### Search models

The models that find articles: [embedding](#embedding) searches by meaning, and [reranking](#reranking) orders the matches.

**In Dewey:** outside Azure, each runs at [Voyage AI](#voyage-ai) or on your computer.

**More:** [STACKS.md: Mixing hosted and local search models](STACKS.md#mixing-hosted-and-local-search-models)

### Setup

One of the five ways to run Dewey: a [stack](#stack) plus a choice of search models and chat model server.

- [Azure](STACKS.md#azure): `azure`
- [Claude with Voyage AI](STACKS.md#claude-with-voyage-ai): `anthropic`
- [Claude with local search models](STACKS.md#claude-with-local-search-models): `anthropic`
- [Fully open on your own computer](STACKS.md#fully-open-on-your-own-computer): `open`
- [Open-weight models through OpenRouter](STACKS.md#open-weight-models-through-openrouter): `open`

**In Dewey:** the [setup wizard](#setup-wizard) asks which one. Only the embedding choice is costly to change later.

**More:** [STACKS.md: The setups at a glance](STACKS.md#the-setups-at-a-glance)

### Setup wizard

A program that asks questions and sets things up from your answers. Re-run it any time: your answers come back as defaults.

**In Dewey:** `python install.py` writes `.env`, and can install packages, check keys, and import (not on Azure) and index your archive.

**More:** [INSTALL.md: Run the setup wizard](INSTALL.md#run-the-setup-wizard)

### Stack

The `DEWEY_STACK` setting in `.env`, which picks who runs the chat model and search:

- `azure`: Microsoft's [Azure OpenAI](#azure-openai) and [Azure AI Search](#azure-ai-search)
- `anthropic`: Claude ([Anthropic API](#anthropic-api)), searching [Postgres](#postgres) with [pgvector](#pgvector)
- `open`: an [open-weight model](#open-weight-model) on [Ollama](#ollama), [vLLM](#vllm) or [OpenRouter](#openrouter), searching Postgres

`open` means open-weight, not OpenAI: the `open` setups don't use OpenAI's own service.

**In Dewey:** if `DEWEY_STACK` is missing, Dewey runs `azure`. The setup wizard always writes it; on a first run, `anthropic` is selected only as a starting point.

**More:** [STACKS.md: The setups at a glance](STACKS.md#the-setups-at-a-glance)

### Trial run

A small first import: check a few articles' headline, text, byline, date and link before importing everything.

**In Dewey:** `--limit 20` stops after 20 articles; from a WordPress site, Drupal or Ghost, those are the oldest. Unless you also add `--prefix`, they go in separate `-sample` files, so they never replace a full import.

**More:** [IMPORTING.md: Start with a trial run](IMPORTING.md#start-with-a-trial-run)

## Your archive

### Byline filter

A search limit to articles by the writers you name; articles without a byline never match.

**In Dewey:** the whole name must match ("Doe" alone finds nothing). Capitals and extra spaces count only on `azure`.

**More:** [MCP.md: The two tools](MCP.md#the-two-tools)

### CMS

Content management system: the software a newsroom publishes with, such as WordPress, Drupal or Ghost.

**In Dewey:** those three have [importers](#importer); for Arc XP, Brightspot and others, try an RSS feed or CSV export.

**More:** [IMPORTING.md: Choose an importer](IMPORTING.md#choose-an-importer)

### CSV

Comma-separated values: a plain-text table, one row per line, column names first.

**In Dewey:** the `csv` importer reads one article per row, and stops at dates like 05/01/2024 until you add `--date-format`.

**More:** [IMPORTING.md: Choose an importer](IMPORTING.md#choose-an-importer)

### ISO 8601 date

Dates written biggest unit first: `2025-09-09` is 9 September 2025, and `2025-09-09T12:00:00Z` adds a time, `Z` meaning UTC.

**In Dewey:** article files store `publish_date` this way; date filters use YYYY-MM-DD.

**More:** [IMPORTING.md: Article file format](IMPORTING.md#article-file-format) · [ISO 8601 on Wikipedia](https://en.wikipedia.org/wiki/ISO_8601)

### JSON

A plain-text data format: named values in `{ }`, lists in `[ ]`, no comments and no comma after the last item.

**In Dewey:** each file in `data/` holds one article or a list of them.

**More:** [IMPORTING.md: Article file format](IMPORTING.md#article-file-format)

### Publisher

The organization that produced an article: your newsroom or another outlet.

**In Dewey:** an optional `publisher` field, not stored on Azure. Importers fill it only when you pass `--publisher`, so use that for other outlets' stories; the rest get `NEWSROOM_NAME` when you index.

> [!IMPORTANT]
> `NEWSROOM_NAME` is required: without it the indexer, the MCP server and the demo page stop with an error. Earlier versions credited those articles to "The Philadelphia Inquirer", the newsroom Dewey was first built for. If you indexed that way, set your newsroom's name in the setup wizard, then index again.

**More:** [IMPORTING.md: Index your articles](IMPORTING.md#index-your-articles)

### RSS and Atom feeds

Formats a website uses to list its latest stories, often at `/feed/` (WordPress), `/rss.xml` (Drupal) or `/rss/` (Ghost).

**In Dewey:** the `feed` importer reads them. Feeds often hold only recent stories or excerpts; `--require-full-text` skips excerpt-only stories.

**More:** [IMPORTING.md: Choose an importer](IMPORTING.md#choose-an-importer)

## AI and search

### Agent harness

A program in which an AI model works through a task, calling tools: Hermes Agent, DeepSeek Harness or Claude Code.

**In Dewey:** a harness can call [`search_archive`](#search_archive) partway through a task.

**More:** [MCP.md: Client recipes](MCP.md#client-recipes)

### Anthropic API

Anthropic's paid service for using Claude from programs, billed per [token](#token-billing) ([Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing), checked November 2026), with `sk-ant-` keys from the [Claude Console](https://platform.claude.com).

**In Dewey:** on the `anthropic` stack, [`ask_archive`](#ask_archive) and the demo page call Claude (`claude-opus-5-5` by default).

**More:** [STACKS.md: Claude (Anthropic API)](STACKS.md#claude-anthropic-api)

### Azure AI Search

Microsoft's hosted search service, now called Foundry IQ (Azure AI Search) on Microsoft's pages ([Microsoft](https://learn.microsoft.com/en-us/azure/search/search-sku-tier), checked November 2026).

**In Dewey:** the Azure setup needs its Basic tier or higher, which costs money every month.

**More:** [AZURE.md: What you need in Azure](AZURE.md#what-you-need-in-azure)

### Azure OpenAI

Microsoft's service running OpenAI's models in Azure; it needs an Azure subscription, not an OpenAI account. Now called Azure OpenAI in Microsoft Foundry Models ([Microsoft](https://learn.microsoft.com/en-us/azure/foundry-classic/openai/how-to/create-resource), checked November 2026).

**In Dewey:** the Azure setup's chat and embedding models run on it.

**More:** [AZURE.md: What you need in Azure](AZURE.md#what-you-need-in-azure)

### Chat model

The AI model that reads and writes text, also called a language model or LLM (large language model).

**In Dewey:** used by [`ask_archive`](#ask_archive) and the [demo page](#demo-page), but the MCP server needs its settings even for `search_archive`.

**More:** [MODELS.md: What Dewey needs from the model](MODELS.md#what-dewey-needs-from-the-model)

### Chunk

A piece of an article: splitting keeps each search result a short passage.

**In Dewey:** up to 380 words, overlapping the next by 70. On Azure, 512 tokens, overlapping by 96.

**More:** [IMPORTING.md: Index your articles](IMPORTING.md#index-your-articles)

### Context window

How much text, in [tokens](#token-billing), a chat model can take in at once, its answer included.

**In Dewey:** an answer prompt is about 7,100 tokens, but Ollama's default on most computers is 4,096, and it silently drops the middle. Set Ollama's context length to 32,768, in the Ollama app's settings or with `OLLAMA_CONTEXT_LENGTH`.

**More:** [MODELS.md: A. Self-hosted with Ollama](MODELS.md#a-self-hosted-with-ollama)

<details><summary>Sources</summary>

- Ollama's default depends on the GPU memory it finds: [docs/context-length.mdx](https://github.com/ollama/ollama/blob/main/docs/context-length.mdx) and [server/routes.go at v0.40.16](https://github.com/ollama/ollama/blob/v0.40.16/server/routes.go#L2570-L2586).
- What Ollama does with a prompt that doesn't fit: [llm/llama_server.go at v0.40.16](https://github.com/ollama/ollama/blob/v0.40.16/llm/llama_server.go#L309-L358).
- Dewey's prompt size was counted with Qwen3's tokenizer, using ten full-length excerpts.

All checked November 2026.

</details>

### Embedding

Numbers standing for a text's meaning, so search finds articles worded differently from your question.

**In Dewey:** a new embedding model needs a fresh database.

**More:** [STACKS.md: Indexes are tied to one embedding model](STACKS.md#indexes-are-tied-to-one-embedding-model)

### GPU

Graphics processing unit: a chip that runs AI models much faster than the main processor.

**In Dewey:** answer Y to the wizard's "Does this machine lack an NVIDIA GPU?" on a Mac or any computer without an NVIDIA card.

**More:** [INSTALL.md: What the wizard asks](INSTALL.md#what-the-wizard-asks)

### Hugging Face

A website where people publish open-weight AI models to download ([huggingface.co](https://huggingface.co)).

**In Dewey:** the local search models download from it once, about 3 GB; after that, Dewey doesn't contact it.

**More:** [STACKS.md: Local search models](STACKS.md#local-search-models)

### Hybrid search

A keyword search plus a meaning ([embedding](#embedding)) search, merged, so it finds exact names and related ideas.

**In Dewey:** outside Azure, Postgres merges them by reciprocal rank fusion (RRF), favoring chunks ranked high in either.

**More:** [STACKS.md: What every setup has in common](STACKS.md#what-every-setup-has-in-common)

### Ollama

A free, open-source app that runs open-weight chat models on your computer ([ollama/ollama](https://github.com/ollama/ollama)).

**In Dewey:** the fully open setup's model server, at `http://localhost:11434/v1`. Set its [context window](#context-window) to 32,768 tokens, or answers can silently miss your question.

**More:** [MODELS.md: A. Self-hosted with Ollama](MODELS.md#a-self-hosted-with-ollama)

### Open source

Code anyone may read, change and share, under a license such as MIT or Apache 2.0. Some [open-weight](#open-weight-model) models aren't: their licenses restrict use.

**In Dewey:** Dewey is open source (MIT); whether your whole setup is depends on your models.

**More:** [MODELS.md: Hermes model licenses](MODELS.md#hermes-model-licenses)

### Open-weight model

An AI model you may download, run and modify under its license, such as OpenAI's `gpt-oss-20b`, on your computer or a host you choose. Open-weight doesn't mean the training data is published.

**In Dewey:** the `open` stack's chat model is one.

**More:** [STACKS.md: Running a chat model yourself](STACKS.md#running-a-chat-model-yourself)

### OpenAI-compatible API

A request format copied from OpenAI's API, so one program works with many model servers; OpenAI needn't be involved.

**In Dewey:** on the `open` stack, `OPEN_LLM_BASE_URL` is such a server's address, ending in `/v1`.

**More:** [MODELS.md: What Dewey needs from the model](MODELS.md#what-dewey-needs-from-the-model)

### OpenRouter

A pay-per-use service that sends each request to one of many model providers, through an OpenAI-compatible API ([OpenRouter docs](https://github.com/OpenRouterTeam/docs/blob/main/guides/routing/provider-selection.mdx), checked November 2026).

**In Dewey:** `ask_archive` and the demo page send it your questions and excerpts; by default, only to [ZDR](#zero-data-retention) providers that don't collect data.

**More:** [MODELS.md: C. Hosted through OpenRouter](MODELS.md#c-hosted-through-openrouter)

### Prompt injection

Text written to trick an AI model into following new instructions, hidden in anything it reads, including your archive.

**In Dewey:** Dewey asks models to treat articles as quoted material, which is no guarantee. Be careful with agents that can also email, edit files or run commands.

**More:** [MCP.md: Security](MCP.md#security)

### PyTorch

A free library that runs AI models from Python.

**In Dewey:** the local search models need 2.9 or later, which Intel Macs can't install: their last build was 2.2.2 ([PyPI](https://pypi.org/project/torch/), checked November 2026).

**More:** [STACKS.md: Local search models](STACKS.md#local-search-models)

### Reranking

A second pass over search results: a model reads the question beside each passage and reorders them.

**In Dewey:** Dewey reranks up to 50 candidates and keeps 10. `RERANK_PROVIDER=none` turns it off.

**More:** [STACKS.md: Mixing hosted and local search models](STACKS.md#mixing-hosted-and-local-search-models)

### Search planning

An answer's first step: a chat model turns the question into a search topic, dates and author names.

**In Dewey:** used by `ask_archive` and the demo page, which shows the plan under "Generating metadata" so you can check its dates and bylines.

**More:** [MODELS.md: What Dewey needs from the model](MODELS.md#what-dewey-needs-from-the-model)

### Structured outputs

A feature that makes a model's reply follow a fixed format, such as [JSON](#json) with named fields.

**In Dewey:** search planning asks for it. If an `open` server lacks it, Dewey asks for plain JSON and checks it.

**More:** [MODELS.md: What Dewey needs from the model](MODELS.md#what-dewey-needs-from-the-model)

### Token (billing)

The unit AI services count text in, billed per million sent and written. Not the MCP token: see [bearer token](#bearer-token).

**In Dewey:** Claude Opus 5.5 counts about 1.8 tokens per word ([Anthropic](https://platform.claude.com/docs/en/models/opus-5-5/overview), checked November 2026). An `ask_archive` call sends about 9,000 tokens (an estimate).

**More:** [STACKS.md: Claude (Anthropic API)](STACKS.md#claude-anthropic-api)

### Vector

A list of numbers, such as an [embedding](#embedding); vector search finds the stored ones closest to the question's.

**In Dewey:** [pgvector](#pgvector) stores them in Postgres.

**More:** [STACKS.md: Indexes are tied to one embedding model](STACKS.md#indexes-are-tied-to-one-embedding-model)

### vLLM

Free, open-source software that serves open-weight models through an OpenAI-compatible API, on a server with a suitable GPU ([vllm-project/vllm](https://github.com/vllm-project/vllm)).

**In Dewey:** an alternative to Ollama, by default on port 8000.

**More:** [MODELS.md: B. Self-hosted with vLLM](MODELS.md#b-self-hosted-with-vllm)

### Voyage AI

A MongoDB company offering hosted search models, with keys from MongoDB Atlas. MongoDB may use what you send to improve its models unless you opt out.

**In Dewey:** add a payment method in Atlas, or you can't opt out. The free trial's 3 requests a minute also stop Dewey's indexing (our reading of the limits; not tested).

**More:** [STACKS.md: Voyage AI search models](STACKS.md#voyage-ai-search-models) · [STACKS.md: What the hosted providers receive](STACKS.md#what-the-hosted-providers-receive)

<details><summary>Sources</summary>

MongoDB's [Voyage AI quickstart](https://www.mongodb.com/docs/voyageai/quickstart/), [model training data](https://www.mongodb.com/docs/voyageai/management/model-training-data/) and [API overview, with the free trial's rate limits](https://www.mongodb.com/docs/voyageai/api-reference/overview/) pages, checked November 2026.

</details>

### Zero Data Retention

A provider's promise, ZDR for short, not to keep your requests or its replies. It's a policy, not a technical control: the provider still sees them.

**In Dewey:** Dewey asks for it on OpenRouter by default. Some ZDR endpoints may still keep prompts for abuse checks unless your account is set to "ZDR only" ([OpenRouter](https://github.com/OpenRouterTeam/docs/blob/main/guides/features/zdr.mdx), checked November 2026).

**More:** [MODELS.md: What the privacy settings do and don't do](MODELS.md#what-the-privacy-settings-do-and-dont-do)

## Your computer

### `.env` file

A plain-text settings file, one `NAME='value'` per line, which Mac and Linux file browsers hide because of its leading dot.

**In Dewey:** it holds your keys and passwords. To change it, re-run the setup wizard.

**More:** [INSTALL.md: Change your settings](INSTALL.md#change-your-settings)

### Environment variable

A named setting, such as `DATABASE_URL`, that a program reads from the [shell](#shell) that started it; `export` sets one until the window closes.

**In Dewey:** one already set beats `.env`. Importer passwords go in one, never on the command line.

**More:** [IMPORTING.md: Passwords and keys](IMPORTING.md#passwords-and-keys)

### Fork

Your own copy of someone else's GitHub project, which you can change freely. To only run Dewey, cloning is enough.

**In Dewey:** this version forks The Philadelphia Inquirer's original to add setups without Azure.

**More:** [README: Contributing](../README.md#contributing)

### Git

A program that downloads a project's code and tracks changes: `git clone` makes your copy, and `git pull` fetches updates.

**In Dewey:** updates leave `.env` and your article files alone, because git ignores them.

**More:** INSTALL.md for your [Mac](INSTALL.md#get-your-mac-ready), [Windows PC](INSTALL.md#get-your-windows-pc-ready) or [Linux computer](INSTALL.md#get-your-linux-computer-ready)

### Homebrew

A free tool for installing Mac programs by typing commands, such as `brew install git` ([Homebrew](https://github.com/Homebrew/brew/blob/main/docs/Installation.md), checked November 2026).

**In Dewey:** optional. Use `brew install python@3.13`: plain `brew install python` gives 3.15, which can't install Dewey.

**More:** [INSTALL.md: Get your Mac ready](INSTALL.md#get-your-mac-ready)

### Path

The address of a file or folder, such as `/Users/you/dewey-ai/app/main.py`; `~` means your home folder. `PATH` lists the folders where the shell looks for commands.

**In Dewey:** AI tools that start Dewey need full paths, such as `/Users/you/dewey-ai/.venv/bin/python`. In Windows JSON files, write each `\` as `\\`.

**More:** [INSTALL.md: Claude Desktop](INSTALL.md#claude-desktop)

### pip

Python's package installer. Some Pythons, such as Homebrew's, refuse to install outside a [virtual environment](#virtual-environment) (`externally-managed-environment`).

**In Dewey:** if pip says "A new release of pip is available" and yours is below 26.2.2, update it. Older ones still trust certificates compromised in October 2026 ([Python Software Foundation](https://discuss.python.org/t/information-for-python-users-regarding-emudhra-emsign-ca-certificate-security-incident/109576), checked November 2026).

**More:** [INSTALL.md: Create a virtual environment](INSTALL.md#create-a-virtual-environment)

### PowerShell

Windows's command-line shell; its prompt starts with `PS`. Windows includes Windows PowerShell 5.1, and PowerShell 7 is a separate install ([Microsoft](https://github.com/MicrosoftDocs/PowerShell-Docs/blob/main/reference/docs-conceptual/install/install-powershell-on-windows.md), checked November 2026).

**In Dewey:** the guides' Windows steps use it, not Command Prompt or Git Bash. Windows hasn't been tested with Dewey.

**More:** [INSTALL.md: Get your Windows PC ready](INSTALL.md#get-your-windows-pc-ready)

### Python

The programming language Dewey is written in.

**In Dewey:** 3.11, 3.12 and 3.13 work; 3.14 is untested, and 3.15 can't install Dewey. A Mac's `python3` may be Apple's 3.9, too old, so the guides type `python3.13`.

**More:** INSTALL.md for your [Mac](INSTALL.md#get-your-mac-ready), [Windows PC](INSTALL.md#get-your-windows-pc-ready) or [Linux computer](INSTALL.md#get-your-linux-computer-ready)

### Repository

A project's code and history, kept by git; on GitHub, each has a page and an address you can clone.

**In Dewey:** cloning Dewey's repository gives you the [Dewey folder](#dewey-folder).

**More:** [INSTALL.md: Update Dewey](INSTALL.md#update-dewey)

### Shell

The program in a terminal window that runs your commands: zsh on a Mac ([Apple](https://support.apple.com/en-us/102360), checked November 2026), usually bash on Linux, [PowerShell](#powershell) on Windows.

**In Dewey:** paste one command box at a time, exactly as written: zsh misreads `#` notes and unquoted `[ ] ? !` ([zsh manual](https://github.com/zsh-users/zsh/blob/zsh-5.9/Doc/Zsh/options.yo)).

**More:** [INSTALL.md: How to use this guide](INSTALL.md#how-to-use-this-guide)

### Terminal

The app where you type commands; Control+C (Ctrl+C) stops a running one.

| Computer | To open it | To paste |
|---|---|---|
| Mac | Command+Space, type Terminal, press Enter | Command+V |
| Windows | Start, type PowerShell, press Enter | Ctrl+V |
| Linux | Super key, type Terminal, press Enter | Ctrl+Shift+V |

**In Dewey:** in each new window, go to the [Dewey folder](#dewey-folder) and turn on the [virtual environment](#virtual-environment).

**More:** [INSTALL.md: How to use this guide](INSTALL.md#how-to-use-this-guide)

<details><summary>Sources</summary>

- Mac: Apple's [Open or quit Terminal on Mac](https://support.apple.com/guide/terminal/open-or-quit-terminal-apd5265185d-f365-44cb-8b09-71a064a42125/mac) and [Mac keyboard shortcuts](https://support.apple.com/en-us/102650).
- Windows: Microsoft's [PowerShell 101: getting started](https://github.com/MicrosoftDocs/PowerShell-Docs/blob/main/reference/docs-conceptual/learn/ps101/01-getting-started.md) and Windows Terminal's [default key bindings](https://github.com/microsoft/terminal/blob/main/src/cascadia/TerminalSettingsModel/userDefaults.json).
- Linux: GNOME's help on [opening apps](https://gitlab.gnome.org/GNOME/gnome-user-docs/-/raw/master/gnome-help/C/shell-apps-open.page) and on [copy and paste in GNOME Terminal](https://gitlab.gnome.org/GNOME/gnome-terminal/-/raw/master/help/C/txt-copy-paste.page).

All checked November 2026.

</details>

### Virtual environment

A private folder of Python packages for one project, so they don't clash with other programs.

**In Dewey:** the `.venv` folder. `source .venv/bin/activate` (PowerShell: `.venv\Scripts\Activate.ps1`) turns it on: the prompt then starts with `(.venv)`, and `python` runs the right Python.

**More:** [INSTALL.md: Turn it on in every new terminal window](INSTALL.md#turn-it-on-in-every-new-terminal-window)

### WSL

Windows Subsystem for Linux: a part of Windows that runs a real Linux system, such as Ubuntu ([Microsoft](https://github.com/MicrosoftDocs/WSL/blob/main/WSL/install.md), checked November 2026).

**In Dewey:** the guides' second Windows route, following the Linux steps; on Windows on Arm, it might be the only one that works. Both routes are *not tested by us*.

**More:** [INSTALL.md: Get your Windows PC ready](INSTALL.md#get-your-windows-pc-ready)

## Servers, databases and security

### API key

A secret string that lets a program use a paid service on your account: anyone who has it can run up your bill.

**In Dewey:** use keys made only for Dewey, so revoking them breaks nothing else.

**More:** [INSTALL.md: What the wizard asks](INSTALL.md#what-the-wizard-asks)

### Bearer token

A secret sent with each request to prove the sender may connect (`Authorization: Bearer YOUR_TOKEN`). Not a billing [token](#token-billing).

**In Dewey:** `DEWEY_MCP_TOKEN` guards the MCP server's HTTP mode. Anyone who has it can search the whole archive.

**More:** [MCP.md: Before you start: HTTP](MCP.md#before-you-start-http) · rotating it: [MCP.md: Security](MCP.md#security)

### Docker

A tool that runs programs in containers, sealed boxes made from an image (the packaged program). Docker Desktop (Mac and Windows) is free only for personal use, education, non-commercial open source and businesses under 250 employees and $10 million revenue. Docker's summary doesn't mention nonprofits. Colima (Mac) and Docker Engine (Linux) are free.

**In Dewey:** optional. Use Docker Engine 28.0 or newer: older versions can expose ports published on 127.0.0.1 to your local network.

**More:** INSTALL.md for your [Mac](INSTALL.md#get-your-mac-ready), [Windows PC](INSTALL.md#get-your-windows-pc-ready) or [Linux computer](INSTALL.md#get-your-linux-computer-ready) · [DEPLOYMENT.md: Why Docker Engine 28.0 or newer](DEPLOYMENT.md#why-docker-engine-280-or-newer)

<details><summary>Sources</summary>

- Docker Desktop's license terms: Docker's [Mac install page](https://github.com/docker/docs/blob/main/content/manuals/desktop/setup/install/mac-install.md).
- Ports published on 127.0.0.1 before Docker Engine 28.0: Docker's [port publishing guide](https://github.com/docker/docs/blob/main/content/manuals/engine/network/port-publishing.md).
- Colima: [abiosoft/colima](https://github.com/abiosoft/colima) (MIT license).

All checked November 2026.

</details>

### Docker Compose

A Docker tool that starts several containers from `docker-compose.yml`; profiles start only when named, as in `docker compose --profile demo up -d`.

**In Dewey:** it runs the MCP server and Postgres, with `demo` and `ollama` profiles. Its database isn't the setup wizard's `dewey-db`.

**More:** [DEPLOYMENT.md: What the Compose file runs](DEPLOYMENT.md#what-the-compose-file-runs)

### DNS rebinding

A trick by which a web page you visit queries programs on your computer, such as Dewey (on your API keys), and reads the answers.

**In Dewey:** the demo page and the MCP server on 127.0.0.1 refuse such requests. In Docker the MCP server listens on 0.0.0.0, where the token is the only guard.

**More:** [MCP.md: Host names and DNS-rebinding protection](MCP.md#host-names-and-dns-rebinding-protection)

### localhost

Your computer's name for itself, at the loopback address 127.0.0.1, which only programs on the same computer can reach. In a container, it means the container.

**In Dewey:** the demo page and the MCP server's HTTP mode listen on 127.0.0.1 by default.

**More:** [DEPLOYMENT.md: Access control](DEPLOYMENT.md#access-control)

### MCP

Model Context Protocol: an open standard that lets AI tools use outside tools, such as an archive search ([modelcontextprotocol.io](https://modelcontextprotocol.io)).

**In Dewey:** the main way to use Dewey, through its [MCP server](#mcp-server).

**More:** [MCP.md: Two ways to connect](MCP.md#two-ways-to-connect)

### MCP client

An AI tool that uses MCP servers' tools, such as Claude Code, Claude Desktop, Open WebUI or LibreChat.

**In Dewey:** your AI tool decides which of its users get Dewey's tools, because Dewey can't tell users apart.

**More:** [MCP.md: Client recipes](MCP.md#client-recipes)

### MCP server

A program that offers tools to AI tools over MCP and does the work, such as a search.

**In Dewey:** `app/mcp_server.py`, which never changes your index. Your AI tool starts it over [stdio](#stdio), or people share one over [Streamable HTTP](#streamable-http).

**More:** [MCP.md: Two ways to connect](MCP.md#two-ways-to-connect)

### pgvector

An extension that adds [vector](#vector) search to Postgres ([pgvector/pgvector](https://github.com/pgvector/pgvector)).

**In Dewey:** every setup except Azure needs 0.8 or newer. Older versions can return fewer articles when you filter by date or byline; Dewey warns when it finds one ([how to upgrade](STACKS.md#upgrade-pgvector)).

**More:** [STACKS.md: Postgres with pgvector](STACKS.md#postgres-with-pgvector)

### Port

A numbered door one program listens at, written after a colon: `127.0.0.1:7860` is port 7860.

**In Dewey:** demo page 7860 (or the next free one), MCP server 8765, Postgres 5432, Ollama 11434. Errors like "port is already allocated" or "address already in use" mean another program has it.

**More:** [INSTALL.md: Troubleshooting](INSTALL.md#troubleshooting)

### Postgres

PostgreSQL: a free, open-source database, also offered as a managed service. The guides use PostgreSQL 18.

**In Dewey:** outside Azure it holds your articles and embeddings, with [pgvector](#pgvector). Keep it off the internet: Dewey's Compose file never publishes it.

**More:** [STACKS.md: Postgres with pgvector](STACKS.md#postgres-with-pgvector)

### Reverse proxy

A server in front of another that passes requests through, often adding HTTPS, such as Caddy or nginx.

**In Dewey:** to reach the MCP server from other machines, put one with [TLS](#tls-and-https) in front. Never publish port 8765 directly.

**More:** [DEPLOYMENT.md: Steps for one server](DEPLOYMENT.md#steps-for-one-server)

### stdio

Standard input and output: how one program can start another and talk to it without a network.

**In Dewey:** the default. Your AI tool starts the MCP server itself, on the same computer, and needs no token. Outside Azure, Postgres must still be running.

**More:** [MCP.md: Before you start: stdio](MCP.md#before-you-start-stdio) · after a restart: [INSTALL.md: Start Dewey again later](INSTALL.md#start-dewey-again-later)

### Streamable HTTP

MCP's way to connect over a network, through a web address.

**In Dewey:** `python app/mcp_server.py --http` serves it at `http://127.0.0.1:8765/mcp`. Clients must send the [bearer token](#bearer-token).

**More:** [MCP.md: Before you start: HTTP](MCP.md#before-you-start-http)

### TLS and HTTPS

TLS encrypts what travels between computers. HTTPS is web traffic over TLS, at `https://` addresses.

**In Dewey:** the MCP server speaks plain HTTP, so beyond one computer it needs TLS in front. Without it, the token and article text cross the network readable.

**More:** [MCP.md: Other machines: TLS in front](MCP.md#other-machines-tls-in-front)

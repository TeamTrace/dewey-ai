# Choosing a setup

**In short:** This page compares Dewey's five [setups](GLOSSARY.md#setup) and gives each one's settings, without recommending one. Only the [embedding](GLOSSARY.md#embedding) model is hard to change later: each database is tied to one.

**Who this is for:** anyone deciding how a newsroom will run Dewey, before running the [setup wizard](GLOSSARY.md#setup-wizard). The [README](../README.md#how-it-works) explains what Dewey does.

> [!NOTE]
> **The names aren't vendor names.** Each setup uses one of three [stacks](GLOSSARY.md#stack), the values of the `DEWEY_STACK` setting:
>
> - `anthropic` means Claude, from Anthropic.
> - `open` means [open-weight models](GLOSSARY.md#open-weight-model), in [Ollama](GLOSSARY.md#ollama) on your computer or at an [OpenAI-compatible](GLOSSARY.md#openai-compatible-api) service such as [OpenRouter](GLOSSARY.md#openrouter). It is **not** OpenAI: none of these setups uses OpenAI's own service.
> - `azure` means Microsoft's [Azure OpenAI](GLOSSARY.md#azure-openai) Service, with [Azure AI Search](GLOSSARY.md#azure-ai-search) and Blob Storage. It isn't OpenAI's own API either.

**On this page:**

- [The setups at a glance](#the-setups-at-a-glance)
- [What every setup has in common](#what-every-setup-has-in-common)
- The setups: [Azure](#azure), [Claude with Voyage AI](#claude-with-voyage-ai), [Claude with local search models](#claude-with-local-search-models), [Fully open](#fully-open-on-your-own-computer), [OpenRouter](#open-weight-models-through-openrouter)
- [What the hosted providers receive](#what-the-hosted-providers-receive)
- [The parts in detail](#the-parts-in-detail)
- [Mixing hosted and local search models](#mixing-hosted-and-local-search-models)
- [Changing your setup later](#changing-your-setup-later)
- [Configuring by hand](#configuring-by-hand)

## The setups at a glance

Each column is one setup, listed by stack, not by preference. The [chat model](GLOSSARY.md#chat-model) writes answers for [`ask_archive`](GLOSSARY.md#ask_archive) and the [demo page](GLOSSARY.md#demo-page); the [search models](GLOSSARY.md#search-models) find and order the articles. *Moving parts* must all be running for Dewey to index and answer. Prices, sizes, versions and test status on this page were checked in November 2026.

| | Azure | Claude with Voyage AI | Claude with local search models | Fully open on your own computer | Open-weight models through OpenRouter |
|---|---|---|---|---|---|
| **Settings** (stack; embeddings; [reranking](GLOSSARY.md#reranking)) | `azure` | `anthropic`; `voyage`; `voyage` | `anthropic`; `local`; `local` or `none` | `open`; `local`; `local` or `none` | `open`; `local`; `local` or `none` |
| **Chat model** | Your Azure OpenAI deployment, such as gpt-5 | Claude Opus 5.5 | Claude Opus 5.5 | qwen3:8b or gpt-oss:20b, in Ollama | A hosted open-weight model, such as `openai/gpt-oss-20b` |
| **Search models** | Azure OpenAI embeddings and Azure's semantic ranker | [Voyage AI](GLOSSARY.md#voyage-ai), hosted | voyage-4-nano and bge-reranker-v2-m3, on your computer | Same as Claude with local search models | Same as Claude with local search models |
| **Database** | Azure AI Search | [Postgres](GLOSSARY.md#postgres) with [pgvector](GLOSSARY.md#pgvector) | Postgres with pgvector | Postgres with pgvector | Postgres with pgvector |
| **Accounts** | Azure | Anthropic, and MongoDB Atlas for Voyage AI, with a payment method | Anthropic | None | OpenRouter |
| **What it costs** (per-question costs are estimates) | About $74 a month for search, plus about 2–4¢ per `ask_archive` question | About 4–12¢ per `ask_archive` question, plus Voyage AI once its free tokens run out | About 4–12¢ per `ask_archive` question | No fees | Under 1¢ per `ask_archive` question with gpt-oss-20b, plus 5.5% on credits |
| **Computer** | Any that runs Python 3.11–3.13 | Any that runs Python 3.11–3.13 | An Apple-silicon Mac, or a Windows or Linux PC, with about 3–6 GB of memory for the search models | For example, an Apple-silicon Mac with 32 GB of memory (estimate) | Same as Claude with local search models |
| **Downloads** | About 0.1 GB | About 0.1 GB, plus the database | About 3.3 GB, plus the database | About 8.7 GB with qwen3:8b, plus the database | About 3.3 GB, plus the database |
| **Moving parts** | 4: Dewey, Azure OpenAI, Azure AI Search, Azure Storage | 4: Dewey, Postgres, Anthropic, Voyage AI | 3: Dewey, Postgres, Anthropic | 3: Dewey, Postgres, Ollama | 3: Dewey, Postgres, OpenRouter |
| **Who receives questions and articles** | Microsoft | MongoDB (Voyage AI) and Anthropic | Anthropic, for `ask_archive` and the demo page | Nobody outside your computers | OpenRouter and the provider it picks, for `ask_archive` and the demo page |

In every setup, your AI tool's own model also receives the [excerpts](GLOSSARY.md#excerpt) that [`search_archive`](GLOSSARY.md#search_archive) returns. If Postgres runs in [Docker](GLOSSARY.md#docker) on your computer, Docker Desktop (or Colima) is one more moving part.

## What every setup has in common

All five share the [importers](GLOSSARY.md#importer) and article files ([IMPORTING.md](IMPORTING.md#how-importing-works)), the [MCP server](GLOSSARY.md#mcp-server) and its two tools ([MCP.md](MCP.md#the-two-tools)), the demo page and the setup wizard. `search_archive`, the tool Dewey suggests, calls no chat model: your AI tool's own model writes the answer. So the chat model only serves `ask_archive` and the demo page, though the MCP server needs its settings to start.

Dewey runs `azure` when `DEWEY_STACK` isn't set, which makes it the *default*. On a first run, the setup wizard has `anthropic` selected, marked "(current)", which isn't a recommendation. It always writes `DEWEY_STACK` to your [`.env` file](GLOSSARY.md#env-file).

<details>
<summary>How each stack handles each step</summary>

| Step | `azure` (default) | `anthropic` | `open` |
|---|---|---|---|
| [Search planning](GLOSSARY.md#search-planning): dates and authors from the question | Azure OpenAI, with a forced function call | Claude, with [structured outputs](GLOSSARY.md#structured-outputs) | The open-weight model, with [JSON](GLOSSARY.md#json) checked against a schema |
| Embeddings | Your Azure OpenAI embedding deployment: `text-embedding-3-small` in AZURE.md, not the template's `text-embedding-3-large` ([why](AZURE.md#known-problems)) | Voyage AI `voyage-4`, hosted (stack default) | `voyage-4-nano`, on your computer (stack default) |
| Search engine | Azure AI Search: [hybrid search](GLOSSARY.md#hybrid-search) plus a semantic ranker | Postgres with pgvector: keyword and [vector](GLOSSARY.md#vector) search, fused with RRF | Same as `anthropic` |
| [Reranking](GLOSSARY.md#reranking) | Azure's semantic ranker | Voyage AI `rerank-3`, hosted (stack default) | `BAAI/bge-reranker-v2-m3`, on your computer (stack default) |
| [Indexing](GLOSSARY.md#indexing) | `app/setup.py`, through Blob Storage and an Azure indexer | `app/setup_postgres.py`, on your computer | Same as `anthropic` |

A *stack default* can be switched: see [Mixing hosted and local search models](#mixing-hosted-and-local-search-models).

</details>

**Computers it has run on:** macOS, with a real install; Linux, only the automated tests (on Ubuntu); Windows, not tested, and Windows on Arm has [extra limits](INSTALL.md#get-your-windows-pc-ready). The automated tests use *stand-ins*: small fake servers on your own computer.

> [!WARNING]
> **On paid setups, every `ask_archive` call costs money, and Dewey has no budget or rate limit.** An AI tool can call it many times in one task. Watch your provider's usage page, or keep your AI tool to `search_archive` ([MCP.md](MCP.md#cost-and-privacy)).

## Azure

**In one line:** Dewey's original setup, built by The Philadelphia Inquirer, all in Microsoft Azure. Full guide: [AZURE.md](AZURE.md).

> [!NOTE]
> **This setup has known problems and hasn't been run since this fork began.** Expect to adjust details. Check the [issue list](https://github.com/TeamTrace/dewey-ai/issues), and report what you find.

**Accounts:** an Azure subscription with Azure OpenAI (chat and embedding deployments), Azure AI Search (Basic tier or higher) and Blob Storage. Two portal steps need the Owner role, or another role that can assign roles.

**What it costs** (list prices; [details](AZURE.md#what-you-need-in-azure)):

- **Azure AI Search, Basic tier:** $73.73 a month in East US and most US and European regions, up to $97.09 elsewhere, charged every hour even when idle.
- **Semantic ranker,** used on every search: after 1,000 free searches a month, Microsoft returns a billing error, so searches should fail (not tested) unless you switch to the Standard plan ($1 per 1,000).
- **gpt-5:** about 2–4¢ per `ask_archive` or demo-page question (estimate).
- **Embeddings:** about $0.25 per [index run](GLOSSARY.md#indexing) of 10,000 articles with text-embedding-3-small, as in AZURE.md, or $1.60 with text-embedding-3-large (estimates). Each run probably embeds every article again (*not verified*).
- **Blob Storage:** pennies.

**Computer:** any that runs [Python](GLOSSARY.md#python) 3.11–3.13, including Intel Macs.

**Downloads:** about 90 MB of Python packages, 400 MB on disk (measured once on an Apple M3 Max with 36 GB, November 2026; yours will differ).

**Who receives questions and articles:** Microsoft ([details](#what-the-hosted-providers-receive)).

**Limits** ([all of them](AZURE.md#limits-of-the-azure-stack)): [byline filters](GLOSSARY.md#byline-filter) match names exactly, capitals included, though Dewey's tool description says otherwise ([MCP.md](MCP.md#the-two-tools)). Articles with no byline aren't indexed; results have no [`publisher`](GLOSSARY.md#publisher); the wizard doesn't run the importers ([IMPORTING.md](IMPORTING.md#run-an-import)); and a second article with the same `id` replaces the first.

**Known problems,** from Microsoft's docs and Dewey's code ([details](AZURE.md#known-problems)):

- With text-embedding-3-large, the template's example, question embeddings have the wrong length (3,072 against the index's 1,536), so searches should fail (not tested). AZURE.md works around it with text-embedding-3-small.
- The search service needs a managed identity with the Cognitive Services OpenAI User role. Nothing in Dewey sets it up, and the Free tier (50 MB) can't have one.
- The example chat model, gpt-5 (version 2025-08-07), is scheduled to retire on February 9, 2027.
- Microsoft keeps Dewey's responses for 30 days, because Dewey doesn't send `store=false`.
- Dewey logs each search's bylines and dates to its error output. The Postgres setups don't.

**How it's been tested:** The Philadelphia Inquirer built Dewey on this setup, and the original project thanks three alpha testers in its newsroom. Since this fork began in November 2026, which changed how Dewey filters, cites and shows answers, nobody has run it against Azure. The only automated tests check the date and byline filter Dewey writes for Azure AI Search, and they never contact Azure.

**Set it up:** in the setup wizard, choose `azure` at "Which stack?" and answer its 11 Azure questions ([AZURE.md](AZURE.md#with-the-setup-wizard)). Or put these lines in `.env` (a file in [the Dewey folder](GLOSSARY.md#dewey-folder)) with a text editor, not a [terminal](GLOSSARY.md#terminal), changing any that are already there ([how](#configuring-by-hand)):

```dotenv
DEWEY_STACK='azure'
# Then the 11 Azure settings listed in AZURE.md, under "By hand".
```

Then build the search index with `python app/setup.py` ([AZURE.md](AZURE.md#build-the-search-index)).

<details>
<summary>Sources and arithmetic</summary>

Microsoft's own pages (its Azure docs are no longer public on GitHub), checked November 2026:

- Tiers, and why Free can't run Dewey's indexing: [pricing tiers](https://learn.microsoft.com/en-us/azure/search/search-sku-tier), [managed identities](https://learn.microsoft.com/en-us/azure/search/search-howto-managed-identities-data-sources), [the embedding skill](https://learn.microsoft.com/en-us/azure/search/cognitive-search-skill-azure-openai-embedding) and [limits](https://learn.microsoft.com/en-us/azure/search/search-limits-quotas-capacity).
- Semantic ranker plans: [enable or disable](https://learn.microsoft.com/en-us/azure/search/semantic-how-to-enable-disable) and the [pricing page](https://azure.microsoft.com/en-us/pricing/details/search/), which also gives a new account's $200 credit for 30 days.
- Prices: Microsoft's [Retail Prices API](https://prices.azure.com/api/retail/prices). The gpt-5 estimate is two calls with about 7,000 input and 1,000–3,000 output tokens in all, at $1.25 and $10 per million. The embedding estimate is 10,000 articles of about 800 words, with text-embedding-3-small at $0.02 and text-embedding-3-large at $0.13 per million [tokens](GLOSSARY.md#token-billing); `app/setup.py` uploads every article on each run.
- gpt-5's retirement, with no replacement named yet: [retirement schedule](https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirement-schedule). The 30 days, the Responses API's default: [Responses API](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses).
- The embedding length, the missing key and the logged filters: Dewey's `app/setup/search_manager.py` and `app/providers/azure_search.py`.

</details>

## Claude with Voyage AI

**In one line:** Claude writes the answers, Voyage AI (a MongoDB service) makes the embeddings and reranks, and Postgres holds the archive.

**Accounts:** Anthropic, for an [API key](GLOSSARY.md#api-key) ([Claude (Anthropic API)](#claude-anthropic-api)); MongoDB Atlas, for a Voyage AI key and a payment method on file ([why](#voyage-ai-search-models)); and a Postgres database ([Postgres with pgvector](#postgres-with-pgvector)).

**What it costs** (list prices):

- **Claude:** about 4–12¢ per `ask_archive` or demo-page question (estimate). `search_archive` makes no Claude call.
- **Voyage AI:** each model's first 200 million [tokens](GLOSSARY.md#token-billing) are free. Then voyage-4 costs $0.06 and rerank-3 $0.05 per million: about $0.76 per index run of 10,000 articles, and at most about $0.0013 per search (estimates). The free tokens cover about 15 index runs and 7,500 searches.
- **Postgres:** free on your own computer ([Docker's terms](#postgres-with-pgvector) apply to Docker Desktop), or on Neon's and Supabase's free plans.

**Computer:** any that runs Python 3.11–3.13, including Intel Macs.

**Downloads:** about 90 MB of Python packages (measured once on an Apple M3 Max with 36 GB, November 2026; yours will differ), plus the database.

**Who receives questions and articles:** Anthropic, and MongoDB, which may train its models on your questions and archive until you switch that off ([how](#voyage-ai-search-models); [details](#what-the-hosted-providers-receive)).

**Limits:**

- **Re-embedding:** every index run embeds all of `data/` again, [trial-run](GLOSSARY.md#trial-run) `-sample` files included ([delete them](IMPORTING.md#import-everything)). That costs money once the free tokens are gone.
- **No retries:** a rate-limit error stops indexing. Run it again: finished articles stay, but every article goes to Voyage AI again.

**How it's been tested:** not yet run with a real Voyage AI account, and no automated test covers the Voyage code. Claude itself ran on real articles in the next setup's test.

**Set it up:** run the setup wizard with this setup's answers ([INSTALL.md](INSTALL.md#which-setup-to-choose)); it also asks for your Voyage AI key and your database. Or put these lines in `.env` with a text editor (not in a terminal), changing any that are already there ([how](#configuring-by-hand)):

```dotenv
DEWEY_STACK='anthropic'
ANTHROPIC_API_KEY='YOUR_ANTHROPIC_KEY'
EMBEDDING_PROVIDER='voyage'
RERANK_PROVIDER='voyage'
VOYAGE_API_KEY='YOUR_VOYAGE_KEY'
DATABASE_URL='postgresql://dewey:YOUR_DB_PASSWORD@127.0.0.1:5432/dewey'
```

<details>
<summary>Sources and arithmetic</summary>

Checked November 2026:

- Claude's prices and tokens per word: [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing) and [Claude Opus 5.5](https://platform.claude.com/docs/en/models/opus-5-5/overview). The arithmetic is in [Claude (Anthropic API)](#claude-anthropic-api).
- Voyage AI's prices and free tokens: [MongoDB: billing](https://www.mongodb.com/docs/voyageai/management/billing/). Limits without a payment method: [MongoDB: API overview](https://www.mongodb.com/docs/voyageai/api-reference/overview/) and [rate limits](https://www.mongodb.com/docs/voyageai/management/rate-limits/).
- The Voyage AI estimates assume 1.3 tokens per word. Indexing 10,000 articles of about 800 words sends about 13 million tokens. Each search reranks up to 50 excerpts, at most about 27,000 tokens. These counts come from Dewey's code, `app/providers/pgvector_store.py`: 380-word chunks with a 70-word overlap, each led by the headline, and 50 candidates per search.
- No retries: Dewey creates the Voyage client with its default `max_retries=0` ([voyageai 0.6.0](https://github.com/voyage-ai/voyageai-python/blob/v0.6.0/voyageai/client.py)).
- The database's download sizes are under [Postgres with pgvector](#postgres-with-pgvector).

</details>

## Claude with local search models

**In one line:** Claude writes the answers, and two open-weight search models run on your own computer, so only `ask_archive` and demo-page questions leave it.

**Accounts:** Anthropic, with an API key or an `ant` sign-in ([Claude (Anthropic API)](#claude-anthropic-api)), and a Postgres database.

**What it costs:** about 4–12¢ per `ask_archive` or demo-page question, for Claude (estimate). The search models cost nothing. Postgres: as for [Claude with Voyage AI](#claude-with-voyage-ai).

**Computer:**

- **A Mac with Apple silicon** on macOS 12 or later; macOS 14 or later gets the current [PyTorch](GLOSSARY.md#pytorch). Not an Intel Mac: PyTorch's last Intel Mac version is 2.2.2, and the search models need 2.9 or later.
- **A Windows PC** (64-bit Intel or AMD) **or a Linux computer:** the packages exist (not installed by us).
- **Memory:** the embedding model used about 3–4 GB (measured once on an Apple M3 Max with 36 GB, November 2026; yours will differ). Local reranking adds about 2.3 GB, the size of its weights (calculated, not measured).

**Downloads:** about 3.3 GB on a Mac, plus the database. With reranking off, the reranker's 2.29 GB never downloads.

**Who receives questions and articles:** Anthropic, for `ask_archive` and the demo page ([details](#what-the-hosted-providers-receive)).

**Limits:**

- **Speed:** on a Mac, the embedding model runs on the processor. Indexing took about 9 minutes per 1,000 articles of about 1,300 words, about an hour and a half for 10,000, plus writing to the database (measured once on an Apple M3 Max with 36 GB, November 2026; yours will differ).
- **First use is slow,** and **voyage-4-nano runs code from its own repository** when it loads ([Local search models](#local-search-models)).

**How it's been tested:** run end to end on 19 November 2026, on an Apple silicon Mac with reranking off: 41 articles (40 public stories from a newsroom's WordPress site and one outside report), searched through Claude Code by topic, date and byline, with cited answers. The local reranker wasn't part of that run; automated tests check model loading with stand-ins.

**Set it up:** run the setup wizard with this setup's answers ([INSTALL.md](INSTALL.md#which-setup-to-choose)); it also asks about an NVIDIA GPU ([the answer](#local-search-models)). Or put these lines in `.env` with a text editor (not in a terminal), changing any that are already there ([how](#configuring-by-hand)):

```dotenv
DEWEY_STACK='anthropic'
ANTHROPIC_API_KEY='YOUR_ANTHROPIC_KEY'
EMBEDDING_PROVIDER='local'
RERANK_PROVIDER='local'
DATABASE_URL='postgresql://dewey:YOUR_DB_PASSWORD@127.0.0.1:5432/dewey'
```

To skip reranking, as the test did, write `RERANK_PROVIDER='none'` instead.

## Fully open on your own computer

**In one line:** everything runs on hardware you control, with no accounts or keys: a chat model in Ollama, the two local search models, and Postgres.

> [!WARNING]
> **Raise Ollama's context length to 32k, or answers won't be about your question.** Each answer request is about 7,100 tokens, but Ollama's default on most computers is 4,096. It cuts longer prompts without an error, so the model never sees the instructions, the question or the best-matching articles.
>
> In the Ollama app, open **Settings** and move **Context length** to **32k** (16k with 16 GB of memory; *menu path unverified*). For `ollama serve` or Linux, set `OLLAMA_CONTEXT_LENGTH=32768`, in digits; Dewey's Compose `ollama` service doesn't. Steps: [MODELS.md](MODELS.md#a-self-hosted-with-ollama).

**Accounts:** none for Dewey. Your AI tool may need its own.

**What it costs:** no fees, only your computer and its electricity ([Docker's terms](#postgres-with-pgvector) apply to Docker Desktop).

**Computer** (estimates from the models' sizes; [memory budget](#running-a-chat-model-yourself)):

- **For example, an Apple-silicon Mac with 32 GB of memory,** on macOS 14 or later. With qwen3:8b, 24 GB should be enough; 16 GB is probably too little once Dewey's search models are loaded.
- **Not an Intel Mac:** the search models need PyTorch 2.9 or later, and Ollama runs on the processor only there.
- **A Windows or Linux PC with an NVIDIA card** that has 12 GB of memory for qwen3:8b, or 16 GB for gpt-oss:20b. Windows isn't tested.

**Downloads:** about 8.7 GB on a Mac with qwen3:8b (17.3 GB with gpt-oss:20b), or about 10.2 GB on a Linux PC with qwen3:8b, plus the database. Ollama alone is about 1.9 GB on Windows.

**Who receives questions and articles:** nobody outside your own computers. [Hugging Face](GLOSSARY.md#hugging-face) and Ollama's model library only serve the downloads.

**Limits:**

- **Answer quality:** small local models are more likely than Claude or GPT-class models to miss a [citation](GLOSSARY.md#citation), cite the wrong source, or not say when the archive can't answer. Check citations against the articles before relying on an answer ([MODELS.md](MODELS.md#test-before-you-rely-on-it)).
- **[Search planning](GLOSSARY.md#search-planning):** when the model's reply doesn't fit Dewey's JSON format, Dewey searches without date or author filters ([MODELS.md](MODELS.md#what-dewey-needs-from-the-model)).
- **Speed** with Ollama hasn't been measured. Local reranking slows every question; `RERANK_PROVIDER='none'` skips it.

**How it's been tested:** not yet run with a real Ollama server; automated tests use a stand-in. The local embedding model ran in the previous setup's test; the local reranker, this setup's default, and small models' answers haven't been checked.

**Set it up:**

1. **Install Ollama, set its context length and download a model** ([MODELS.md](MODELS.md#a-self-hosted-with-ollama)). This downloads 5.2 GB:

   ```bash
   ollama pull qwen3:8b
   ```

2. **Run the setup wizard** with this setup's answers ([INSTALL.md](INSTALL.md#which-setup-to-choose)), and type the model name when it asks.

Or put these lines in `.env` with a text editor (not in a terminal), changing any that are already there ([how](#configuring-by-hand)):

```dotenv
DEWEY_STACK='open'
OPEN_LLM_MODEL='qwen3:8b'
EMBEDDING_PROVIDER='local'
RERANK_PROVIDER='local'
DATABASE_URL='postgresql://dewey:YOUR_DB_PASSWORD@127.0.0.1:5432/dewey'
```

Without `OPEN_LLM_BASE_URL`, Dewey uses Ollama on the same computer, `http://localhost:11434/v1`. Another model server, such as [vLLM](GLOSSARY.md#vllm) ([MODELS.md](MODELS.md#b-self-hosted-with-vllm)) or llama.cpp's, needs `OPEN_LLM_BASE_URL`, and `OPEN_LLM_API_KEY` if it checks keys.

<details>
<summary>Sources</summary>

Checked November 2026:

- Ollama's default context and how it cuts prompts: [Ollama: context length](https://github.com/ollama/ollama/blob/main/docs/context-length.mdx) and [Ollama's code at v0.40.16](https://github.com/ollama/ollama/blob/v0.40.16/server/routes.go#L2570-L2586). The slider: [Settings.tsx](https://github.com/ollama/ollama/blob/v0.40.16/app/ui/app/src/components/Settings.tsx#L743-L775). The 7,100 tokens were counted with Qwen3's tokenizer on a sample request.
- Model sizes: Ollama's library pages for [qwen3](https://ollama.com/library/qwen3/tags) (qwen3:8b, 5.2 GB) and [gpt-oss](https://ollama.com/library/gpt-oss/tags) (gpt-oss:20b, 13.8 GB). The Ollama app, 230 MB on a Mac and about 1.6 GB on Linux: [release v0.40.16](https://github.com/ollama/ollama/releases/tag/v0.40.16). The rest of the download is the search models (about 3.0 GB) and the Python packages (about 0.3 GB on a Mac).
- The example computer is our estimate from those sizes ([memory budget](#running-a-chat-model-yourself)).
- Intel Macs: [PyTorch's CPU index](https://download.pytorch.org/whl/cpu/torch/), whose last Intel Mac version is 2.2, and [voyageai 0.6.0 on PyPI](https://pypi.org/pypi/voyageai/0.6.0/json), which needs `torch>=2.9.0`. Ollama on Intel Macs: [Ollama: macOS](https://github.com/ollama/ollama/blob/main/docs/macos.mdx). In Docker on a Mac, Ollama can't use the GPU at all ([Ollama FAQ](https://github.com/ollama/ollama/blob/main/docs/faq.mdx)).

</details>

## Open-weight models through OpenRouter

**In one line:** an open-weight chat model runs at a company that OpenRouter picks for each request; the search models and Postgres stay on your computer.

> [!WARNING]
> **Questions leave your network.** Every `ask_archive` and demo-page question goes to OpenRouter, and to the company it picks to run the model, with up to ten excerpts. Questions can reveal what a reporter is working on. Dewey asks for providers that don't keep or train on your data, but those are policies, not technical controls ([MODELS.md](MODELS.md#what-the-privacy-settings-do-and-dont-do)).

**Accounts:** OpenRouter, with an API key and credits ([MODELS.md](MODELS.md#c-hosted-through-openrouter)), and a Postgres database.

**What it costs:** OpenRouter passes on each provider's price, and adds 5.5% ($0.80 minimum) when you buy credits, which can expire after a year. On endpoints that meet Dewey's default privacy setting, `openai/gpt-oss-20b` costs from about $0.02 per million input tokens and $0.09 per million output, roughly $0.0002–$0.001 a question; `nousresearch/hermes-4-405b` costs $1 and $3, about $0.01 a question (estimates). `search_archive` costs nothing at OpenRouter.

The `openai/` in `openai/gpt-oss-20b` names the model's maker, which published it as an open-weight model; the request goes to OpenRouter, which picks a company that hosts it.

**Computer:** as for [Claude with local search models](#claude-with-local-search-models), so not an Intel Mac.

**Downloads:** about 3.3 GB on a Mac (the Python packages, PyTorch and the search models), plus the database.

**Who receives questions and articles:** OpenRouter and the provider it picks, for `ask_archive` and the demo page ([details](#what-the-hosted-providers-receive)).

**Limits:** Dewey's default privacy setting, `strict`, can rule out the cheapest provider (for gpt-oss-20b, one of 12 endpoints). Some model ids, such as `qwen/qwen3-8b` and `nousresearch/hermes-4-70b`, have no providers at all. If no provider fits, Dewey stops with an error that says what to change ([MODELS.md](MODELS.md#when-openrouter-cant-route-a-request)). Hermes 4 405B carries Meta's Llama 3 license, which isn't [open source](GLOSSARY.md#open-source) ([MODELS.md](MODELS.md#hermes-model-licenses)).

**How it's been tested:** not yet run with a real OpenRouter account. Automated tests check, against a stand-in server, what Dewey sends (privacy preferences included) and how it explains routing errors. No test can check what OpenRouter or its providers do with your data.

**Set it up:** create a key on OpenRouter's [API keys page](https://openrouter.ai/keys) (keys start with `sk-or-v1-`). Run the setup wizard with this setup's answers ([INSTALL.md](INSTALL.md#which-setup-to-choose)): type `https://openrouter.ai/api/v1` at "Server URL", then the model id, and paste the key when it asks. Or put these lines in `.env` with a text editor (not in a terminal), changing any that are already there ([how](#configuring-by-hand)):

```dotenv
DEWEY_STACK='open'
OPEN_LLM_BASE_URL='https://openrouter.ai/api/v1'
OPEN_LLM_API_KEY='YOUR_OPENROUTER_KEY'
OPEN_LLM_MODEL='openai/gpt-oss-20b'
EMBEDDING_PROVIDER='local'
RERANK_PROVIDER='local'
DATABASE_URL='postgresql://dewey:YOUR_DB_PASSWORD@127.0.0.1:5432/dewey'
# OPENROUTER_PRIVACY='strict' is the default. See MODELS.md before you change it.
```

Dewey refuses to start without the key, or with an `http://` OpenRouter address.

<details>
<summary>Sources</summary>

Checked November 2026:

- The fee and credit expiry: [OpenRouter FAQ](https://github.com/OpenRouterTeam/docs/blob/main/faq.mdx), with the fee's value in [constants.mdx](https://github.com/OpenRouterTeam/docs/blob/main/snippets/exports/constants.mdx).
- Prices and endpoints: OpenRouter's [models API](https://openrouter.ai/api/v1/models) and each model's endpoint list. These change often.
- Which endpoints the privacy setting removes: [OpenRouter: Zero Data Retention](https://github.com/OpenRouterTeam/docs/blob/main/guides/features/zdr.mdx) and [provider selection](https://github.com/OpenRouterTeam/docs/blob/main/guides/routing/provider-selection.mdx).
- The key prefix: [OpenRouter: authentication](https://github.com/OpenRouterTeam/docs/blob/main/api_reference/authentication.mdx).
- The per-question estimate counts Dewey's prompts (`app/tools/retrieve.py` and `app/tools/answer.py`) and 500–2,000 output tokens. gpt-oss-20b is an open-weight model that OpenAI published.

</details>

## What the hosted providers receive

> [!WARNING]
> **Questions can leave your network, and some are sensitive** (for example, ones that name a confidential source). Check each provider's data-use and retention terms, and the settings you control, before you open Dewey to reporters.

These notes weren't verified against the providers' current terms, except where a line says it was checked in November 2026. The linked pages are the authority.

- **Voyage AI (MongoDB Atlas)**, with `voyage` search models: every question, up to 50 candidate excerpts per search, and the whole archive at indexing. MongoDB documents that it can use this data to improve Voyage AI models ([Model training data](https://www.mongodb.com/docs/voyageai/management/model-training-data/)). It doesn't give the default, and you can opt out in Atlas **Organization Settings** only with a payment method on file, so assume it's on until you switch it off (checked November 2026).
- **Anthropic (Claude)**, for `ask_archive` and the demo page: the conversation and up to 10 excerpts. Your API terms, and any zero-data-retention arrangement, set retention and use: check them in the Claude Console.
- **Azure OpenAI and Azure AI Search:** every question (to embed it); the conversation and retrieved text for `ask_archive` and the demo page; and the whole archive at indexing.
  - Abuse monitoring can keep prompts and outputs for a limited time, unless Microsoft approved your organization for modified abuse monitoring (["Data, privacy, and security"](https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/openai/data-privacy)).
  - The Responses API keeps each response for 30 days by default, and Dewey doesn't turn that off ([Microsoft: Responses API](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses), checked November 2026). "Global" deployments may process prompts in any region where the model runs.
  - Dewey's error output, which your logs may keep, includes each search's bylines and dates.
- **A hosted OpenAI-compatible server** (`OPEN_LLM_BASE_URL` at another company): the conversation and up to 10 excerpts, under its terms. OpenRouter also stores metadata, such as token counts, for every request. Dewey asks it per request for providers that don't collect data and for [zero data retention](GLOSSARY.md#zero-data-retention) (`OPENROUTER_PRIVACY`; [MODELS.md](MODELS.md#c-hosted-through-openrouter)); your account's privacy settings apply too. Your own model server, with `local` or `none` search models, keeps questions and articles on your own machines.
- **Your AI tool's own model**, on every setup: what `search_archive` returns and what you type, under its terms. Claude Code, for example, sends them to the Claude account it signs in with.
- **Hugging Face**, with local search models: only the model downloads.

## The parts in detail

### Claude (Anthropic API)

Both Claude setups use the [Anthropic API](GLOSSARY.md#anthropic-api). Create an account and an API key in the [Claude Console](https://platform.claude.com); keys start with `sk-ant-`. It bills per token, and new accounts get free trial credits ([Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing)).

**What a question costs:** each `ask_archive` call or demo-page question makes two calls to Claude Opus 5.5, one to plan the search and one to write the answer: about 4–12¢ in all (an estimate, not measured). Each step stops at 16,000 output tokens, so one call can't cost more than about 70¢.

<details>
<summary>The model, its prices and the arithmetic</summary>

Dewey uses Claude Opus 5.5 (`claude-opus-5-5`), at `low` effort for search planning and `medium` for the answer; `ANTHROPIC_MODEL`, `ANTHROPIC_METADATA_EFFORT` and `ANTHROPIC_ANSWER_EFFORT` override these. It costs $4 per million input tokens and $20 per million output; cache writes cost $5 and cache reads $0.20 (checked November 2026).

The two calls use about 9,000 tokens, with the answer written from up to 10 excerpts. The estimate rests on Anthropic's figure of about 555,000 words per million tokens, on answers of 150–500 words, and on "thinking" tokens, which Claude bills as output.

</details>

<details>
<summary>Refusals</summary>

If a safety classifier declines a request, Anthropic's server-side fallback retries it on a fallback model in the same call, at that model's rates. If the whole chain declines, Dewey says so in the answer ([Anthropic: refusals](https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback)).

</details>

<details>
<summary>Signing in without an API key</summary>

On your own computer, Anthropic's `ant` tool can store a sign-in instead ([Anthropic's CLI docs](https://platform.claude.com/docs/en/cli-sdks-libraries/cli/authentication), checked November 2026). On a Mac with [Homebrew](GLOSSARY.md#homebrew), install it:

```bash
brew install anthropics/tap/ant
```

Then sign in. A browser opens, and you pick the workspace:

```bash
ant auth login
```

Dewey's Claude client then uses the sign-in saved under `~/.config/anthropic` (on Windows, `%APPDATA%\Anthropic`). On Linux, get `ant` from its [releases page](https://github.com/anthropics/anthropic-cli/releases). To see what the `ant` tool finds in your terminal, run:

```bash
ant auth status
```

That is `ant`'s own view, and it can differ from Dewey's: `ant` doesn't read Dewey's `.env`. With `ANTHROPIC_PROFILE` naming a sign-in that isn't saved and Workload Identity Federation variables set, `ant` shows federation as active, while Dewey's Claude client stops (seen with ant 2.12.1, November 2026).

With a sign-in, leave `ANTHROPIC_API_KEY` out of `.env` and out of your terminal: a key always wins. In the setup wizard, press Enter at the key question; if a key is already saved, type `-` instead, since Enter keeps it. By hand, delete the template's `ANTHROPIC_API_KEY` line: a blank value makes Dewey refuse to start. The wizard's check step tests only an API key; for a sign-in it prints `Claude sign-in: not checked`.

Dewey and the wizard look for the sign-in in the same order as Anthropic's client ([credential precedence](https://platform.claude.com/docs/en/manage-claude/wif-reference#credential-precedence), checked November 2026): a key or token first, then the sign-in `ANTHROPIC_PROFILE` names, then Workload Identity Federation variables, then the active sign-in (`default` unless you changed it). They check that the sign-in's files are in place. They don't ask Anthropic whether it still works, so an expired or revoked sign-in passes both.

- **A sign-in that isn't saved.** If `ANTHROPIC_PROFILE` or the active sign-in names one that doesn't exist, or its settings file is damaged, the Claude client doesn't fall back to another credential. The wizard lists `ANTHROPIC_API_KEY` under `Still missing:` and Dewey won't start; both name the sign-in and print the commands that fix it.
- **Signed out.** After `ant auth logout`, the wizard says `Dewey could not confirm a Claude credential` and carries on: importing and indexing don't use Claude. Dewey starts with the same warning: searches work, but `ask_archive` and the demo page fail until you sign in again. No restart is needed afterwards.

To sign in again under a name:

```bash
ant auth login --profile NAME
```

That also makes NAME the active sign-in, for `ant` and for Anthropic's other tools on your computer ([Anthropic's CLI docs](https://platform.claude.com/docs/en/cli-sdks-libraries/cli/authentication) and [active profile](https://platform.claude.com/docs/en/manage-claude/wif-reference#active-profile), checked November 2026). `ANTHROPIC_PROFILE` can go in `.env`: Dewey reads it there, and so does the wizard.

**On a server** (Docker, Fly, Render, a CI job), Dewey can't see the sign-in on your computer. Use an API key, or Workload Identity Federation: short-lived tokens from your platform's identity provider, set up by an Anthropic organization admin ([Anthropic: Workload Identity Federation](https://platform.claude.com/docs/en/manage-claude/workload-identity-federation)). Dewey reads `ANTHROPIC_FEDERATION_RULE_ID`, `ANTHROPIC_ORGANIZATION_ID` and `ANTHROPIC_IDENTITY_TOKEN_FILE`, plus `ANTHROPIC_WORKSPACE_ID` when the rule covers several workspaces.

</details>

### Voyage AI search models

Anthropic doesn't offer an embedding model; its documentation points to Voyage AI ([Anthropic embeddings guide](https://platform.claude.com/docs/en/build-with-claude/embeddings)). Voyage AI is part of MongoDB, so its keys and billing live in MongoDB Atlas. Dewey uses two current models, `voyage-4` (1,024-dimension embeddings) and `rerank-3`. Prices: [Claude with Voyage AI](#claude-with-voyage-ai).

To get a key:

1. **Create or open an Atlas account** on the [Atlas sign-up page](https://www.mongodb.com/cloud/atlas/register?onboardingScenario=voyage). A new account gets an organization and a project.
2. **Add a payment method** (a card or PayPal): open your organization, then the **Billing** icon at the upper right. Atlas charges $1.00 to check a card, then refunds it.

   Without one, Atlas allows only 3 requests and 10,000 tokens a minute, and you can't turn off data collection. Dewey's indexer would then stop with a rate-limit error after a few articles (our reading of the limits; not tested).
3. **Turn off data collection,** if you don't want MongoDB to train Voyage models on your archive and questions: in **Organization Settings**, switch **Help Improve Voyage AI Models** to Off.
4. **Create the key:** in your project, choose **AI Model APIs**, then **Create model API key**. Name it, leave **Cloud provider** and **Geography** set to **any**, and select **Create**.
5. **Copy the key now;** Atlas shows it only once. Paste it at the wizard's Voyage AI key question, or into `VOYAGE_API_KEY`.

A key scoped to a geography (one starting `al-eu-`, for example) won't work: Dewey's Voyage library sends every Atlas key to `ai.mongodb.com` (from the library's code; not tested).

<details>
<summary>Sources</summary>

Checked November 2026:

- Signing up and creating a key: [MongoDB: Voyage AI quick start](https://www.mongodb.com/docs/voyageai/quickstart/) and [MongoDB: API keys](https://www.mongodb.com/docs/voyageai/management/api-keys/).
- The payment method, the $1.00 check and the free-trial limits: [MongoDB: billing](https://www.mongodb.com/docs/voyageai/management/billing/) and [MongoDB: API overview](https://www.mongodb.com/docs/voyageai/api-reference/overview/), which also explains scoped keys.
- Data collection: [MongoDB: model training data](https://www.mongodb.com/docs/voyageai/management/model-training-data/) and [managing organization settings](https://www.mongodb.com/docs/atlas/tutorial/manage-organization-settings/#manage-data-collection-for-voyage-ai-model-training).
- Where Atlas keys go: [voyageai 0.6.0, util.py](https://github.com/voyage-ai/voyageai-python/blob/v0.6.0/voyageai/util.py).

</details>

### Local search models

Two open-weight models run inside Dewey's own process, so searching needs no account. Which computers they need, and their speed: [Claude with local search models](#claude-with-local-search-models).

| Model | Job | Size | Download | License |
|---|---|---|---|---|
| [voyage-4-nano](https://hf.co/MongoDB/voyage-4-nano) | Embeddings | 346 million parameters | 0.70 GB | Apache 2.0 |
| [bge-reranker-v2-m3](https://hf.co/BAAI/bge-reranker-v2-m3) | Reranking | 568 million parameters | 2.29 GB | Apache 2.0 |

**Installing them:** they need PyTorch, a large download. The setup wizard installs it after asking "Does this machine lack an NVIDIA GPU?": **Y** (yes) gets the smaller CPU-only build (on Linux, read the note below first). By hand, with the [virtual environment](GLOSSARY.md#virtual-environment) on, install that build first:

```bash
python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
```

Then the rest:

```bash
python -m pip install -r requirements-local.txt
```

On a Mac, the two commands download about 190 MB, and the virtual environment grows to about 1.5 GB (measured once on an Apple M3 Max with 36 GB, November 2026; yours will differ). Keep the `--index-url` part when you update PyTorch.

**The first time:** each model downloads when first needed (the embedding model on the first index run, the reranker on the first question) into a hidden `.cache/huggingface` folder in your home folder. Later loads use that copy, at that version, without contacting Hugging Face. The reranker's download can make an AI tool give up on its first call, so first ask one question on the demo page ([INSTALL.md](INSTALL.md#try-the-demo-page)). After each start, the first question waits about 5 seconds for the embedding model to load, 35 the very first time after installing (measured once on an Apple M3 Max with 36 GB, November 2026; yours will differ). The reranker's load time wasn't measured.

> [!WARNING]
> **voyage-4-nano runs code from its own repository when it loads** (Hugging Face's `trust_remote_code`), inside the Dewey process that holds your keys and sees every question. So Dewey never updates it by itself.
>
> - **To choose the version,** set `EMBED_MODEL_REVISION` in `.env` to a commit hash from the model's Hugging Face page whose files you have reviewed (`RERANK_MODEL_REVISION` for the reranker). To update, set a newer one, or delete the cached model.
> - **To rule out any request to Hugging Face** once both models are downloaded, add `HF_HUB_OFFLINE='1'` to `.env` (Dewey loads `.env` first: from the code; *not tested*) or to the environment that starts Dewey.
> - **`EMBEDDING_PROVIDER=local` only ever runs voyage-4-nano:** Dewey refuses to start if `EMBED_MODEL` names a hosted Voyage model, rather than send questions to Voyage AI.

<details>
<summary>On Windows</summary>

*Not tested by us.* On Windows, PyTorch is the CPU build whichever way you answer the wizard's NVIDIA question (about 127 MB). The models go into `C:\Users\YOURNAME\.cache\huggingface`. A warning about symlinks and Developer Mode during the first download is harmless.

If PyTorch says `Microsoft Visual C++ Redistributable is not installed`, install Microsoft's runtime and try again:

```powershell
winget install --id 'Microsoft.VCRedist.2015+.x64' -e --source winget
```

Or download it from [aka.ms/vc14/vc_redist.x64.exe](https://aka.ms/vc14/vc_redist.x64.exe) (26 MB).

</details>

<details>
<summary>On Linux</summary>

*Not tested by us.* Answer **Y** to the wizard's NVIDIA question, unless this computer has a recent NVIDIA card (RTX 20 or GTX 16 series or newer) with NVIDIA's driver 580 or newer; `nvidia-smi` shows the driver version. **Y** downloads the CPU-only PyTorch, about 210 MB. **N** downloads PyTorch with NVIDIA's libraries, about 3 GB, and the embedding model then uses the card.

With an AMD or Intel graphics card, answer **Y**: the wizard offers no PyTorch build for those. Updating PyTorch without the `--index-url` part also fetches the 3 GB NVIDIA build.

</details>

<details>
<summary>Sources</summary>

Checked November 2026:

- Sizes and licenses: Hugging Face's file lists for [voyage-4-nano](https://huggingface.co/api/models/MongoDB/voyage-4-nano/tree/main?recursive=true) and [bge-reranker-v2-m3](https://huggingface.co/api/models/BAAI/bge-reranker-v2-m3/tree/main?recursive=true).
- PyTorch builds and sizes: [PyTorch's CPU index](https://download.pytorch.org/whl/cpu/torch/) and [torch on PyPI](https://pypi.org/pypi/torch/json). On a Mac, the CPU-only build is the normal Mac build (139 MB, for macOS 14 or later) and loses nothing. Intel Macs: [voyageai 0.6.0 on PyPI](https://pypi.org/pypi/voyageai/0.6.0/json) needs `torch>=2.9.0`.
- Windows: [PyTorch's Visual C++ check](https://github.com/pytorch/pytorch/blob/v2.15.0/torch/__init__.py). Linux drivers: [NVIDIA's CUDA release notes](https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html).
- From Dewey's code, `app/providers/embeddings.py`: where the embedder runs (the processor, or an NVIDIA card when PyTorch can use one; voyage-4-nano runs through sentence-transformers, as the `voyageai` library's local mode runs it), the pinned versions and the refusal to run a hosted model.

</details>

### Running a chat model yourself

The wizard suggests two models for the fully open setup. It calls qwen3:8b (5.2 GB) "a reasonable start on a laptop", which by the memory budget below holds from 24 GB of memory. gpt-oss:20b (13.8 GB) is stronger, and needs much more memory. [MODELS.md](MODELS.md#what-dewey-needs-from-the-model) covers choosing a model, vLLM and Hermes models.

The chat model, its context and Dewey's search models all need memory at once. The example computers under [Fully open on your own computer](#fully-open-on-your-own-computer) rest on this budget.

<details>
<summary>Memory budget</summary>

Calculated from the models' published sizes, not measured (low to medium confidence):

| | qwen3:8b, 16k context | qwen3:8b, 32k context | gpt-oss:20b, 32k context |
|---|---|---|---|
| Ollama's model file | 4.87 GiB | 4.87 GiB | 12.85 GiB |
| Context memory | 2.25 GiB | 4.5 GiB | 0.75 GiB |
| Dewey's two search models | 3.41 GiB | 3.41 GiB | 3.41 GiB |
| **Subtotal** | **10.5 GiB** | **12.8 GiB** | **17.0 GiB** |

Not included: Ollama's working memory, Python and PyTorch, Postgres, Docker's virtual machine, macOS and your other apps. A Mac sold with "32 GB" has 32 GiB. With `RERANK_PROVIDER='none'`, the search models take about 1.3 GiB instead of 3.41.

On a PC, an NVIDIA card needs about 7.1 GiB for qwen3:8b with a 16k context. A 12 GB card fits that, and an 8 GB card would spill over to the processor. For gpt-oss:20b it needs about 13.6 GiB, so a 16 GB card, matching OpenAI's own "run within 16GB of memory" ([gpt-oss README](https://github.com/openai/gpt-oss/blob/main/README.md)). These are estimates.

</details>

<details>
<summary>The parts and their licenses</summary>

| Part | What it is | License |
|---|---|---|
| Chat model | Any open-weight model on an OpenAI-compatible server. The wizard suggests [Qwen3-8B](https://hf.co/Qwen/Qwen3-8B) (8 billion parameters; `qwen3:8b` in Ollama) and [gpt-oss-20b](https://hf.co/openai/gpt-oss-20b) (21 billion; `gpt-oss:20b`) | Apache 2.0 (both) |
| Model server | [Ollama](https://github.com/ollama/ollama), the default, or vLLM or llama.cpp's server | MIT (Ollama) |
| Search models | voyage-4-nano and bge-reranker-v2-m3 ([Local search models](#local-search-models)) | Apache 2.0 |
| Database | PostgreSQL with [pgvector](https://github.com/pgvector/pgvector) | PostgreSQL License (Postgres); see the pgvector repository |
| Python libraries | gradio, openai (client only), voyageai, sentence-transformers, psycopg, pgvector, pydantic | Apache 2.0, MIT or BSD, except psycopg (LGPL-3.0) |

</details>

### Postgres with pgvector

The four Postgres setups find their database through `DATABASE_URL`, written like `postgresql://USER:PASSWORD@HOST:5432/DATABASE`. Percent-encode `%`, `@`, `:`, `/`, `#`, `?` or spaces in the password, such as `%40` for `@`.

Use pgvector 0.8 or newer: with older versions, very selective date or author filters can return fewer articles. Dewey tells you when it finds an older one, in the setup wizard's check and in a warning at its first search ([Upgrade pgvector](#upgrade-pgvector)). Dewey runs `CREATE EXTENSION IF NOT EXISTS vector` itself when it indexes. It was tested with PostgreSQL 18.7 and pgvector 0.8.7, from the wizard's Docker image.

| Option | What you do | Notes |
|---|---|---|
| The setup wizard's Docker container | Choose `docker` at the wizard's database question: it prints a `docker run` command for `pgvector/pgvector:pg18`, with a generated password | Published on [localhost](GLOSSARY.md#localhost) only. About 165 MB to download (Docker Desktop itself is about 570–630 MB), 0.7 GB on disk. It stays stopped after a restart ([INSTALL.md](INSTALL.md#start-dewey-again-later)) |
| Homebrew, on a Mac | `brew install postgresql@18 pgvector` | Apple silicon only; about 80 MB. Steps below |
| Postgres.app, on a Mac | Download "Postgres.app with PostgreSQL 18" | Includes pgvector 0.8.7; about 123 MB. Steps below |
| Linux packages | PostgreSQL's own apt repository, or Fedora's packages | Ubuntu 24.04's own pgvector (0.6.0) is too old. Steps below |
| A hosted database | Neon or Supabase, among others | Free plans with limits, and which connection string to use: [DEPLOYMENT.md](DEPLOYMENT.md#managed-postgres-with-pgvector) |

Run only one Postgres on [port](GLOSSARY.md#port) 5432 at a time. The database needs at least about 55 MB per 1,000 articles of about 1,300 words (a calculation; the real size is larger), so 10,000 articles already need more than Supabase's free 500 MB.

**Docker Desktop's license:** free for personal use, education, non-commercial open source projects and small businesses: in Docker's words, "fewer than 250 employees AND less than $10 million in annual revenue". Other professional use, and any government entity, needs a paid subscription ([Docker's terms](https://github.com/docker/docs/blob/main/content/manuals/desktop/setup/install/mac-install.md); prices: [Docker pricing](https://www.docker.com/pricing/)). Docker's summary doesn't mention nonprofits: check its [subscription agreement](https://www.docker.com/legal/docker-subscription-service-agreement) and [FAQ](https://www.docker.com/pricing/faq) rather than assume an exemption.

**Docker on each system:** INSTALL.md gives the requirements and free alternatives for [Mac](INSTALL.md#get-your-mac-ready), [Windows](INSTALL.md#get-your-windows-pc-ready) and [Linux](INSTALL.md#get-your-linux-computer-ready). On Linux, use Docker Engine from Docker's own repository ([Docker: install Engine](https://github.com/docker/docs/blob/main/content/manuals/engine/install/_index.md)), not Debian's `docker.io` package, version 26: Docker before 28.0 can let other machines on your network reach ports published on 127.0.0.1 ([DEPLOYMENT.md](DEPLOYMENT.md#why-docker-engine-280-or-newer)).

<details>
<summary>On a Mac without Docker</summary>

*Not tested by us.* **Homebrew** (Apple silicon only; Homebrew no longer builds packages for Intel Macs). Install Postgres 18 and pgvector together, because `brew install pgvector` alone doesn't install Postgres:

```bash
brew install postgresql@18 pgvector
```

Start Postgres now and at every login:

```bash
brew services start postgresql@18
```

Create Dewey's database:

```bash
/opt/homebrew/opt/postgresql@18/bin/createdb dewey
```

In the setup wizard, choose "I have one already" and enter `postgresql://localhost:5432/dewey`. It needs no password: Homebrew's Postgres trusts connections from your own Mac.

**Postgres.app:** download "Postgres.app with PostgreSQL 18" from [postgresapp.com](https://postgresapp.com/downloads.html); the default download is now PostgreSQL 19, which Dewey hasn't been tested with. Drag it to Applications, open it and click Initialize; pgvector is included. To use its `createdb` command, add its tools to your path:

```bash
sudo mkdir -p /etc/paths.d && echo /Applications/Postgres.app/Contents/Versions/latest/bin | sudo tee /etc/paths.d/postgresapp
```

Then open a new Terminal window and create the database:

```bash
createdb dewey
```

In the setup wizard, use `postgresql://localhost:5432/dewey`. The first time Dewey connects, Postgres.app asks whether to allow it: click OK. Whether the wizard's check waits long enough for you to click is *unverified*.

</details>

<details>
<summary>On Linux without Docker</summary>

*Not tested by us.* **Ubuntu and Debian:** add PostgreSQL's own apt repository, which has the same versions as Dewey's Docker image. On Ubuntu 24.04 this matters: Ubuntu's own pgvector is 0.6.0, which makes date and author filters return fewer articles.

```bash
sudo apt install -y postgresql-common
```

```bash
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh
```

Press Enter when it says "Press Enter to continue". Then install Postgres 18 and pgvector:

```bash
sudo apt install postgresql-18 postgresql-18-pgvector
```

**Fedora:** its own packages are PostgreSQL 18 (18.6 on Fedora 44, 18.3 on Fedora 45) with pgvector 0.8.0. The middle command sets Postgres up to accept the password Dewey sends; without it, Dewey gets `Ident authentication failed for user "dewey"`. Type `y` when dnf asks `Is this ok [y/N]:`; Enter alone cancels.

```bash
sudo dnf install postgresql-server pgvector
```

```bash
sudo env PGSETUP_INITDB_OPTIONS='--auth-host=scram-sha-256' postgresql-setup --initdb
```

```bash
sudo systemctl enable --now postgresql
```

**Then, on all three,** create a user called `dewey`. It asks for a new password twice, and shows nothing as you type; use letters and digits only:

```bash
sudo -u postgres createuser --pwprompt dewey
```

Create its database:

```bash
sudo -u postgres createdb --owner=dewey dewey
```

Turn on pgvector in it. This needs Postgres's own superuser, so Dewey can't do it here:

```bash
sudo -u postgres psql -d dewey -c 'CREATE EXTENSION IF NOT EXISTS vector'
```

In the setup wizard, choose "I have one already" and enter `postgresql://dewey:YOUR_PASSWORD@127.0.0.1:5432/dewey`, with the password you chose. Postgres then starts by itself whenever the computer starts.

</details>

<details>
<summary>On Windows without Docker</summary>

*Not tested by us.* pgvector has no Windows installer: its own instructions compile it with Visual Studio's C++ tools, as an administrator ([pgvector README](https://github.com/pgvector/pgvector)). The other routes are a hosted database, or Postgres inside [WSL](GLOSSARY.md#wsl), which Windows reaches at `localhost` (not tested with Dewey). For a hosted database, choose "I have one already" in the wizard and paste its address.

</details>

<details>
<summary>Sources</summary>

Checked November 2026:

- pgvector 0.8 and Dewey: `app/providers/pgvector_store.py` turns on iterative scanning only with 0.8 or newer. The image's versions: [pgvector README](https://github.com/pgvector/pgvector/blob/master/README.md).
- Homebrew: [pgvector's Homebrew note](https://github.com/pgvector/pgvector/blob/master/README.md#homebrew) and the [pgvector formula](https://github.com/Homebrew/homebrew-core/blob/main/Formula/p/pgvector.rb). Postgres.app: [its extensions page](https://github.com/PostgresApp/PostgresApp/blob/master/docs/extensions/index.md) and [releases](https://github.com/PostgresApp/PostgresApp/releases/tag/v2.9.7).
- Linux: [PostgreSQL for Ubuntu](https://www.postgresql.org/download/linux/ubuntu/) and the [Fedora pgvector package](https://src.fedoraproject.org/rpms/postgresql18-pgvector/blob/f45/f/postgresql18-pgvector.spec).
- Hosted plans: [Neon plans](https://github.com/neondatabase/website/blob/main/content/docs/introduction/plans.md) and [Neon extensions](https://github.com/neondatabase/website/blob/main/content/docs/extensions/pg-extensions.md); Supabase's [billing](https://github.com/supabase/supabase/blob/master/apps/docs/content/guides/platform/billing-on-supabase.mdx), [database size](https://github.com/supabase/supabase/blob/master/apps/docs/content/guides/platform/database-size.mdx), [pausing](https://github.com/supabase/supabase/blob/master/apps/docs/content/guides/platform/free-project-pausing.mdx) and [pgvector](https://github.com/supabase/supabase/blob/master/apps/docs/content/guides/database/extensions/pgvector.mdx).
- The 55 MB: 1,024-dimension vectors at pgvector's stated "4 * dimensions + 8 bytes", stored twice (table and index), plus the text. Indexes, row overhead and compression are left out.
- Docker Desktop: [on a Mac](https://github.com/docker/docs/blob/main/content/manuals/desktop/setup/install/mac-install.md), [on Windows](https://github.com/docker/docs/blob/main/content/manuals/desktop/setup/install/windows-install.md) and [on Linux](https://github.com/docker/docs/blob/main/content/manuals/desktop/setup/install/linux/_index.md). Its paid plans cost $9 (Pro), $15 (Team) or $24 (Business) per user per month, billed annually. A free alternative on a Mac: [Colima](https://github.com/abiosoft/colima). Debian's `docker.io` version: Debian's package archive.

</details>

#### Upgrade pgvector

On pgvector older than 0.8, a search with a date or byline filter can miss stories that match by meaning, without any error. Stories that match by their words still come back, so nothing looks wrong. When Postgres answers from pgvector's index, the filter is applied only after pgvector has picked its nearest candidates, 40 by default, so a filter that few stories pass can leave very little ([pgvector: filtering](https://github.com/pgvector/pgvector/blob/master/README.md#filtering)). From 0.8.0 on it can keep looking until it has enough ([iterative index scans](https://github.com/pgvector/pgvector/blob/master/README.md#iterative-index-scans)), and Dewey switches that on at every search. Dewey tells you about an older version in two places:

- **The setup wizard's check** prints a result that starts `Postgres: connected, but pgvector 0.6.0 is older than 0.8` (with your version), followed by what to do.
- **The indexer, the MCP server and the demo page** print a warning that starts `Dewey: warning: the pgvector extension in this database is version 0.6.0`. Each prints it once per run, at its first search (the indexer: when indexing starts), not when it starts up. The warning goes to Dewey's error output, never to the chat: look in the terminal that runs Dewey or, when an AI tool starts Dewey itself, in that tool's log ([MCP.md](MCP.md#security)).

*Not tested by us:* we haven't run Dewey, or the steps below, on a pgvector older than 0.8. That Dewey still runs on pgvector 0.5 to 0.7 follows from pgvector's changelog and Dewey's code. The two steps are pgvector's own ([Upgrading](https://github.com/pgvector/pgvector/blob/master/README.md#upgrading)); pgvector lists nothing else to do afterwards, such as indexing again.

**First, back up the database.** Step 1 can replace Postgres's own packages too, and Postgres may restart while that happens: searches fail until it is back. On Linux, with a database called `dewey`, this writes the backup to your home folder:

```bash
sudo -u postgres pg_dump -Fc dewey > ~/dewey.dump
```

1. **Install pgvector 0.8 or newer on the database server,** the same way pgvector was installed the first time. Skip this step if the wizard's check says the database server already has it.

   *Not tested by us.* On Ubuntu 24.04 with Ubuntu's own PostgreSQL 16, that way gives 0.6.0 again. PostgreSQL's own apt repository has `postgresql-16-pgvector` 0.8.7 ([its package list](https://apt.postgresql.org/pub/repos/apt/pool/main/p/pgvector/)). Installing it from there also replaces Ubuntu's PostgreSQL 16 with that repository's newer build of PostgreSQL 16. Run these one at a time, and press Enter when the second says "Press Enter to continue":

   ```bash
   sudo apt install -y postgresql-common
   ```

   ```bash
   sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh
   ```

   ```bash
   sudo apt install postgresql-16-pgvector
   ```

   On a hosted database, the provider installs pgvector: see its documentation (not checked by us for any provider).

2. **Update pgvector in Dewey's database.** This needs Postgres's own superuser, so Dewey can't do it for you. On Linux, with a database called `dewey`:

   ```bash
   sudo -u postgres psql -d dewey -c 'ALTER EXTENSION vector UPDATE'
   ```

   On a hosted database, run `ALTER EXTENSION vector UPDATE;` in the provider's SQL editor. Whether your provider allows that wasn't checked by us.

**Then check the version** with the query Dewey itself runs at every search. It should show 0.8 or newer:

```sql
SELECT extversion FROM pg_extension WHERE extname = 'vector';
```

On Linux, from a terminal:

```bash
sudo -u postgres psql -d dewey -c "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
```

A running MCP server or demo page needs no restart: its next search reads the new version. The setup wizard's check then says something like `Postgres: ok, pgvector 0.8.7 available`.

<details>
<summary>Sources</summary>

Checked November 2026:

- pgvector's [Upgrading](https://github.com/pgvector/pgvector/blob/master/README.md#upgrading) section and its [changelog](https://github.com/pgvector/pgvector/blob/master/CHANGELOG.md): iterative index scans arrived in 0.8.0, and the HNSW index type, which Dewey's index uses, in 0.5.0. The changelog's last upgrade note is for 0.6.0.
- Only a superuser can update an extension unless its control file says otherwise ([PostgreSQL: extensions](https://www.postgresql.org/docs/current/extend-extensions.html)), and [pgvector's control file](https://github.com/pgvector/pgvector/blob/master/vector.control) doesn't.
- Ubuntu 24.04's own [pgvector is 0.6.0](https://packages.ubuntu.com/noble/postgresql-16-pgvector) and its [PostgreSQL 16 is 16.15](https://launchpad.net/ubuntu/noble/+source/postgresql-16). In PostgreSQL's apt repository, `postgresql-16-pgvector` 0.8.7 for Ubuntu 24.04 (the files with `pgdg24.04` in their names) depends on a newer `postgresql-16` than Ubuntu's 16.15; that repository's own 16.16 meets it. This comes from the repository's package index, read on 21 November 2026.
- What Dewey prints, and when: `app/providers/pgvector_store.py` (the warning) and `install.py` (the wizard's check). Both go by the version in your database: `extversion`, which the wizard reads as `installed_version`. Before pgvector is turned on in that database, the wizard goes by the version the server would give it, `default_version` ([PostgreSQL: pg_available_extensions](https://www.postgresql.org/docs/current/view-pg-available-extensions.html)).

</details>

## Mixing hosted and local search models

On the two Postgres stacks, `EMBEDDING_PROVIDER` and `RERANK_PROVIDER` choose where each search model runs, separately from the chat model; `azure` ignores both. A setting you leave out takes its stack's default.

| Embeddings / reranking | `RERANK_PROVIDER='voyage'` | `RERANK_PROVIDER='local'` | `RERANK_PROVIDER='none'` |
|---|---|---|---|
| `EMBEDDING_PROVIDER='voyage'` | Both at Voyage AI: the `anthropic` default | Embeddings at Voyage AI, reranking on your computer | Embeddings at Voyage AI, no reranking |
| `EMBEDDING_PROVIDER='local'` | Embeddings on your computer, reranking at Voyage AI | Both on your computer: the `open` default | Embeddings on your computer, no reranking: the combination run end to end in November 2026 |

`voyage` anywhere needs `VOYAGE_API_KEY` and sends Voyage AI every question, plus the candidate excerpts (reranking) or the whole archive at indexing (embeddings). `none` keeps the [hybrid-search](GLOSSARY.md#hybrid-search) order; the wizard calls it "Fastest, likely less precise". Only the embedding choice is fixed for each database ([below](#indexes-are-tied-to-one-embedding-model)).

## Changing your setup later

Re-run the setup wizard to change a setting ([INSTALL.md](INSTALL.md#change-your-settings)). The chat model, the reranker, and Claude's model and effort can change at any time: Dewey doesn't record them. The embedding model can't (next section). `azure` and the Postgres stacks keep separate indexes, so switching between them means indexing again with the other stack's indexer: `app/setup.py` on `azure`, `app/setup_postgres.py` on the others.

### Indexes are tied to one embedding model

[Vectors](GLOSSARY.md#vector) from different embedding models can't be compared, so Dewey records which model built each database and refuses to search it with another. Changing `EMBEDDING_PROVIDER`, `EMBED_MODEL` or `EMBED_DIM` means indexing into a fresh database. If you change one by mistake, you should see something like:

```text
Embedding settings don't match this database (embed_model: index has 'voyage-4', config asks for 'voyage-4-nano'). Either restore the original EMBEDDING_PROVIDER/EMBED_MODEL/EMBED_DIM settings, or index into a fresh database with `python app/setup_postgres.py`.
```

Switching between `anthropic` and `open` changes the default embedding model (`voyage-4` or `voyage-4-nano`), so set `EMBEDDING_PROVIDER` yourself to keep your database's. A fresh database can be a new one on the same Postgres server, or a new container: point `DATABASE_URL` at it and run the indexer ([IMPORTING.md](IMPORTING.md#index-your-articles)). That costs money with Voyage AI, and time with local models ([how long](#claude-with-local-search-models)).

## Configuring by hand

You can skip the setup wizard and write `.env` yourself, in a [terminal](GLOSSARY.md#terminal) in the Dewey folder with the virtual environment on ([how](INSTALL.md#turn-it-on-in-every-new-terminal-window)). The indexer and the MCP server must use the same `.env`, above all the same embedding settings.

1. **Install the Python packages.** This downloads about 90 MB, about half a minute on a fast connection (measured once on an Apple M3 Max with 36 GB, November 2026; yours will differ):

   ```bash
   python -m pip install -r requirements.txt
   ```

   With local search models, also run the two commands under [Local search models](#local-search-models).

2. **Copy the template to `.env`:**

   ```bash
   cp .env.template .env
   ```

3. **Make `.env` readable only by you,** because it will hold your keys:

   ```bash
   chmod 600 .env
   ```

4. **Edit `.env`** in nano, a text editor that runs inside the terminal:

   ```bash
   nano .env
   ```

   Press Control+O, then Enter, to save, and Control+X to quit. Don't use TextEdit: it can turn straight quotes into curly ones. Change `DEWEY_STACK`, then fill in your setup's **Set it up** lines; only your stack's lines are read. Where the template already has a setting, such as `ANTHROPIC_API_KEY` or `DATABASE_URL`, edit that line rather than add another: when a setting appears twice, Dewey uses the last one. A line starting with `#` is a note Dewey ignores: to use a setting written that way, delete the `#`. If you sign in with `ant` instead of a key, delete the `ANTHROPIC_API_KEY` line.

<details>
<summary>On Windows</summary>

*Not tested by us.* In [PowerShell](GLOSSARY.md#powershell), the `python` and `cp` commands work as they are. Skip `chmod`: Windows ignores it. Keep the Dewey folder in your own user folder, not a shared one, because `.env` holds your keys. Edit the file with Notepad:

```powershell
notepad .env
```

</details>

> [!WARNING]
> **Set the newsroom details, or Dewey describes your archive as The Philadelphia Inquirer's.** Without them, Dewey's prompts assume the Inquirer, an archive starting January 2, 1978, and the Greater Philadelphia Region. On the Postgres setups, articles indexed without a `publisher` field are labelled "The Philadelphia Inquirer" in answers and results. If you already indexed, fix `.env` and run the indexer again to replace the label.

5. **Set the newsroom details.** Add these lines to `.env` in a text editor (not in a terminal):

   ```dotenv
   NEWSROOM_NAME='Your Newsroom'
   ARCHIVE_START_DATE='January 1, 2015'
   DEFAULT_REGION=''
   ```

   Use your own values. `DEFAULT_REGION=''` suits a national outlet: Dewey then assumes no region. A regional outlet names the region questions should default to, such as `'the Tri-County Area'`.

**Where to go next:** [index your articles](IMPORTING.md#index-your-articles) ([on `azure`](AZURE.md#build-the-search-index)), then [try the demo page](INSTALL.md#try-the-demo-page) or [connect an AI tool](INSTALL.md#connect-an-ai-tool). To run Dewey for a team: [DEPLOYMENT.md](DEPLOYMENT.md). Back to the [README](../README.md).

# Dewey on Azure

**In short:** This page sets up the `azure` [setup](GLOSSARY.md#setup): Dewey on Microsoft's [Azure OpenAI](GLOSSARY.md#azure-openai) Service, [Azure AI Search](GLOSSARY.md#azure-ai-search) and Azure Blob Storage. It isn't OpenAI's own API, and it isn't the `open` [stack](GLOSSARY.md#stack), which runs [open-weight models](GLOSSARY.md#open-weight-model).

**Who this is for:** someone who can create resources in your newsroom's Azure subscription and paste commands into a [terminal](GLOSSARY.md#terminal).

**You'll need:**

- An Azure subscription where you may create resources and assign roles, for example as its Owner.
- About $74 a month for the search service, plus usage ([costs](#what-you-need-in-azure)).
- Dewey's code, with its [virtual environment](GLOSSARY.md#virtual-environment) on, in a terminal open in the [Dewey folder](GLOSSARY.md#dewey-folder) ([how](INSTALL.md#turn-it-on-in-every-new-terminal-window)).
- Your articles, or a site an [importer](GLOSSARY.md#importer) can read ([choose one](IMPORTING.md#choose-an-importer)).

> [!NOTE]
> Nobody has run this setup since this fork began ([how it's been tested](#limits-of-the-azure-stack)). Reading the code against Microsoft's documentation turned up [known problems](#known-problems) likely to stop it working as shipped. Read them before you create anything in Azure. Azure facts here were checked in November 2026.

**On this page:**

- [What you need in Azure](#what-you-need-in-azure)
- [Configure Dewey](#configure-dewey)
- [Prepare your articles](#prepare-your-articles)
- [Build the search index](#build-the-search-index)
- [Try it](#try-it)
- [Known problems](#known-problems)
- [Limits of the azure stack](#limits-of-the-azure-stack)

## What you need in Azure

Create Azure OpenAI, Azure AI Search and a storage account in the [Azure portal](https://portal.azure.com) before you run the [setup wizard](GLOSSARY.md#setup-wizard). Microsoft's pages now call the first two "Azure OpenAI in Microsoft Foundry Models" and "Foundry IQ (Azure AI Search)" (portal names not checked).

> [!WARNING]
> The search service is billed every hour it exists, even when nobody uses Dewey. On the Basic tier, that's about $74 a month in most US and European regions, and up to about $97 elsewhere.

**Other costs** (estimates for 10,000 articles of about 800 words, from list prices checked November 2026):

- **Semantic ranker:** one request per search; 1,000 a month free, then $1 per 1,000 on the Standard plan.
- **Embeddings:** about $0.25 with text-embedding-3-small, or $1.60 with text-embedding-3-large, to index the archive once.
- **Each question** on the [demo page](GLOSSARY.md#demo-page) or through [`ask_archive`](GLOSSARY.md#ask_archive): about $0.02 to $0.04 with gpt-5. A [`search_archive`](GLOSSARY.md#search_archive) call costs only the ranker and a tiny embedding.
- New Azure accounts get a $200 credit for their first 30 days. Blob Storage costs pennies.

<details><summary>Prices and assumptions</summary>

| Item | List price, US dollars |
|---|---|
| Search service, Basic tier | $0.101 an hour in East US and most US and European regions; up to $0.133 elsewhere (a month is 730 hours) |
| Embeddings (Global) | text-embedding-3-small: $0.02 per million [tokens](GLOSSARY.md#token-billing); text-embedding-3-large: $0.13 |
| Chat model, gpt-5 (Global Standard) | $1.25 per million input tokens; $10 per million output tokens |
| Blob Storage (Hot tier, locally redundant, East US) | $0.0208 per GB a month (under a cent a month here); $0.05 per 10,000 writes (about $0.05 per upload) |

The embedding estimate assumes about 1,230 tokens per article once Azure splits it into overlapping [chunks](GLOSSARY.md#chunk). Each question makes two gpt-5 calls, with about 7,000 tokens in and 1,000 to 3,000 out, counting gpt-5's reasoning.

</details>

**Set up the services** (Microsoft's documented steps, checked November 2026; nobody has followed them with Dewey yet):

1. **Pick regions**: for the search service, one with the semantic ranker that takes new services; for Azure OpenAI, one with your models (**Regions** below).
2. **Create the Azure OpenAI resource.** Select **Create a resource**, search for **Azure OpenAI**, then select **Create**. Microsoft says to **Cancel** any Foundry upgrade the portal offers; Dewey with an upgraded resource is *unverified*.
3. **Deploy a [chat model](GLOSSARY.md#chat-model) and an [embedding](GLOSSARY.md#embedding) model in it**: **Deployments** > **+ Deploy model** > **Deploy base model**. Write down the deployment names you choose: Dewey refers to each model by its deployment name. The wizard asks for both names. Use text-embedding-3-small ([why](#known-problems)).
4. **Create the search service on the Basic tier or higher.** It holds the [search index](GLOSSARY.md#indexing). The Free tier can't have a managed identity and holds only 50 MB, roughly 700 articles (an estimate). Dewey on the new Serverless pricing model is *unverified*.
5. **Turn on the search service's managed identity.** That's its own Azure account, for signing in to Azure OpenAI without a [key](GLOSSARY.md#api-key). Select **Settings > Identity**; on the **System assigned** tab, set **Status** to **On**, then select **Save**.
6. **Give that identity the Cognitive Services OpenAI User role.** On the Azure OpenAI resource, open **Access control** (perhaps "Access control (IAM)"; not checked), then **Add** > **Add role assignment**. Under **Job function roles**, select the role, then **Next**. Under **Members**, select **Managed identity**, then **Members**, pick your search service and select **Review + assign**. Microsoft says this takes the Owner role or another that may assign roles, such as User Access Administrator.
7. **Create a storage account** for a copy of each article, which the search service reads. `app/setup.py` creates its blob container the first time you index.
8. **Choose a semantic ranker plan.** The free plan allows 1,000 requests a month ([why](#known-problems)). For more, open **Settings** > **Premium features** on the search service, then choose **Select plan** in the **Standard** card under **Semantic ranker**.
9. **Collect the endpoints and keys.** The setup wizard says they're on each resource's **Keys and Endpoint** or **Access keys** page (*unverified*: not re-checked in the portal).

<details><summary>Regions</summary>

Checked November 2026 ([search regions](https://learn.microsoft.com/en-us/azure/search/search-region-support), [model regions](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure-region-availability)):

- **No semantic ranker**, for example: Austria East, Belgium Central, Denmark East, Germany North, Norway East, Chile Central and Mexico Central.
- **No new search services** (Microsoft: "in high demand"): East US 2, North Europe, Sweden Central, West Europe, Germany West Central and UAE North.
- **gpt-5 as a Global Standard deployment** (Microsoft's region codes, parsed from its tables):
  - Americas: brazilsouth, canadaeast, centralus, eastus, eastus2, northcentralus, southcentralus, westus, westus3.
  - Europe: francecentral, germanywestcentral, italynorth, norwayeast, polandcentral, spaincentral, swedencentral, switzerlandnorth, uksouth, westeurope.
  - Asia Pacific: australiaeast, japaneast, koreacentral, southeastasia, southindia.
  - Middle East and Africa: southafricanorth, uaenorth.
- **text-embedding-3-large as Global Standard:** the same, plus canadacentral, northeurope and switzerlandwest. Regions for text-embedding-3-small weren't checked.

</details>

**What Azure receives:** your whole archive at indexing, and every question; for `ask_archive` and the demo page, also the conversation and the [excerpts](GLOSSARY.md#excerpt) Dewey found. Chat responses are kept 30 days by default, and Global deployments may process prompts in any geography where the model runs. [What the hosted providers receive](STACKS.md#what-the-hosted-providers-receive) has more, including abuse monitoring and what your AI tool's own model reads ([Microsoft's privacy page](https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/openai/data-privacy)).

<details><summary>Sources</summary>

All checked November 2026. Microsoft's Azure documentation is no longer public on GitHub, so these are Microsoft Learn pages and Microsoft's price list:

- Tiers and the Basic tier's fixed cost: [pricing tiers](https://learn.microsoft.com/en-us/azure/search/search-sku-tier) ("the billing rate becomes both a fixed cost of running the service around the clock").
- The Free tier's limits: [service limits](https://learn.microsoft.com/en-us/azure/search/search-limits-quotas-capacity) ("storage is limited to 50 MB").
- The identity steps, and who may do them: [managed identities](https://learn.microsoft.com/en-us/azure/search/search-howto-managed-identities-data-sources) ("To assign roles, you must be an Owner, User Access Administrator, Role-based Access Control Administrator…").
- The resource and its deployments: [create an Azure OpenAI resource](https://learn.microsoft.com/en-us/azure/foundry-classic/openai/how-to/create-resource) ("Select **Cancel** to proceed without resource type upgrade"; "refer to the deployment name rather than the underlying model name in API calls").
- Semantic ranker plans: [semantic ranker billing](https://learn.microsoft.com/en-us/azure/search/semantic-how-to-enable-disable) and the [Azure AI Search pricing page](https://azure.microsoft.com/en-us/pricing/details/search/), which also gives the free-account credit.
- Model sizes: the [models page](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure).
- Prices: Microsoft's public [Retail Prices API](https://prices.azure.com/api/retail/prices), East US unless noted, because the pricing page draws its prices with JavaScript.
- Privacy and retention: the [privacy page](https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/openai/data-privacy) and the [Responses API page](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses). For Global deployments, "prompts and responses may be processed in any geography where the relevant model sold by Azure is deployed". Prompts and completions "are NOT used to train any generative AI foundation models without your permission or instruction".
- Names: the page titles on Microsoft Learn.

</details>

## Configure Dewey

The setup wizard writes Dewey's settings to a [`.env` file](GLOSSARY.md#env-file) in the Dewey folder, or you write it by hand.

> [!WARNING]
> Fill in the newsroom details on either path. Left blank, Dewey's prompts assume The Philadelphia Inquirer's archive, starting January 2, 1978, and questions about the Greater Philadelphia Region. Dewey also gives that name to your AI tool when it describes its tools.

### With the setup wizard

1. **Start the wizard** from the Dewey folder.

   ```bash
   python install.py
   ```

2. **Choose azure.** At `1. Which stack?`, type `3` and press Enter (Enter alone picks `anthropic`). You should see something like:

   ```text
   Endpoints and keys are on each resource's 'Keys and Endpoint' / 'Access keys' page.
     Azure portal: https://portal.azure.com
     Open it in your browser now? (y/N):
   ```

3. **Answer the 11 Azure questions**, in this order. The three marked hidden show nothing as you paste.

| The wizard asks | What to type |
|---|---|
| `Azure OpenAI endpoint (https://<name>.openai.azure.com)`, then `Azure OpenAI key` (hidden) | Its endpoint, then one of its keys |
| `Chat deployment name`, then `Chat model name (e.g. gpt-5)` | The deployment's name, not the model's; then the model you deployed (only a label) |
| `Embedding deployment name`, then `Embedding model name (e.g. text-embedding-3-large)` | The same for embeddings, spelling the model exactly ([Known problems](#known-problems)) |
| `AI Search endpoint (https://<name>.search.windows.net)`, then `AI Search admin key` (hidden) | Its address, then one of its admin keys |
| `Index name to create (your choice)` | A new name, such as `dewey-archive` |
| `Storage connection string` (hidden), then `Blob container name (your choice)` | Its connection string, then a new name, such as `dewey-articles` |

4. **Answer the remaining screens** as in [What the wizard asks](INSTALL.md#what-the-wizard-asks). Fill in the newsroom details (see the warning above), then press Enter to write `.env` and again to install the packages. You should see something like:

   ```text
     Wrote /Users/you/dewey-ai/.env
   ```

5. **Let the wizard finish.** It leaves checking Azure to `app/setup.py`, which only checks that the settings are filled in. If `data/` holds article files, it asks `Index them now? (y/N):`. Type `y` only once [What you need in Azure](#what-you-need-in-azure) is done; otherwise press Enter and [build the index](#build-the-search-index) later.

### By hand

1. **Install the packages and create `.env`**, as in [Configuring by hand](STACKS.md#configuring-by-hand). On Windows, `.env` keeps its folder's permissions (*unverified* on your PC; `icacls .env` shows them), so keep the Dewey folder inside your own user folder, not a shared one.
2. **Set the Azure lines to your own values.** Set these lines in `.env` (a file in the Dewey folder; edit it in a text editor, don't paste this into the terminal). The template already has all 11 Azure lines, with placeholder values: change those lines rather than adding copies. When a setting appears twice, Dewey uses the last one. Keep the quotes, then save the file. Settings for other setups can stay: Dewey ignores them on azure.

   ```dotenv
   DEWEY_STACK='azure'

   NEWSROOM_NAME='Your Newsroom'
   ARCHIVE_START_DATE='January 1, 2015'
   DEFAULT_REGION=''

   AZURE_OPENAI_ENDPOINT='https://YOUR-RESOURCE.openai.azure.com'
   AZURE_OPENAI_API_KEY='YOUR_AZURE_OPENAI_KEY'
   EMBEDDING_DEPLOYMENT_NAME='YOUR_EMBEDDING_DEPLOYMENT'
   # The template says text-embedding-3-large: read Known problems first
   EMBEDDING_MODEL_NAME='text-embedding-3-small'
   CHATGPT_DEPLOYMENT_NAME='YOUR_CHAT_DEPLOYMENT'
   CHATGPT_MODEL_NAME='gpt-5'
   AZURE_SEARCH_ENDPOINT='https://YOUR-SEARCH-SERVICE.search.windows.net'
   AZURE_SEARCH_API_KEY='YOUR_SEARCH_ADMIN_KEY'
   # A new name you choose; app/setup.py creates the index
   AZURE_SEARCH_INDEX_NAME='dewey-archive'
   AZURE_STORAGE_CONNECTION_STRING='YOUR_CONNECTION_STRING'
   # A name you choose; app/setup.py creates the container
   AZURE_STORAGE_CONTAINER_NAME='dewey-articles'
   ```

   In the template, the newsroom lines start with `#`: delete it so Dewey reads them. Paste the whole connection string from your storage account; it starts with `DefaultEndpointsProtocol=https;`.

To use the [MCP](GLOSSARY.md#mcp) server over HTTP later, also add `DEWEY_MCP_TOKEN` ([make one](MCP.md#before-you-start-http)).

## Prepare your articles

Dewey indexes the article files ([JSON](GLOSSARY.md#json)) in the Dewey folder's `data` folder ([format](IMPORTING.md#article-file-format)). The setup wizard has no import step on azure, so [run an importer](IMPORTING.md#run-an-import) yourself. On azure:

- **Every field must be filled in.** `app/setup.py` skips any article with an empty `headline`, `content`, `url`, `authors` or `publish_date`. So articles with no byline are left out; the importer's [report](IMPORTING.md#read-the-report) counts them.
- **The [publisher](GLOSSARY.md#publisher) is ignored**: the Azure index has no place for it, so Dewey can't tell your own reporting from articles collected elsewhere.
- **The `id` names the uploaded copy** (`doc_ID.json`): a later article with the same `id` replaces the earlier one, and one without an `id` is named by its position.

For each skipped article, you should see something like:

```text
⚠️  Skipping document 12 in wordpress-example-com-0001.json: missing required fields: authors
```

## Build the search index

`app/setup.py` builds the search index and hands Azure your articles (the wizard's `Index them now?` runs it too). Finish [What you need in Azure](#what-you-need-in-azure) first.

1. **Run the script.** This costs money: Azure embeds every article, about $0.25 to $1.60 per 10,000, depending on the model (estimates).

   ```bash
   python app/setup.py
   ```

   You should see something like this (from the script's code; no real Azure run has been captured):

   ```text
   ✅ Indexer 'dewey-archive-indexer' started successfully
   ...
   🎉 Setup completed successfully!
   ```

2. **Look for lines containing `Error` or `Failed`.** The script still ends with `Setup completed successfully!` after an upload or indexer error, and when `data/` has no articles. Its `Documents indexed:` count is the articles it read, not the number Azure indexed.
3. **Wait for Azure's indexer to embed the articles.** In the portal, your search service's indexer (`dewey-archive-indexer`, for example; *unverified*: page names not checked) shows errors in its execution history. For a 401 or 403, recheck steps 5 and 6 in [What you need in Azure](#what-you-need-in-azure).

A missing setting stops the script before it contacts Azure. You should see something like:

```text
❌ Configuration Error:
Missing required environment variables in .env file:
  - AZURE_STORAGE_CONNECTION_STRING: Azure Storage connection string
```

**Running it again** adds new and changed articles. It re-uploads every article and keeps the existing index, though it prints `Index created successfully`. It never deletes: articles removed from `data/` stay in Blob Storage and the index. Azure probably re-embeds every article each run, since each upload replaces the file (our reading; not verified).

<details><summary>What the script does, in order</summary>

It checks that its nine Azure settings are filled in, reads `data/`, and creates the search index unless one with that name exists. It creates or updates the data source `NAME-blob-ds`, the skillset `NAME-skillset` and the indexer `NAME-indexer`, where `NAME` is your index name. Then it uploads the articles and starts the indexer. The skillset splits each article into [chunks](GLOSSARY.md#chunk) of 512 tokens, overlapping by 96, and embeds each chunk.

</details>

## Try it

1. **Start the demo page.** It runs on your computer only, with no login.

   ```bash
   python app/main.py
   ```

   You should see something like:

   ```text
   * Running on local URL:  http://127.0.0.1:7860
   ```

   Open that address in your browser; [Try the demo page](INSTALL.md#try-the-demo-page) explains the rest.

2. **Ask questions you already know the answers to**, such as "What has Jane Doe written about school funding since 2020?" Check a few numbered [citations](GLOSSARY.md#citation) against the articles. Write bylines exactly as the articles show them, capitals included ([why](#limits-of-the-azure-stack)).
3. **Connect your AI tool**: [Connect an AI tool](INSTALL.md#connect-an-ai-tool) and the [client recipes](MCP.md#client-recipes) work the same on azure. Nothing limits how often an AI tool calls `ask_archive`, and each call costs as much as a demo-page question ([cost and privacy](MCP.md#cost-and-privacy)).

If every search fails, see the embedding, identity and semantic ranker rows in [Known problems](#known-problems), or check your keys: Dewey first contacts Azure at the first question.

## Known problems

Found by reading Dewey's code against Microsoft's documentation (November 2026), not on a real Azure account. [The issue list](https://github.com/TeamTrace/dewey-ai/issues) tracks them; report anything else you find there.

> [!WARNING]
> Two of these affect privacy: Microsoft keeps Dewey's chat responses for 30 days by default, and Dewey writes each search's bylines and dates to its log.

| Problem | What to do |
|---|---|
| **Embedding sizes don't match**: the index holds 1,536 numbers per chunk, but searches ask text-embedding-3-large for its default 3,072, so Azure AI Search should refuse them. | Deploy text-embedding-3-small (default 1,536) and set `EMBEDDING_MODEL_NAME` to match; if you already indexed, use a new index name and run `python app/setup.py` again. Not tested with Dewey. |
| **The search service needs a managed identity** (Dewey gives it no Azure OpenAI key); without one, indexing should fail with 401 or 403, though `app/setup.py` reports success. | Steps 4 to 6 in [What you need in Azure](#what-you-need-in-azure) (Microsoft's steps; not tested with Dewey). |
| **gpt-5 retires on February 9, 2027**: it's the template's example chat model, and no replacement is named yet. | Before then, deploy a chat model that supports the Responses API and function calling; put its deployment name in `CHATGPT_DEPLOYMENT_NAME`. Not tested with other models. |
| **Microsoft keeps chat responses for 30 days** (the Responses API's default); Dewey doesn't send `store=False`, by our reading of the code. | Nothing in Dewey yet. |
| **Each search's filters are printed to the log**, including bylines and dates from the question (not on `anthropic` or `open`). | Nothing in Dewey yet. Treat Dewey's error output as sensitive: the terminal, your AI tool's log of Dewey, a server's logs. |
| **The semantic ranker's free plan stops after 1,000 requests a month** (one per search); then Microsoft returns a billing error, so searches should fail. | Switch to the Standard plan (step 8 in [What you need in Azure](#what-you-need-in-azure)); it needs the Basic tier or higher. |

<details><summary>Sources</summary>

Microsoft's pages, all checked November 2026:

- Embedding sizes: the [models page](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure) gives 3,072 output dimensions for text-embedding-3-large and 1,536 for text-embedding-3-small. The [embedding skill page](https://learn.microsoft.com/en-us/azure/search/cognitive-search-skill-azure-openai-embedding) says "The default is the maximum dimensions for each model".
- Managed identity: the embedding skill page says "Omit both apiKey and authIdentity to use the Foundry IQ service's system-assigned identity". Managed identities for indexers are "Not available on the Free tier" ([pricing tiers](https://learn.microsoft.com/en-us/azure/search/search-sku-tier)) and need "the Basic tier or higher" ([managed identities](https://learn.microsoft.com/en-us/azure/search/search-howto-managed-identities-data-sources)).
- Retirement: the [retirement schedule](https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirement-schedule) lists gpt-5 (version 2025-08-07) for 2027-02-09, with no replacement; text-embedding-3-small and text-embedding-3-large retire on 2028-02-09. The [lifecycle policy](https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirements) says "Microsoft selects the official replacement approximately 90–120 days before retirement."
- Retention: the [Responses API page](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses) says "By default, response data is retained for 30 days."
- Semantic ranker: "After the free allowance is consumed, semantic ranker requests return a billing error" ([semantic ranker billing](https://learn.microsoft.com/en-us/azure/search/semantic-how-to-enable-disable)); "First 1k requests free per month" ([pricing page](https://azure.microsoft.com/en-us/pricing/details/search/)).

In Dewey's code: `app/setup/embedding_service.py` (the 1,536 size), `app/setup/search_manager.py` (the skill and vectorizer, with no key), `app/providers/azure_search.py` (the question's embedding with no size, the semantic query and the printed filter) and `app/providers/azure_openai.py` (the Responses API calls).

</details>

## Limits of the azure stack

- **How it's been tested.** Not run against Azure since this fork began in November 2026. The only automated tests check the date and byline filter Dewey writes for Azure AI Search, and they never contact Azure. The Philadelphia Inquirer built Dewey on this setup, and the original project thanks three alpha testers in its newsroom. The fork has since changed how Dewey filters, cites and shows answers, but `app/setup.py` is almost unchanged.
- **API version.** Nobody had an Azure account to test with, so it is *unverified* whether Azure still accepts the one Dewey asks for (`2025-03-01-preview`).
- **Bylines must match exactly**, capitals included, though Dewey's tool description says case doesn't matter: that's true only on the other setups ([the byline rule](MCP.md#the-two-tools)).
- **Indexing differs:** articles without a byline are skipped, there's no publisher, and nothing is ever deleted ([Prepare your articles](#prepare-your-articles), [Build the search index](#build-the-search-index)).

<details><summary>Sources</summary>

The byline rule: Microsoft's [OData comparison reference](https://learn.microsoft.com/en-us/azure/search/search-query-odata-comparison-operators) says string comparisons with `eq` "are case-sensitive" (checked November 2026). Dewey's index gives the `authors` field no normalizer (`app/setup/search_manager.py`), and the filter is built in `app/providers/azure_search.py`.

</details>

**Where to go next:**

- [Client recipes](MCP.md#client-recipes): connect your AI tool.
- [The setups at a glance](STACKS.md#the-setups-at-a-glance): compare `azure` with the other setups.
- [What Dewey needs](DEPLOYMENT.md#what-dewey-needs): run Dewey for a team, on a server.
- [Glossary](GLOSSARY.md): look up a term.

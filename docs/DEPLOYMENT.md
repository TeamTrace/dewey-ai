# Deploying Dewey

**In short:** run Dewey's [MCP server](GLOSSARY.md#mcp-server) and its database on a server, so colleagues can search the archive from the AI tools they already use.
You configure Dewey, start it, index your articles and connect each tool with a shared token.

**Who this is for:** a newsroom data team running Dewey for colleagues who use [agent harnesses](GLOSSARY.md#agent-harness) such as Claude Code, or chat apps such as Open WebUI.
Dewey has no login system or chat product of its own, and its [demo page](GLOSSARY.md#demo-page) is for your own computer only.

**You'll need:** for Fly.io and Render, Dewey set up on your own computer too ([INSTALL.md](INSTALL.md#before-you-start)), because you index from there.
Commands that start with `python app/` run in the Dewey folder with its [virtual environment](GLOSSARY.md#virtual-environment) on ([how](INSTALL.md#turn-it-on-in-every-new-terminal-window)).

> [!NOTE]
> **Status, November 2026:** tests check that the Dockerfile, `docker-compose.yml` and the Fly.io and Render examples parse and that their key settings match the code; `docker compose config --quiet` accepts the Compose file.
> Nobody has built the image, run `docker compose up` or deployed to Fly.io or Render yet: expect to adjust details, and tell the maintainers what you changed.
> Confirm anything marked *unverified* in the provider's own docs.

**On this page:**

- [What runs](#what-runs), [what Dewey needs](#what-dewey-needs), [access control](#access-control), [the demo page](#the-demo-page)
- [Where to run Dewey](#where-to-run-dewey): [A. Docker Compose](#a-one-server-with-docker-compose), [B. Fly.io](#b-flyio-with-managed-postgres), [C. Render](#c-render-with-neon-or-supabase), but not [Netlify](#why-not-netlify) or [Hugging Face Spaces](#why-hugging-face-spaces-is-a-poor-fit)
- [Settings reference](#settings-reference), [security checklist](#security-checklist), [troubleshooting](#troubleshooting)
- Connecting each AI tool: [MCP.md](MCP.md#client-recipes)

## What runs

Dewey has three [stacks](GLOSSARY.md#stack), chosen with `DEWEY_STACK`: `anthropic` ([Claude](GLOSSARY.md#anthropic-api)), `open` ([open-weight models](GLOSSARY.md#open-weight-model), not OpenAI) and `azure` (Microsoft's [Azure OpenAI](GLOSSARY.md#azure-openai) and [Azure AI Search](GLOSSARY.md#azure-ai-search), not OpenAI's own API).
[STACKS.md](STACKS.md#the-setups-at-a-glance) compares the setups built on them.

| Part | What it does | Runs |
|---|---|---|
| **MCP server** | Serves `search_archive` and `ask_archive` to AI tools over [Streamable HTTP](GLOSSARY.md#streamable-http), with a [bearer token](GLOSSARY.md#bearer-token) | all the time |
| **[Postgres](GLOSSARY.md#postgres) with [pgvector](GLOSSARY.md#pgvector)** | Stores articles, their [chunks](GLOSSARY.md#chunk) and [embeddings](GLOSSARY.md#embedding) (`azure` uses Azure AI Search) | all the time |
| **[Indexer](GLOSSARY.md#indexing)** | Splits, embeds and stores `data/*.json`, updating articles in place (`azure`: [AZURE.md](AZURE.md#build-the-search-index)) | when you add articles |
| **[Importers](GLOSSARY.md#importer)** (optional) | Turn a [CMS](GLOSSARY.md#cms) export or API into `data/*.json` | when you import |
| **Demo page** (optional) | A small Gradio chat page for trying Dewey | on your own computer only |

## What Dewey needs

- **A long-running process:** the MCP server keeps sessions in memory and streams replies (Server-Sent Events), so platforms that only run short functions per request can't host it.
- **A database:** on `anthropic` and `open`, **Postgres with pgvector 0.8 or newer** ([why](#managed-postgres-with-pgvector)), reachable on its [port](GLOSSARY.md#port) (normally 5432). On `azure`, Azure AI Search.
- **Outbound HTTPS** to your model providers ([Voyage AI](GLOSSARY.md#voyage-ai), Anthropic, Azure or [OpenRouter](GLOSSARY.md#openrouter)), or on `open`, a model server Dewey can reach, such as [Ollama](GLOSSARY.md#ollama).
- **Chat-model settings, even if you only use `search_archive`:** the server checks them at startup (a Claude credential, `OPEN_LLM_MODEL` or the Azure settings).
  It calls the [chat model](GLOSSARY.md#chat-model) only for `ask_archive` and the demo page.
- **Memory:** not measured on a server.
  Without local [search models](GLOSSARY.md#search-models), Dewey runs no models itself, so start small and watch for out-of-memory restarts.
  Local search models (`INSTALL_LOCAL_MODELS=true` in Docker) need several GB more ([STACKS.md](STACKS.md#claude-with-local-search-models)).

## Access control

Dewey has no accounts.
Four things decide who can use it:

1. **The MCP bearer token.**
   In HTTP mode the server won't start without `DEWEY_MCP_TOKEN`, and answers `401` to requests without it.
   Anyone who has this one shared token can search the whole archive and spend your model budget through `ask_archive`.
   Share it only with the apps and people who need it, and rotate it when someone leaves ([MCP.md](MCP.md#security)).
2. **TLS in front.**
   The server speaks plain HTTP, so beyond one machine, an HTTPS [reverse proxy](GLOSSARY.md#reverse-proxy) (Caddy, nginx, or Fly's or Render's edge) must encrypt the token and the archive text ([TLS](GLOSSARY.md#tls-and-https)).
3. **Network placement.**
   [Docker Compose](GLOSSARY.md#docker-compose) publishes the MCP port on the host's `127.0.0.1` only and never publishes Postgres.
   Chat apps in [Docker](GLOSSARY.md#docker) reach Dewey over a network without Postgres ([MCP.md](MCP.md#chat-apps-in-docker-the-dewey-mcp-clients-network)).
4. **The client app's own access control.**
   A chat app uses the shared token for all its users, so Dewey can't tell them apart: decide in the app who gets the Dewey tool ([Open WebUI, for example](MCP.md#open-webui)).

> [!WARNING]
> **Never expose Postgres to the internet.**
> It holds the whole archive, and only the database password protects it, not Dewey's token.
> With a managed database, use TLS and the provider's access restrictions.

In a container, the MCP server prints this at startup (never the token):

```text
dewey-mcp: listening on http://0.0.0.0:8765/mcp (bearer token required)
dewey-mcp: 0.0.0.0 is reachable from other machines, and this server speaks plain HTTP. Put it behind a TLS reverse proxy ...
```

The second line is expected there: what matters is that the host publishes the port only on `127.0.0.1`, or behind TLS.

**Every `ask_archive` call costs money on the hosted stacks, and Dewey sets no budget or rate limit** ([MCP.md](MCP.md#cost-and-privacy)).
`search_archive` also calls your embedding and reranking services: Voyage AI on `anthropic` by default.
Use [API keys](GLOSSARY.md#api-key) dedicated to Dewey so you can revoke them, and watch your providers' usage pages ([what each receives](STACKS.md#what-the-hosted-providers-receive)).

## The demo page

`python app/main.py` serves a small chat page at `http://127.0.0.1:7860` (or the next free port) for trying Dewey on your own computer ([how to use it](INSTALL.md#try-the-demo-page)).
These rules keep it private.

> [!WARNING]
> **The demo page has no login.**
> Anyone who can reach it can search the whole archive and ask questions on your API keys.
> Keep it on `127.0.0.1`, or behind a proxy that makes people sign in.

- **It refuses to start** (exit status 2) on any address but `127.0.0.1`, `localhost` or `::1` ([localhost](GLOSSARY.md#localhost)), such as `GRADIO_SERVER_NAME=0.0.0.0`, unless you set one of these on purpose:
  - `DEWEY_DEMO_BEHIND_AUTH_PROXY=true`: a proxy in front makes people sign in, and nothing else can reach the page's port.
    Dewey can't check this, or see who signed in.
  - `DEWEY_DEMO_PUBLISHED_ON_LOOPBACK=true`: the page runs in a container whose port Docker publishes on the host's `127.0.0.1` only. The Compose `demo` service sets it.
    **Don't set it yourself** anywhere else: on a machine's own network address, it puts the page on the network, because the `Host` check below stops only browsers.
- **It answers only requests addressed to `localhost` or `127.0.0.1`** (others get status 400), unless `DEWEY_DEMO_BEHIND_AUTH_PROXY=true`.
  This stops a web page you visit from pointing its name at 127.0.0.1 ([DNS rebinding](GLOSSARY.md#dns-rebinding)) to ask your Dewey questions on your API keys.
- **It answers only its own page:** a script on another web page (marked by its `Origin`) gets status 403, again unless `DEWEY_DEMO_BEHIND_AUTH_PROXY=true`.
  Otherwise a page could ask questions at `http://anything.localhost:7860`, which Gradio's own check misses, and read the answers.
- **Always off:** Gradio's usage telemetry, the "Use via API" page, the `/monitoring` pages (Dewey answers 404), Gradio's own MCP server and public share links.
- **Not off:** Gradio's theme loads a font from Google Fonts, so each browser that opens the page contacts Google (without the questions or answers).
- **Blocked:** a script (iframe-resizer) that Gradio's page loads from `cdnjs.cloudflare.com` with no integrity check; the browser console's "blocked" message is expected.

<details><summary>Details, checked in Gradio 6.16.0's source</summary>

Gradio's REST routes can't skip the queue, and the generic upload route accepts at most 1 MB (Dewey has no upload fields).
Gradio 6.16.0 serves `/monitoring/summary` (request counts and timings) even with monitoring off, hence Dewey's 404.
The font comes from `themes/default.py`, and the page template preconnects to `fonts.googleapis.com`.
The script, from `templates/frontend/index.html`, would run next to your questions and answers, so Dewey sends `Content-Security-Policy: script-src 'self' 'unsafe-inline'`; the demo is never embedded in another page, so it doesn't need the script.
Browsers send `*.localhost` to your own computer.

</details>

To give colleagues a chat page, don't publish the demo: connect a chat app they sign in to, such as Open WebUI, to the MCP server.

## Where to run Dewey

These options run the MCP server and the database.

| Platform | Fits Dewey? | Postgres with pgvector | Notes (checked November 2026) |
|---|---|---|---|
| Your own server (VM or VPS) with Docker Compose | **Yes**: [A](#a-one-server-with-docker-compose) | `pgvector/pgvector:pg18` in the same Compose file | You run the HTTPS reverse proxy (Caddy, nginx). Docker Engine is free |
| Fly.io | **Yes**: [B](#b-flyio-with-managed-postgres) | Fly Managed Postgres (MPG), with pgvector switched on per database | HTTPS at Fly's edge. MPG Basic $38 a month, storage $0.28 per GB-month (prices change), plus the app's machine |
| Render | **Yes, with a database elsewhere**: [C](#c-render-with-neon-or-supabase) | On Render Postgres: *unverified*. Use Neon or Supabase | HTTPS at Render's edge. Free web services spin down when idle and run one instance |
| Hugging Face Spaces | **Poor fit**: [why](#why-hugging-face-spaces-is-a-poor-fit) | Can't reach Postgres: outbound traffic only on ports 80, 443 and 8080 | Gradio and Docker Spaces need a paid plan |
| Netlify | **No**: [why](#why-not-netlify) | — | — |
| Railway, Google Cloud Run, AWS, Azure | Also possible; check their docs | *unverified* | *unverified* |

<details><summary>Sources</summary>

- Your own server: [pgvector README](https://github.com/pgvector/pgvector) and Docker's [install overview](https://github.com/docker/docs/blob/main/content/manuals/engine/install/_index.md), which says Docker Engine is free and open source (Apache License 2.0).
- Fly.io: [superfly/docs](https://github.com/superfly/docs).
- Render: [render-oss/skills](https://github.com/render-oss/skills).
- Hugging Face Spaces: [hub-docs](https://github.com/huggingface/hub-docs/tree/main/docs/hub).
- Netlify: [netlify/build](https://github.com/netlify/build/tree/main/packages/zip-it-and-ship-it/src/runtimes).

</details>

### Managed Postgres with pgvector

The indexer runs `CREATE EXTENSION IF NOT EXISTS vector` itself, so where the provider allows that, there's nothing to switch on by hand.

| Provider | Switching on pgvector | Free plan (checked November 2026) |
|---|---|---|
| Neon | Available on every plan ([Neon docs](https://raw.githubusercontent.com/neondatabase/website/main/content/docs/extensions/pgvector.md)) | 1 GB per project, 100 compute-hours a month, pgvector 0.8.7. Sleeps after 5 idle minutes and wakes on the next query ([Neon plans](https://github.com/neondatabase/website/blob/main/content/docs/introduction/plans.md)) |
| Supabase | Dashboard > Database > Extensions > enable "vector", or with SQL ([Supabase docs](https://raw.githubusercontent.com/supabase/supabase/master/apps/docs/content/guides/database/extensions/pgvector.mdx)) | 2 projects of 500 MB each. Paused after a week of little activity; you can restore it within a year ([Supabase billing](https://github.com/supabase/supabase/blob/master/apps/docs/content/guides/platform/billing-on-supabase.mdx)) |
| Self-hosted | Docker image `pgvector/pgvector:pg18`: PostgreSQL 18.7 with pgvector 0.8.7, the versions Dewey was tested with in November 2026 ([pgvector README](https://github.com/pgvector/pgvector#docker)) | — |

**How much fits:** at least about 55 MB per 1,000 articles of about 1,300 words (a calculation, not a measurement; the real size is larger).
Check the plan's limit before you index a large archive.

**Check the pgvector version.**
Before 0.8, narrow date and byline searches return fewer articles, without any error; Ubuntu 24.04's own package is 0.6.0.
Supabase builds pgvector up to 0.8.2, but a project may run another version ([Supabase's build list](https://github.com/supabase/postgres/blob/develop/nix/ext/versions.json)).
Run this in the provider's SQL editor, not in a terminal:

```sql
SELECT extversion FROM pg_extension WHERE extname = 'vector';
```

**Use the direct connection string, not the pooled one.**
Through a connection pooler (PgBouncer) in transaction mode, Dewey's per-connection search setting (`SET hnsw.iterative_scan`) can be lost, so narrow date or byline searches may return fewer articles.
The indexer's repeated inserts may also fail with errors about prepared statements.
Whether a given pooler runs in transaction mode is *unverified*, so use the direct string for both the MCP server and `setup_postgres.py`.

## A. One server with Docker Compose

One Linux server runs everything in Docker, and AI tools on other machines connect over HTTPS through a reverse proxy on it.

**You'll need:**

- **Docker Engine 28.0 or newer** and its Compose plugin, from Docker's own repository ([why](#why-docker-engine-280-or-newer)).
- [git](GLOSSARY.md#git): `sudo apt install git` on Debian and Ubuntu, `sudo dnf install git` on Fedora.
- Python 3.11 or newer for the importers (check with `python3 --version`).
- A domain name pointing at the server, if AI tools on other machines will connect.
- Only ports 80 and 443 open to the internet. Ports **8765, 7860 and 5432 must not be**.

### Why Docker Engine 28.0 or newer

> [!WARNING]
> **On Docker older than 28.0, machines on your local network can reach ports published on `127.0.0.1`** ([Docker's port publishing guide](https://github.com/docker/docs/blob/main/content/manuals/engine/network/port-publishing.md), [moby/moby#45610](https://github.com/moby/moby/issues/45610)).
> A machine next to your server could then open the demo page, which has no login, or reach the MCP server over plain HTTP, where the token travels unencrypted.

**Install Docker Engine from Docker's own repository** ([Ubuntu](https://github.com/docker/docs/blob/main/content/manuals/engine/install/ubuntu.md), [Debian](https://github.com/docker/docs/blob/main/content/manuals/engine/install/debian.md), [Fedora](https://github.com/docker/docs/blob/main/content/manuals/engine/install/fedora.md); their steps include the Compose plugin).
Don't use Debian's own `docker.io` package: on Debian 13 it is Docker 26.1.5 (checked November 2026).
[INSTALL.md](INSTALL.md#get-your-linux-computer-ready) walks through the same install on a Linux desktop.

**Decide how to run `docker`.**
Each `docker` command needs `sudo` in front, unless you add yourself to the `docker` group, then log out and back in:

```bash
sudo usermod -aG docker $USER
```

Docker warns that "The `docker` group grants root-level privileges to the user" ([post-installation steps](https://github.com/docker/docs/blob/main/content/manuals/engine/install/linux-postinstall.md)).
The commands on this page leave out `sudo`.

**Check the version:** in the `Server:` part of the output, the Engine's `Version:` must be 28.0 or higher.

```bash
docker version
```

### One Dewey per server

The `dewey-mcp-clients` network has a fixed name, so a second copy of Dewey's Compose project on the same machine would share its alias, `dewey-mcp`, and port 8765.
A chat app could then reach the wrong archive: with a shared alias, "exactly which container the name resolves to is not guaranteed" ([Compose specification](https://github.com/compose-spec/compose-spec/blob/main/spec.md#aliases)).
To run two, give the second its own network name, alias and host port in its `docker-compose.yml`.

### What the Compose file runs

| Service | Starts with | What it is |
|---|---|---|
| `dewey` | `docker compose up -d` | The MCP server, published on `127.0.0.1:8765`. Also runs one-off commands such as the indexer (`docker compose run --rm dewey ...`) |
| `db` | `docker compose up -d` | Postgres with pgvector (`pgvector/pgvector:pg18`), keeping its data in the `pgdata` volume; never published |
| `demo` | `docker compose --profile demo up -d` | The demo page, published on `127.0.0.1:7860` only |
| `ollama` | `docker compose --profile ollama up -d` | An Ollama model server for `DEWEY_STACK=open`; never published |

`--profile` turns on an optional service.
The `db` service is separate from the `dewey-db` container that the [setup wizard](GLOSSARY.md#setup-wizard) can start on your own computer: an index built in one isn't in the other.

> [!WARNING]
> **The `ollama` service needs one more setting, or its answers ignore the question.**
> With Ollama's default [context window](GLOSSARY.md#context-window) of 4,096 tokens, it silently drops the middle of Dewey's answer prompt (about 7,100 tokens): the instructions, the question and the best sources.
> The Ollama part of [Steps for one server](#steps-for-one-server) raises the context to 32,768.

### Steps for one server

Run these in a [terminal](GLOSSARY.md#terminal) on the server, usually over SSH.
Dewey runs inside Docker, so you don't need a virtual environment there.

1. **Get the code.**
   In your home folder on the server, run this (or clone your newsroom's own [fork](GLOSSARY.md#fork) of Dewey):

   ```bash
   git clone https://github.com/TeamTrace/dewey-ai.git
   ```

   Then go into the folder (`/home/you/dewey-ai`), where every later command runs:

   ```bash
   cd dewey-ai
   ```

2. **Make a database password and a token.**
   Run each command and copy what it prints.
   The first makes the database password, letters and digits only because it goes inside a URL:

   ```bash
   openssl rand -hex 24
   ```

   The second makes the token your AI tools will send; keep a copy in a password manager:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

3. **Create `.env` and fill it in.**
   Copy the template, and make the copy readable only by you:

   ```bash
   cp .env.template .env
   chmod 600 .env
   ```

   Open the [`.env` file](GLOSSARY.md#env-file) in a text editor on the server, for example nano:

   ```bash
   nano .env
   ```

   First set `DEWEY_STACK` and its keys for your [setup](GLOSSARY.md#setup) ([STACKS.md](STACKS.md#configuring-by-hand)).
   Then make sure the file has these lines, with your own values.
   Change lines that are already there rather than adding copies.
   In the template, these lines start with `#`, which makes Dewey and Compose ignore them: delete the `#` (edit the file; don't paste this into the terminal):

   ```dotenv
   NEWSROOM_NAME='Your Newsroom'
   ARCHIVE_START_DATE='January 1, 2015'
   DEFAULT_REGION=''
   POSTGRES_PASSWORD='PASTE_THE_PASSWORD'
   DEWEY_MCP_TOKEN='PASTE_THE_TOKEN'
   ```

   With local search models, add this line too, which makes a much bigger image (CPU-only PyTorch and the models' packages):

   ```dotenv
   INSTALL_LOCAL_MODELS='true'
   ```

   The models themselves download from Hugging Face on first use, into the `hf-cache` volume, so the first index run and the first question take longer.

   In nano, save with Control+O and Enter, then quit with Control+X.

   - `DEFAULT_REGION=''` means no region, as for a national outlet; a regional newsroom writes its region.
   - Compose ignores `DATABASE_URL` in `.env` and points Dewey at its own `db` service.
   - Compose reads `.env` [its own way](https://github.com/compose-spec/compose-spec/blob/main/spec.md): only single-quoted values, as `install.py` writes them, are literal. So avoid `$` outside single quotes, and backslashes.

> [!WARNING]
> **Set the three newsroom settings before you index.**
> Without them, Dewey uses its original newsroom's defaults: The Philadelphia Inquirer, articles from January 2, 1978, and the Greater Philadelphia Region.
> Every article without a [publisher](GLOSSARY.md#publisher) is then labelled as the Inquirer's, and the MCP server tells every AI tool it's the Inquirer's archive.
> To fix it later, correct `.env`, restart the server (step 4) and index again.

4. **Start the MCP server and Postgres.**
   The first time, this builds Dewey's image, so it takes a while:

   ```bash
   docker compose up -d --build
   ```

   Then watch the server's log:

   ```bash
   docker compose logs -f dewey
   ```

   You should see something like:

   ```text
   dewey-mcp: listening on http://0.0.0.0:8765/mcp (bearer token required)
   ```

   Press Control+C to stop watching; the server keeps running.

5. **Import your articles.**
   Dewey indexes the JSON files in `data/` ([format](IMPORTING.md#article-file-format)), which an importer can write from your CMS.
   Importers need only Python, so they run on the server without Docker:

   ```bash
   python3 app/import_archive.py --help
   ```

   [IMPORTING.md](IMPORTING.md#run-an-import) explains each importer and a 20-article trial run.
   Its commands start with `python`; on the server, with no virtual environment, type `python3` instead.
   You can also import on your own computer and copy the files into `data/` on the server.

6. **Index them**, in a temporary container from the server's image that publishes no ports:

   ```bash
   docker compose run --rm -v ./data:/app/data:ro dewey python app/setup_postgres.py
   ```

   You should see something like:

   ```text
   🎉 Indexed 41 article(s) as 188 chunk(s).
   ```

   Re-run it whenever you add articles: it updates articles in place and never deletes any.
   Each run embeds every article again, which takes time with local models and costs money with Voyage AI ([IMPORTING.md](IMPORTING.md#index-your-articles)).

7. **Connect an AI tool and test it.**
   On the server itself, tools use `http://127.0.0.1:8765/mcp` and send the header `Authorization: Bearer YOUR_TOKEN`, with your `DEWEY_MCP_TOKEN` as YOUR_TOKEN.
   Chat apps in Docker join the `dewey-mcp-clients` network and use `http://dewey-mcp:8765/mcp`.
   [MCP.md](MCP.md#client-recipes) has a recipe for each tool, and a [checklist to test it](MCP.md#test-that-it-works).

8. **For AI tools on other machines, put HTTPS in front.**
   Run a reverse proxy, such as Caddy or nginx, on the same server. It must:

   - hold a real TLS certificate for your domain;
   - forward to `127.0.0.1:8765`, the only place Compose publishes the MCP server;
   - not buffer streaming responses (Server-Sent Events), and allow long requests: `ask_archive` can take a minute or more.

   For example, a Caddyfile along these lines (*not tested by us; check Caddy's documentation*):

   ```text
   dewey.example.org {
       reverse_proxy 127.0.0.1:8765
   }
   ```

   AI tools then use `https://dewey.example.org/mcp`, with no extra flags on the server.
   Optionally, add `"--public-url", "https://dewey.example.org/mcp"` to the `dewey` service's `command`, so `401` responses name the public address ([details](MCP.md#host-names-and-dns-rebinding-protection)).

**Optional: an open-weight model in a container** (`DEWEY_STACK=open`).
In `.env`, set the model server's address and a model, such as `qwen3:8b` ([choosing a model](MODELS.md#a-self-hosted-with-ollama)):

```dotenv
OPEN_LLM_BASE_URL='http://ollama:11434/v1'
OPEN_LLM_MODEL='qwen3:8b'
```

Then raise Ollama's context length to 32,768 tokens ([why](#what-the-compose-file-runs)).
This command creates `compose.override.yaml`, which `git pull` leaves alone and Compose reads together with `docker-compose.yml` ([merge guide](https://github.com/docker/docs/blob/main/content/manuals/compose/how-tos/multiple-compose-files/merge.md); we checked it with Docker Compose 5.8.0, November 2026):

```bash
printf 'services:\n  ollama:\n    environment:\n      OLLAMA_CONTEXT_LENGTH: "32768"\n' > compose.override.yaml
```

The file then holds these lines:

```yaml
services:
  ollama:
    environment:
      OLLAMA_CONTEXT_LENGTH: "32768"
```

With little memory, 16384 is the smallest value that leaves room for an answer (an estimate).
Write the number in digits, because Ollama ignores `32k`.
Check the files; this prints nothing when they're valid:

```bash
docker compose --profile ollama config --quiet
```

Start Ollama, download the model (qwen3:8b is about 5.2 GB), then restart the MCP server so it reads the new `.env`:

```bash
docker compose --profile ollama up -d
```

```bash
docker compose exec ollama ollama pull qwen3:8b
```

```bash
docker compose up -d
```

Within five minutes of asking Dewey a question, while the model is still loaded, the CONTEXT column here should say 32768:

```bash
docker compose exec ollama ollama ps
```

No output from this means Ollama hasn't cut any prompts (*not tested by us*):

```bash
docker compose logs ollama | grep 'truncating input prompt'
```

The container runs on the CPU, at a speed nobody has measured.
qwen3:8b takes about 4.9 GiB of memory, plus about 4.5 GiB for a 32,768-token context (calculated, not measured; Ollama's working buffers come on top).

<details><summary>With a GPU</summary>

The `ollama` service asks Docker for no GPU.
For an NVIDIA [GPU](GLOSSARY.md#gpu), see [Ollama's Docker guide](https://raw.githubusercontent.com/ollama/ollama/main/docs/docker.mdx), the source of the `ollama/ollama` image, its `/root/.ollama` volume and port 11434.
On a Mac, Docker can't give Ollama the GPU at all ([Ollama FAQ](https://github.com/ollama/ollama/blob/main/docs/faq.mdx)).

</details>

**Optional: the demo page.**
This builds and starts its container, published on the server's `127.0.0.1:7860` only; don't publish it further, because it has no login ([the demo page](#the-demo-page)):

```bash
docker compose --profile demo up -d --build
```

Its log shows something like:

```text
Dewey: Listening on 0.0.0.0:7860 (reachable from other machines).
Dewey: Demo page, no login (DEWEY_DEMO_PUBLISHED_ON_LOOPBACK=true): Docker must publish this port on the host's 127.0.0.1 only.
...
* Running on local URL:  http://0.0.0.0:7860
* To create a public link, set `share=True` in `launch()`.
```

Open http://127.0.0.1:7860, not the `Running on local URL` address: the page refuses any other address with status 400.
Ignore the `share=True` hint; Dewey keeps share links off on purpose.
From your own computer, an SSH tunnel keeps the page private (*not tested by us*).
Run this there with your login and the server's address (it works in PowerShell too), then open the same address while it stays connected:

```bash
ssh -L 7860:127.0.0.1:7860 YOUR_LOGIN@YOUR_SERVER
```

**Keep Dewey up to date.**
Get the new code, then rebuild and restart; add `--profile demo` to the second command if you run the demo page:

```bash
git pull
```

```bash
docker compose up -d --build
```

Restarting ends open MCP sessions, and clients reconnect (how gracefully differs per client, *unverified*).
Run the indexer again (step 6) if Dewey says the database "was set up by an earlier version of Dewey", or when the update notes ask for it.

**Back up the database** regularly, and keep copies off the server.
This writes a dated file in your home folder:

```bash
docker compose exec -T db pg_dump -U dewey -Fc dewey > ~/dewey-$(date +%F).dump
```

To restore into an empty database, such as a new server after step 4, replace DATE with the date in the dump's name, for example `2026-11-18`:

```bash
docker compose exec -T db pg_restore -U dewey -d dewey < ~/dewey-DATE.dump
```

The indexer can rebuild the index from `data/*.json`, so backups mainly save re-embedding time and cost.
But re-importing a feed can drop older stories from `data/`, leaving the database as Dewey's only copy ([IMPORTING.md](IMPORTING.md#run-it-again-later)).
Keep `data/` and `.env`, which holds secrets, somewhere safe too.

## B. Fly.io with Managed Postgres

Fly runs Dewey's Docker image, whose default command is the MCP server, with HTTPS at Fly's edge; Fly Managed Postgres (MPG) supports pgvector.

**You'll need:** a Fly.io account; `flyctl`, Fly's command-line tool; and Dewey on your own computer, with the same setup the server will run ([INSTALL.md](INSTALL.md#before-you-start)).
Run the commands below on your computer, in the Dewey folder.

> [!WARNING]
> **Set the same newsroom settings in `fly.toml` as in your `.env`.**
> The server won't start without `NEWSROOM_NAME`. Without `ARCHIVE_START_DATE` it states no start date, and without `DEFAULT_REGION` it assumes no region.
> Your computer's indexer labels articles without a `publisher` with your `.env`'s `NEWSROOM_NAME`, so if the names differ, answers present your own reporting as another outlet's.

1. **Install `flyctl` and log in**, following [Fly's docs](https://github.com/superfly/docs).

2. **Create the app without deploying.**

   ```bash
   fly launch --no-deploy
   ```

   It writes a `fly.toml`: replace its contents with `deploy/fly.toml.example`, but keep the `app` name and `primary_region` that `fly launch` chose.
   Then set the newsroom settings in its `[env]` section:

   ```toml
   [env]
     DEWEY_STACK = "anthropic"
     NEWSROOM_NAME = "Your Newsroom"
     ARCHIVE_START_DATE = "January 1, 2015"
     DEFAULT_REGION = ""
   ```

   Whether Fly passes the empty `DEFAULT_REGION` on to Dewey is *unverified*.
   With default search models, the example's `anthropic` stack is the [Claude with Voyage AI](STACKS.md#claude-with-voyage-ai) setup.
   For another, change `DEWEY_STACK` here and add your `.env`'s other settings that aren't secrets, such as `EMBEDDING_PROVIDER` and `RERANK_PROVIDER` with local search models (see also the notes below).
   Then change the keys in step 5.

3. **Create the database** in the same region, where YOUR_REGION is the `primary_region` in `fly.toml`:

   ```bash
   fly mpg create --name dewey-db --region YOUR_REGION
   ```

   Then, on the cluster's **Extensions** page, pick the database and switch on `vector` ([Fly docs](https://github.com/superfly/docs/blob/main/postgres/extensions.mdx)).
   Plans and prices: [Fly MPG](https://github.com/superfly/docs/blob/main/postgres/index.mdx) (Basic: $38 a month, checked November 2026).

4. **Connect the app to the database.**
   `fly mpg attach` would set the **pooled** (PgBouncer) URL ([pooled vs direct](#managed-postgres-with-pgvector)), so copy the **direct** connection string from the cluster's Connection tab, and set it yourself:

   ```bash
   fly secrets set DATABASE_URL='PASTE_THE_DIRECT_CONNECTION_STRING'
   ```

   This leaves the connection string, password included, in your [shell](GLOSSARY.md#shell) history.

5. **Set the other secrets.**
   Setting a secret restarts the app's machines.
   For Claude with Voyage AI, set these two keys; with local search models, leave out `VOYAGE_API_KEY`:

   ```bash
   fly secrets set ANTHROPIC_API_KEY='YOUR_ANTHROPIC_KEY' VOYAGE_API_KEY='YOUR_VOYAGE_KEY'
   ```

   Then make the token, with the virtual environment on, and set it:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   ```bash
   fly secrets set DEWEY_MCP_TOKEN='PASTE_THE_TOKEN'
   ```

   Keep the token in your password manager: `fly secrets list` shows only names, and your AI tools need it.
   Typed keys end up in your shell history; `fly secrets set` can also read from standard input (see `fly secrets --help`).

6. **Deploy.**

   ```bash
   fly deploy
   ```

   AI tools then connect to `https://APP_NAME.fly.dev/mcp` with the token, where APP_NAME is the `app` in `fly.toml`.
   The `curl` checks in [MCP.md](MCP.md#test-that-it-works) should give 401 without the token and 200 with it.

7. **Index your articles from your computer.**
   MPG isn't reachable from the internet, so in one terminal window, open a tunnel and leave it running; it forwards `localhost:16380` to the database:

   ```bash
   fly mpg proxy
   ```

   In a second window, go to the Dewey folder and turn on its virtual environment ([how](INSTALL.md#turn-it-on-in-every-new-terminal-window)).
   The tunnel's address is the direct connection string with its host (between `@` and the next `/`) replaced by `localhost:16380`, like `postgres://USER:PASSWORD@localhost:16380/DBNAME`.

   To keep it out of your shell history, give it to this window only.
   Paste the line below and press Enter, then paste the address and press Enter again.
   Nothing appears on screen, which is expected.

   On macOS and Linux:

   ```bash
   read -rs DATABASE_URL && export DATABASE_URL
   ```

   On Windows, in [PowerShell](GLOSSARY.md#powershell), which shows asterisks as you paste (*not tested by us*):

   ```powershell
   $env:DATABASE_URL = [System.Net.NetworkCredential]::new('', (Read-Host -AsSecureString 'DATABASE_URL')).Password
   ```

   Then run the indexer; your `.env` must use the app's embedding settings, or Dewey refuses to search the index:

   ```bash
   python app/setup_postgres.py
   ```

   You should see something like:

   ```text
   🎉 Indexed 41 article(s) as 188 chunk(s).
   ```

   Afterwards, forget the address, so this window uses your `.env` again.
   On macOS and Linux:

   ```bash
   unset DATABASE_URL
   ```

   On Windows, in PowerShell:

   ```powershell
   Remove-Item -Path Env:\DATABASE_URL
   ```

**Notes:**

- With `auto_stop_machines = "stop"`, an idle app stops: the next request waits for it to start, and MCP sessions end. `"off"` keeps it running.
- Sessions live in one server's memory, so run a single Machine (`fly scale count`).
  Whether `fly launch` creates more than one by default is *unverified*, so check how many your app has.
- **Memory:** the example's `memory = "1gb"` is a guess for the `anthropic` and `azure` stacks without local models.
  With local search models, set `INSTALL_LOCAL_MODELS = "true"` under `[build.args]` and raise the memory.
- Unmanaged "Fly Postgres" is archived and unsupported, so use MPG.

<details><summary>Optional: a public URL in 401 responses</summary>

To pass `--public-url https://APP_NAME.fly.dev/mcp`, override the image's command in `fly.toml` (`cmd` "overrides the `CMD` set by the Dockerfile"):

```toml
[experimental]
  cmd = ["python", "app/mcp_server.py", "--http", "--host", "0.0.0.0", "--port", "8765", "--public-url", "https://APP_NAME.fly.dev/mcp"]
```

</details>

<details><summary>Sources</summary>

Fly facts are from [superfly/docs](https://github.com/superfly/docs), checked November 2026: `reference/configuration.mdx`, `apps/secrets.mdx` and `postgres/*.mdx`.
The example's health check is TCP, because Fly's HTTP checks expect a 2xx and the MCP endpoint answers 401 without the token.

</details>

## C. Render with Neon or Supabase

Render runs Dewey's Docker image as a web service, with HTTPS at its edge.
We couldn't confirm from a Render-owned source that Render Postgres offers pgvector, so the database stays at Neon or Supabase.
The example Blueprint in step 3 runs the [Claude with Voyage AI](STACKS.md#claude-with-voyage-ai) setup; local search models would also need `EMBEDDING_PROVIDER`, `RERANK_PROVIDER` and an image built with `INSTALL_LOCAL_MODELS=true`, which it doesn't set up.

**You'll need:** Render and Neon or Supabase accounts; Dewey on your own computer, with the same setup the server will run ([INSTALL.md](INSTALL.md#before-you-start)); and your own copy of Dewey in a [repository](GLOSSARY.md#repository) Render can deploy from, such as a fork on GitHub.

> [!WARNING]
> **Set the same newsroom settings in Render as in your `.env`.**
> The server won't start without `NEWSROOM_NAME`. Without `ARCHIVE_START_DATE` it states no start date, and without `DEFAULT_REGION` it assumes no region.
> Your computer's indexer labels articles without a `publisher` with your `.env`'s `NEWSROOM_NAME`, so if the names differ, answers present your own reporting as another outlet's.

1. **Create the database** on Neon or Supabase, and switch on pgvector ([how](#managed-postgres-with-pgvector)).
   Copy its **direct** connection string, and make sure it uses TLS (for example, it ends in `?sslmode=require`).

2. **Index from your computer**, as in [Fly's step 7](#b-flyio-with-managed-postgres) but without a tunnel.
   In the Dewey folder, with its virtual environment on, paste the line below and press Enter, then paste the connection string and press Enter again (nothing appears on screen).

   On macOS and Linux:

   ```bash
   read -rs DATABASE_URL && export DATABASE_URL
   ```

   On Windows, in PowerShell (*not tested by us*):

   ```powershell
   $env:DATABASE_URL = [System.Net.NetworkCredential]::new('', (Read-Host -AsSecureString 'DATABASE_URL')).Password
   ```

   Then run the indexer:

   ```bash
   python app/setup_postgres.py
   ```

   Afterwards, forget the connection string, so this window uses your `.env` again.
   On macOS and Linux:

   ```bash
   unset DATABASE_URL
   ```

   On Windows, in PowerShell:

   ```powershell
   Remove-Item -Path Env:\DATABASE_URL
   ```

3. **Add the Blueprint**, Render's name for a `render.yaml` file that describes your services.
   Copy the example to the top of your repository:

   ```bash
   cp deploy/render.yaml.example render.yaml
   ```

   In it, change `Your Newsroom` to your newsroom's name (the server won't start without `NEWSROOM_NAME`).
   Add the other newsroom settings at the end of its `envVars` list, indented like the other `- key:` lines:

   ```yaml
         - key: ARCHIVE_START_DATE
           value: January 1, 2015
         - key: DEFAULT_REGION
           value: ""
   ```

   Whether Render keeps an empty value is *unverified*.
   Then commit and push the file:

   ```bash
   git add render.yaml
   git commit -m "Add Render Blueprint"
   git push
   ```

4. **Create the Blueprint in Render's Dashboard** from your repository (see Render's docs for the exact steps).
   Render asks once for the `sync: false` secrets: `DEWEY_MCP_TOKEN`, `DATABASE_URL` (the direct string), `ANTHROPIC_API_KEY` and `VOYAGE_API_KEY`.
   Later, change them in the service's [environment variables](GLOSSARY.md#environment-variable).
   Make the token on your computer, with the virtual environment on:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

5. **Check the port.**
   Render sends public traffic to the port in its `PORT` variable ("default is often 10000"; see the service's Settings) and needs the app on `0.0.0.0`.
   The Blueprint's `dockerCommand` listens on `0.0.0.0`, port 10000; if Render's `PORT` is different, change its `--port` to match.

6. **Connect.**
   AI tools connect to `https://SERVICE_NAME.onrender.com/mcp` with the token, where SERVICE_NAME is your service's name on Render.
   Check it with the `curl` commands in [MCP.md](MCP.md#test-that-it-works).

Free web services spin down after a while without traffic: the next request waits for a cold start, and open MCP sessions end.
The example's `plan: starter` comes from Render's own examples, so check the current plans.

<details><summary>Sources</summary>

Render facts are from [render-oss/skills](https://github.com/render-oss/skills), checked November 2026: `render-web-services` (including `references/deployments.md`), `render-blueprints` (including `references/field-reference.md`) and `render-postgres`.
The Blueprint sets no `healthCheckPath`, so Render checks the service with a TCP connection: an HTTP check needs a 2xx or 3xx answer, which the MCP endpoint doesn't give without the token.
Free web services run a single instance with no persistent disk, which Dewey doesn't need.

</details>

## Why not Netlify

Dewey can't run on Netlify.
Netlify runs code only as functions, once per request on AWS Lambda, and its function bundler supports Node.js, Go and Rust, but not Python.
Dewey's MCP server is a long-running Python server, so Netlify could host only a static page, such as one that explains how to connect to wherever Dewey runs.

<details><summary>Sources</summary>

- [Runtimes in netlify/build](https://github.com/netlify/build/tree/main/packages/zip-it-and-ship-it/src/runtimes) and the [zip-it-and-ship-it README](https://raw.githubusercontent.com/netlify/build/main/packages/zip-it-and-ship-it/README.md): Node.js, Go and Rust runtimes, and function archives that target AWS Lambda.
- [netlify/cli function templates](https://github.com/netlify/cli/tree/main/functions-templates): JavaScript and TypeScript only.

</details>

## Why Hugging Face Spaces is a poor fit

From the [Hugging Face](GLOSSARY.md#hugging-face) [Spaces docs](https://github.com/huggingface/hub-docs/tree/main/docs/hub) (`spaces-overview.md`, `spaces-config-reference.md` and `spaces-sdks-docker.md`; checked November 2026):

- Creating a Gradio or Docker Space requires a paid plan (PRO, Team or Enterprise).
- Outbound requests are allowed only on ports 80, 443 and 8080, so a Space can't reach a normal Postgres on port 5432.
  That rules out the `anthropic` and `open` stacks, unless your database listens on one of those ports.
- Free CPU hardware sleeps when unused, and its disk isn't persistent: the persistent storage feature "is no longer available".

If you use a Space anyway, for example with the `azure` stack, which only makes HTTPS calls, make it **private** and store keys as Space secrets.
Don't use "protected": it keeps the running app publicly reachable through its embed URL.
Whether MCP clients can reach a private Space at all is *unverified*, so we have no recipe for one.

<details><summary>Space settings</summary>

Set `python_version` to 3.11, 3.12 or 3.13 (the default is 3.10); Space secrets arrive as environment variables.
Docker Spaces serve `app_port` (7860 by default), while Dewey's image serves the MCP server on 8765.

</details>

## Settings reference

Set these in `.env`, the Compose file, `fly secrets` or `fly.toml` `[env]`, or the Render Dashboard.

**Engine**

| Setting | Default | What it does |
|---|---|---|
| `DEWEY_MCP_TOKEN` | required for HTTP | Bearer token clients send: at least 32 letters, digits or `- . _ ~ + /` (and `=` at the end) |
| `POSTGRES_PASSWORD` | required | Compose only: the bundled Postgres's password, letters and digits |
| `INSTALL_LOCAL_MODELS` | `false` | Docker build argument for CPU-only [PyTorch](GLOSSARY.md#pytorch) and the local search models. With Compose, set it in `.env` so rebuilds keep them |
| `NEWSROOM_NAME` | required ([why](#steps-for-one-server)) | Your newsroom's name; the same on the server and the indexing computer |
| `ARCHIVE_START_DATE`, `DEFAULT_REGION` | empty | Your earliest article date and region. Empty: Dewey states no start date and assumes no region |
| `DATABASE_URL`, `DEWEY_STACK`, API keys | [STACKS.md](STACKS.md#configuring-by-hand) | The same embedding settings on the server and the indexer |

Server options (`python app/mcp_server.py --help`): `--http` (Streamable HTTP, not [stdio](GLOSSARY.md#stdio)), `--host` (default `127.0.0.1`; `0.0.0.0` in the Docker image), `--port` (default 8765), `--public-url` and `--no-auth` (loopback testing only; [details](MCP.md#before-you-start-http)).

**Demo page** (`app/main.py`; rules in [the demo page](#the-demo-page))

| Setting | Default | What it does |
|---|---|---|
| `GRADIO_SERVER_NAME` | `127.0.0.1` | Address to listen on. Any but 127.0.0.1, `localhost` or `::1` needs a flag below |
| `GRADIO_SERVER_PORT` | `PORT`, else the first free port from 7860 | Port to listen on; the Compose `demo` service pins 7860 |
| `PORT` | — | Used when `GRADIO_SERVER_PORT` is unset; Render and similar platforms set it |
| `DEWEY_DEMO_BEHIND_AUTH_PROXY` | `false` | `true`: a sign-in proxy is in front, and nothing else can reach the port. Allows a network address and any `Host` header |
| `DEWEY_DEMO_PUBLISHED_ON_LOOPBACK` | `false` | `true`: in a container published on the host's `127.0.0.1` only. Allows `0.0.0.0` inside it; the `Host` check stays |
| `DEWEY_CONCURRENCY` | `4` | Questions answered at once (1-100; Gradio's default is 1). More means more parallel API calls and, with local models, memory |
| `GRADIO_ROOT_PATH` | — | Serves the demo under a sub-path behind a proxy, such as `/dewey` (a Gradio setting) |

## Security checklist

- [ ] **`DEWEY_MCP_TOKEN`** is random, in a secret store, shared only as needed, and rotated when someone leaves or a config file leaks ([MCP.md](MCP.md#security)).
- [ ] **TLS** is in front beyond one machine; port 8765 isn't open to the network.
- [ ] **Docker Engine 28.0 or newer** on a Compose server ([why](#why-docker-engine-280-or-newer)).
- [ ] **Postgres is never public**; a managed one uses TLS and the provider's access restrictions (*unverified per provider; check their docs*).
- [ ] **The demo page stays on localhost**, or behind a sign-in proxy.
- [ ] **Chat apps limit who gets the Dewey tool.**
- [ ] **Secrets stay in a secret store** (`fly secrets`, Render's `sync: false` variables, a `chmod 600` `.env`), never in git, `fly.toml` `[env]` or `render.yaml` values; git and Docker keep ignoring `.env`.
- [ ] **Agents that also have risky tools** (email, files, shell) are reviewed: archive text is untrusted ([prompt injection](GLOSSARY.md#prompt-injection); [MCP.md](MCP.md#security)).
- [ ] **Backups** run with `pg_dump` on a schedule, are kept off the server with `data/*.json`, and a restore has been tested.
- [ ] **Logs are newsroom-confidential.** On `azure`, the error log records each search's filter, with bylines and dates from the question (`anthropic` and `open` no longer log filters). On every stack, it can hold text derived from a question: a date the model wrote that couldn't be parsed, the `open` stack's search plan when it fails validation, and tracebacks.
- [ ] **Updates are merged and rebuilt.** `requirements.txt` pins every Python package, so a rebuild alone updates none of them, and Docker reuses the base image (`python:3.12-slim`) it already has unless you add `--pull` ([Docker's build best practices](https://github.com/docker/docs/blob/main/content/manuals/build/building/best-practices.md)). Merge the Dependabot pull requests (`.github/dependabot.yml`; turn on "Dependabot security updates" in your fork's settings), then rebuild: `docker compose build --pull` and `docker compose up -d` (*not tested by us*), or `fly deploy`.

## Troubleshooting

For `401`, `421 Invalid Host header` or a chat app in Docker that can't connect, see [MCP.md](MCP.md#troubleshooting).

| You see | What it means and what to do |
|---|---|
| `required variable DEWEY_MCP_TOKEN is missing a value` (or `POSTGRES_PASSWORD`), from Docker Compose | Set it in `.env` ([step 3](#steps-for-one-server)) |
| `dewey-mcp: refusing to start: ...` | The token is missing, too short or has characters a header can't carry; the message says how to make one |
| `permission denied while trying to connect to the docker API at unix:///var/run/docker.sock` (Docker 28 and older: `... the Docker daemon socket ...`) | You're not in the `docker` group yet, or haven't logged in again since joining. Use `sudo`, or see [the group step](#why-docker-engine-280-or-newer) |
| `Refusing to start: GRADIO_SERVER_NAME=0.0.0.0 lets other machines reach the demo page` | The demo has no login: unset `GRADIO_SERVER_NAME`, or see [the demo page](#the-demo-page) |
| Status 400, "Dewey's demo page has no login, so it only answers requests addressed to localhost..." | Open `http://127.0.0.1:7860/`, not another name such as the Compose log's `http://0.0.0.0:7860`. Behind a sign-in proxy, set `DEWEY_DEMO_BEHIND_AUTH_PROXY=true` |
| Answers from the `ollama` service ignore the question | Ollama still reads only 4,096 tokens: add `compose.override.yaml` ([steps](#steps-for-one-server)), then check CONTEXT in `docker compose exec ollama ollama ps` |
| Answers call your own articles another outlet's, or name The Philadelphia Inquirer | `NEWSROOM_NAME` differs between the server and the indexing computer, or the articles were indexed by an earlier version with no name set: set it the same on both, then index again |
| The server stops with `NEWSROOM_NAME is not set` | Add `NEWSROOM_NAME` to the server's settings (`.env`, `fly.toml` or `render.yaml`) and start it again |
| `Embedding settings don't match this database` | Use the same `DEWEY_STACK`, `EMBEDDING_PROVIDER`, `EMBED_MODEL` and `EMBED_DIM` for the server and the indexer ([STACKS.md](STACKS.md#indexes-are-tied-to-one-embedding-model)) |
| Render deploy never becomes healthy | `--port` in `dockerCommand` doesn't match Render's `PORT` |
| Fly health checks fail | `internal_port` and the check's `port` must both be 8765, the image's port |
| Clients lose their connection after a quiet period | The platform stopped the idle server (Fly auto-stop, Render free tier): reconnect, or keep it running |
| Narrow date or byline searches return too few articles, or indexing fails with prepared-statement errors | Use the direct connection string, not the pooled one, and check that pgvector is 0.8 or newer ([how](#managed-postgres-with-pgvector)) |

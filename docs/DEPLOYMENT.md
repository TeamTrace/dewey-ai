# Deploying Dewey

This guide is for a newsroom data team that runs Dewey as an **archive engine** for colleagues: the
archive is indexed once, and people reach it from AI tools they already use (agent harnesses such as
Hermes Agent, DeepSeek Harness and Claude Code, or chat apps such as Open WebUI and LibreChat) through
Dewey's MCP server. Dewey has no login system and no chat product of its own; its Gradio page is a demo
for your own computer.

It covers:

- [What runs](#what-runs) and [what Dewey needs](#what-dewey-needs)
- [Access control](#access-control) and [the demo page](#the-demo-page)
- [Where to run Dewey](#where-to-run-dewey), then step-by-step setups:
  - [A. One server (VM/VPS) with Docker Compose](#a-one-server-with-docker-compose)
  - [B. Fly.io with Fly Managed Postgres](#b-flyio-with-managed-postgres)
  - [C. Render with a Neon or Supabase database](#c-render-with-neon-or-supabase)
- [Why not Netlify](#why-not-netlify) or [Hugging Face Spaces](#why-hugging-face-spaces-is-a-poor-fit)
- a [settings reference](#settings-reference), a [security checklist](#security-checklist) and
  [troubleshooting](#troubleshooting)

Connecting each client is in [MCP.md](MCP.md).

> **Status.** The Dockerfile, `docker-compose.yml` and the Fly/Render examples are checked by automated
> tests (they parse, `docker compose config` accepts the Compose file, and their key settings line up
> with the code), but we have not yet built and deployed them end to end. Expect to adjust details, and
> tell the maintainers what you had to change.
>
> Platform facts below link to the source we checked them against. Anything marked *unverified* we
> could not check: confirm it in the provider's own docs.

## What runs

| Part | What it does | Command | Runs |
|---|---|---|---|
| **Postgres + pgvector** | Stores articles, their chunks and embeddings (`anthropic` and `open` stacks; the `azure` stack uses Azure AI Search instead) | the `db` service, or a managed database | all the time |
| **Importers** (optional) | Turn a CMS export or API (WordPress, RSS/Atom, CSV, Drupal, Ghost) into `data/*.json` | `python3 app/import_archive.py --help` | when you import |
| **Indexer** | Splits, embeds and stores `data/*.json` in Postgres; re-running updates articles in place | `python app/setup_postgres.py` | when you add articles, then exits |
| **MCP server** | What clients connect to: the `search_archive` and `ask_archive` tools over Streamable HTTP, with a bearer token | `python app/mcp_server.py --http` (the Docker image's default command) | all the time |
| **Demo page** (optional) | A small Gradio chat page for trying Dewey | `python app/main.py` | on your own computer only |

## What Dewey needs

- **A long-running process** for the MCP server. Streamable HTTP keeps sessions in the server's memory
  and streams responses (Server-Sent Events). Platforms that only run short functions per request can't
  host it.
- **A database**, depending on `DEWEY_STACK`:
  - `azure`: Azure AI Search (no Postgres).
  - `anthropic` and `open`: **Postgres with the pgvector extension**, reachable from Dewey on Postgres's
    port (normally 5432).
- **Outbound HTTPS** to your model providers (Anthropic, Voyage AI, Azure), or for the `open` stack a
  model server such as Ollama that Dewey can reach.
- **Memory:** we haven't measured it. The `azure` and `anthropic` stacks run no models locally;
  `INSTALL_LOCAL_MODELS=true` (local embeddings/reranking) needs several GB more. Start small and watch
  for out-of-memory restarts.

## Access control

Dewey has no accounts. Who can use it is decided by four things:

1. **The MCP bearer token.** In HTTP mode the server refuses to start without `DEWEY_MCP_TOKEN` (at
   least 32 characters), and answers `401` to any request without it. There is one shared token, not
   one per person: anyone who has it can search the whole archive and, through `ask_archive`, spend your
   model budget. Give it only to the apps and people who need it, and rotate it when someone leaves.
2. **TLS in front.** The server speaks plain HTTP. Anywhere beyond one machine, put an HTTPS reverse
   proxy (Caddy, nginx, Fly's or Render's edge) in front, so the token and the archive text are
   encrypted on the way.
3. **Network placement.** Docker Compose publishes the MCP port on the host's `127.0.0.1` only and
   never publishes Postgres. Chat apps in Docker reach the MCP server over a separate network that
   doesn't include Postgres ([MCP.md](MCP.md#chat-apps-in-docker-the-dewey-mcp-clients-network)).
   **Never expose Postgres** to the internet.
4. **The client app's own access control.** Dewey can't see individual users: a chat app connects
   with the shared token on behalf of all its users. Decide in the app which users get the Dewey tool
   (Open WebUI, for example, has Access Control for MCP connections; see [MCP.md](MCP.md#open-webui)).

When the MCP server starts in a container it prints, without secrets:

```
dewey-mcp: listening on http://0.0.0.0:8765/mcp (bearer token required)
dewey-mcp: 0.0.0.0 is reachable from other machines, and this server speaks plain HTTP. Put it behind a TLS reverse proxy ...
```

The second line is expected in a container (inside it, `0.0.0.0` is the only way Docker's port mapping
reaches the server); what matters is that the host publishes the port only on `127.0.0.1` or behind TLS.

**Every `ask_archive` call costs money on the hosted stacks**, and `search_archive` still calls your
embedding and reranking services (Voyage AI on the `anthropic` stack by default). Use API keys
dedicated to Dewey so you can revoke them, and watch your providers' usage pages.

## The demo page

`python app/main.py` serves a small chat page at `http://127.0.0.1:7860` (or the next free port) for
trying Dewey on your own computer. It has **no login**, so:

- **It refuses to start** (exit status 2) on any address other than `127.0.0.1`, `localhost` or `::1`
  (for example `GRADIO_SERVER_NAME=0.0.0.0`), with two exceptions you have to set on purpose:
  - `DEWEY_DEMO_BEHIND_AUTH_PROXY=true`: a proxy in front of the page makes people sign in (single
    sign-on through your newsroom's Google or Microsoft accounts, for example), and nothing else can
    reach the page's port. Dewey can't check this, and it can't see who signed in.
  - `DEWEY_DEMO_PUBLISHED_ON_LOOPBACK=true`: the page runs in a container whose port Docker publishes on
    the host's `127.0.0.1` only. The `demo` service in `docker-compose.yml` sets it, because inside a
    container the page has to listen on `0.0.0.0` for Docker's port mapping to reach it. **Don't set it
    yourself** outside such a container: on a machine's own network address it puts the page on the
    network, because the `Host` check below only stops web browsers, not a program that sends
    `Host: 127.0.0.1` directly. The refusal message doesn't offer it for that reason.
- **It answers only requests addressed to `localhost` or `127.0.0.1`** and refuses others with status
  400, unless `DEWEY_DEMO_BEHIND_AUTH_PROXY=true` (a proxy forwards its own host name). Without this, a
  web page you visit could point its own name at 127.0.0.1 ("DNS rebinding") and ask your Dewey
  questions on your API keys.
- **It answers only its own page:** a request that a script on another web page sends (the browser
  marks it with that page's `Origin`) is refused with status 403, again unless
  `DEWEY_DEMO_BEHIND_AUTH_PROXY=true`. Gradio blocks other pages only when the address is exactly
  `localhost` or `127.0.0.1`; without this check, a page you visit could ask questions at
  `http://anything.localhost:7860` (browsers send `*.localhost` to your own computer) and read the
  answers.
- **Always off:** Gradio's usage telemetry, the "Use via API" page, the `/monitoring` pages, Gradio's
  own MCP server and public share links. Gradio's REST routes can't skip the queue, and the generic
  upload route accepts at most 1 MB (Dewey has no upload fields). These are Gradio 6.16.0 options we
  checked in its source. (Gradio 6.16.0 still serves `/monitoring/summary`, request counts and timings,
  even with monitoring off, so Dewey answers 404 for every `/monitoring` path itself.)
- **Not off:** Gradio 6.16.0's default theme loads a font from Google Fonts (`themes/default.py`, and
  its page template preconnects to `fonts.googleapis.com`), so each browser that opens the page
  contacts Google. Questions and answers are not part of that request.
- **Blocked:** Gradio 6.16.0's page template (`templates/frontend/index.html`) also loads a script,
  iframe-resizer, from `cdnjs.cloudflare.com`, with no integrity check. A script runs inside the page,
  next to your questions and answers, so Dewey sends a `Content-Security-Policy: script-src 'self'
  'unsafe-inline'` header that lets the page run only scripts served by Dewey itself (the demo is never
  embedded in another page, so iframe-resizer isn't needed). The browser then doesn't load or run it;
  its developer console reports the blocked script, which is expected.
- `DEWEY_CONCURRENCY` (default 4) is how many questions are answered at the same time. Gradio's own
  default is 1, which makes everyone wait in line behind the slowest answer.

At startup it prints what it's doing, for example in the Compose `demo` service:

```
Dewey: Listening on 0.0.0.0:7860 (reachable from other machines).
Dewey: Demo page, no login (DEWEY_DEMO_PUBLISHED_ON_LOOPBACK=true): Docker must publish this port on the host's 127.0.0.1 only.
Dewey: Requests must be addressed to localhost or 127.0.0.1 (blocks DNS rebinding) and come from the demo page itself (blocks other web pages).
```

To give colleagues a chat page, connect a chat app they can sign in to (Open WebUI, LibreChat) to the
MCP server instead of publishing the demo.

## Where to run Dewey

These options run the MCP server and the database. The demo page stays on your own computer.

| Platform | Fits Dewey? | Postgres + pgvector | TLS and port notes | Cost notes (verified only) | Source |
|---|---|---|---|---|---|
| Your own VM/VPS with Docker Compose | **Yes** | `pgvector/pgvector:pg18` container in the same Compose file | You run the HTTPS reverse proxy (Caddy, nginx) | — | [pgvector README](https://github.com/pgvector/pgvector) |
| Fly.io | **Yes** | Fly Managed Postgres (MPG) includes pgvector, enabled per database | TLS at Fly's edge (`force_https`); MCP server on `internal_port` 8765, TCP health check | MPG Basic plan $38/month, storage $0.28/GB-month (Fly docs, Nov 2026; prices change), plus the app's machine | [superfly/docs](https://github.com/superfly/docs) |
| Render | **Yes, with an external database** | pgvector on Render Postgres: *unverified*. Use Neon or Supabase | TLS at Render's edge; the server's `--port` must equal Render's `PORT` | Free web services spin down when idle (slow first request) and run one instance | [render-oss/skills](https://github.com/render-oss/skills) |
| Hugging Face Spaces | **Poor fit** | Can't reach Postgres: outbound traffic only on ports 80, 443, 8080 | Use "private" visibility, never "protected" | Gradio and Docker Spaces need a paid plan to create | [hub-docs](https://github.com/huggingface/hub-docs/tree/main/docs/hub) |
| Netlify | **No** | — | — | — | [netlify/build](https://github.com/netlify/build/tree/main/packages/zip-it-and-ship-it/src/runtimes) |
| Railway, Google Cloud Run, AWS, Azure | Also possible; check their docs | *unverified* | *unverified* | *unverified* | — |

### Managed Postgres with pgvector

`app/setup_postgres.py` runs `CREATE EXTENSION IF NOT EXISTS vector` itself, so where the provider
allows that, there's nothing to enable by hand.

| Provider | Enabling pgvector | Source |
|---|---|---|
| Neon | Available on every plan; `CREATE EXTENSION IF NOT EXISTS vector` per database | [Neon docs](https://raw.githubusercontent.com/neondatabase/website/main/content/docs/extensions/pgvector.md) |
| Supabase | Dashboard > Database > Extensions > enable "vector" (or with SQL) | [Supabase docs](https://raw.githubusercontent.com/supabase/supabase/master/apps/docs/content/guides/database/extensions/pgvector.mdx) |
| Fly MPG | The cluster's Extensions page: pick the database, switch on `vector` | [Fly docs](https://github.com/superfly/docs/blob/main/postgres/extensions.mdx) |
| Self-hosted | Docker image `pgvector/pgvector:pg18` (or `pg17`, ...) | [pgvector README](https://github.com/pgvector/pgvector#docker) |

**Pooled vs direct connection strings.** Many providers offer a "pooled" connection string (through
PgBouncer) as well as a direct one. Through a pooler in transaction mode, two things in Dewey may
misbehave: the per-connection setting it uses for filtered vector search (`SET hnsw.iterative_scan`)
can be lost, so narrow date/author searches may return fewer articles, and the indexer's repeated
inserts may fail with errors about prepared statements. Whether a given pooler runs in transaction mode
is *unverified*, so **use the direct connection string** for both the MCP server and
`setup_postgres.py`.

## A. One server with Docker Compose

What you need: a Linux server with **Docker Engine 28.0 or newer** and the Compose plugin (install them
following Docker's docs), and, if clients on other machines will connect, a domain name pointing at the
server. Python 3 on the host is handy for the importers (they need only Python's standard library).
Only ports 80 and 443 should be open to the internet; **8765, 7860 and 5432 must not be**.

Why 28.0: Compose keeps the MCP server and the demo page off the network by publishing their ports on
the host's `127.0.0.1` only. Docker's documentation warns that "in releases older than 28.0.0, hosts
within the same L2 segment (for example, hosts connected to the same network switch) can reach ports
published to localhost"
([Docker's port publishing guide](https://github.com/docker/docs/blob/main/content/manuals/engine/network/port-publishing.md),
[moby/moby#45610](https://github.com/moby/moby/issues/45610)). On an older Docker, a machine next to
yours could open the demo page, which has no login, or send the MCP token over plain HTTP. Check with
`docker version` (the Server's `Version`).

One Dewey per server: the `dewey-mcp-clients` network has a fixed name, so a second copy of Dewey's
Compose project on the same machine would join the same network under the same name, `dewey-mcp`. The
[Compose specification](https://github.com/compose-spec/compose-spec/blob/main/spec.md#aliases) says
that when containers share an alias, "exactly which container the name resolves to is not guaranteed",
so a chat app could reach the wrong archive. (The published port, 8765, would also clash.) To run two,
give the second one its own network name, alias and host port in its `docker-compose.yml`.

`docker-compose.yml` runs:

| Service | Starts with | What |
|---|---|---|
| `dewey` | `docker compose up -d` | The MCP server, published on `127.0.0.1:8765`. Also used for one-off commands such as the indexer (`docker compose run --rm dewey ...`) |
| `db` | `docker compose up -d` | Postgres with pgvector; never published |
| `demo` | `docker compose --profile demo up -d` | The demo page, published on `127.0.0.1:7860` only |
| `ollama` | `docker compose --profile ollama up -d` | An Ollama model server for `DEWEY_STACK=open`; never published |

1. **Get the code and configure it.**
   ```bash
   git clone <your fork of dewey-ai> dewey && cd dewey
   cp .env.template .env && chmod 600 .env
   ```
   In `.env`, set:
   - `DEWEY_STACK` and its keys (see the README);
   - `POSTGRES_PASSWORD` to a random string of letters and digits, e.g. the output of
     `openssl rand -hex 24` (it goes inside a URL, so no symbols). `DATABASE_URL` in `.env` is ignored:
     Compose points Dewey at the `db` service;
   - `DEWEY_MCP_TOKEN` to the output of
     `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. Compose refuses to start without it.

   Compose reads `.env` with its own rules: single-quoted values (what `install.py` writes) are taken
   literally, but double-quoted and unquoted values expand `$NAME`, and backslashes are handled
   differently from Python's reader. Avoid `$` outside single quotes and avoid backslashes
   ([Compose env_file rules](https://github.com/compose-spec/compose-spec/blob/main/spec.md)).

2. **Start the MCP server and Postgres.**
   ```bash
   docker compose up -d --build
   docker compose logs -f dewey     # look for "listening on ... (bearer token required)"
   ```
   For local embedding/reranking models, build with
   `INSTALL_LOCAL_MODELS=true docker compose up -d --build` (CPU-only PyTorch; a much bigger image). The
   models download on first use into the `hf-cache` volume.

3. **Import and index your articles.** Put them in `data/` as JSON (format in the README), for example
   with an importer, run on the host:
   ```bash
   python3 app/import_archive.py --help
   ```
   Then run the indexer in the same image as the server:
   ```bash
   docker compose run --rm -v ./data:/app/data:ro dewey python app/setup_postgres.py
   ```
   Re-run it whenever you add articles; it updates in place. (`run` starts a separate, temporary
   container from the `dewey` service with this command instead of the server, and publishes no ports.)

4. **Connect a client and test it.** On the server itself, clients use `http://127.0.0.1:8765/mcp`
   with the header `Authorization: Bearer <DEWEY_MCP_TOKEN>`. Chat apps running in Docker join the
   `dewey-mcp-clients` network and use `http://dewey-mcp:8765/mcp`. Recipes for each client and a
   "test it works" checklist: [MCP.md](MCP.md).

5. **For clients on other machines, put HTTPS in front.** Run a reverse proxy on the same server that:
   - holds a real TLS certificate for your domain,
   - forwards to `127.0.0.1:8765` (the only place Compose publishes the MCP server),
   - does not buffer streaming responses and allows long requests: responses stream as Server-Sent
     Events, and `ask_archive` can take a minute or more.

   Caddy and nginx can both do this. For example, a Caddyfile along these lines (*not tested by us;
   check Caddy's documentation*):
   ```
   dewey.example.org {
       reverse_proxy 127.0.0.1:8765
   }
   ```
   Clients then use `https://dewey.example.org/mcp`. Inside the container the server listens on
   `0.0.0.0`, so it accepts the proxy's `Host` header without extra flags. Optionally, add
   `"--public-url", "https://dewey.example.org/mcp"` to the `dewey` service's `command` so the server
   names its public address in `401` responses ([details](MCP.md#host-names-and-dns-rebinding-protection)).

6. **Optional: an open-weight model in a container** (`DEWEY_STACK=open`). Set
   `OPEN_LLM_BASE_URL='http://ollama:11434/v1'` in `.env`, then:
   ```bash
   docker compose --profile ollama up -d
   docker compose exec ollama ollama pull qwen3:8b
   ```
   The `ollama/ollama` image, its `/root/.ollama` volume and port 11434 are from
   [Ollama's Docker guide](https://raw.githubusercontent.com/ollama/ollama/main/docs/docker.mdx), which
   also covers GPUs. The `ollama` service isn't published to the host. With the
   [recommended](MCP.md#the-two-tools) `search_archive` tool, Dewey calls no chat model at all; Ollama
   is only needed for `ask_archive` and the demo page.

7. **Optional: the demo page.**
   ```bash
   docker compose --profile demo up -d --build
   ```
   It is published on the server's `127.0.0.1:7860` only, so it's for someone working on the server
   itself. Don't publish it further: it has no login ([the demo page](#the-demo-page)).

8. **Updates:** `git pull && docker compose up -d --build` (add `--profile demo` if you run the demo).
   Restarting the server ends open MCP sessions; clients reconnect (how gracefully differs per client,
   *unverified*).

9. **Backups.** Dump the database regularly and keep copies off the server:
   ```bash
   docker compose exec -T db pg_dump -U dewey -Fc dewey > dewey-$(date +%F).dump
   # restore into an empty database:
   docker compose exec -T db pg_restore -U dewey -d dewey < dewey-2026-11-18.dump
   ```
   The index can always be rebuilt from `data/*.json` with `setup_postgres.py`, so backups mainly save
   re-embedding time and cost. Keep `data/` and `.env` somewhere safe too (`.env` holds secrets).

## B. Fly.io with Managed Postgres

Fly runs the Docker image on its machines and terminates HTTPS for you; the image's default command is
the MCP server. Fly's Managed Postgres (MPG) supports pgvector. Facts are from
[superfly/docs](https://github.com/superfly/docs) (`reference/configuration.mdx`, `apps/secrets.mdx`,
`postgres/*.mdx`).

1. **Install `flyctl` and log in** (see Fly's docs).
2. **Create the app without deploying** from the repository root:
   ```bash
   fly launch --no-deploy
   ```
   It detects the Dockerfile and writes a `fly.toml`. Replace its contents with
   `deploy/fly.toml.example`, keeping the `app` name and `primary_region` that `fly launch` chose. The
   example sets `internal_port = 8765`, `force_https`, auto stop/start, and a TCP health check on port
   8765 (Fly's HTTP checks expect a 2xx response, and the MCP endpoint answers 401 without the token).
3. **Create the database** in the same region and enable pgvector:
   ```bash
   fly mpg create --name dewey-db --region <same region as the app>
   ```
   Then, in the cluster's **Extensions** page, pick the database and switch on `vector`. Plans and
   prices: [Fly MPG](https://github.com/superfly/docs/blob/main/postgres/index.mdx) (Basic: $38/month as
   of Nov 2026).
4. **Connect the app to it.** `fly mpg attach <CLUSTER_ID> -a <app>` sets `DATABASE_URL` as a secret,
   but it uses the **pooled** (PgBouncer) URL; see [pooled vs direct](#managed-postgres-with-pgvector).
   Instead, copy the **direct** connection string from the cluster's Connection tab and set it yourself:
   ```bash
   fly secrets set DATABASE_URL='postgres://<user>:<password>@<direct host>/<db>'
   ```
5. **Set the other secrets.** They are encrypted, injected as environment variables, and
   `fly secrets list` shows names only. Setting a secret restarts the machines.
   ```bash
   fly secrets set ANTHROPIC_API_KEY='...' VOYAGE_API_KEY='...'
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # the token: keep it for your clients
   fly secrets set DEWEY_MCP_TOKEN='<the output>'
   ```
   Fly won't show the token again (`fly secrets list` shows names only), so store it in your password
   manager. Typed keys end up in your shell history. `fly secrets set` can also read from standard
   input; see `fly secrets --help`.
6. **Deploy:** `fly deploy`. Clients connect to `https://<app>.fly.dev/mcp` with the bearer token;
   check it with the `curl` commands in [MCP.md](MCP.md#test-that-it-works) (401 without the token, 200
   with it).
7. **Index your articles from your computer.** MPG isn't reachable from the internet, so tunnel to it.
   In one terminal:
   ```bash
   fly mpg proxy          # forwards localhost:16380 to the database
   ```
   In another, using the direct connection string with its host replaced by `localhost:16380`, and the
   same embedding settings as the app:
   ```bash
   DATABASE_URL='postgres://<user>:<password>@localhost:16380/<db>' python app/setup_postgres.py
   ```

Notes:
- With `auto_stop_machines = "stop"` an idle app stops: the next request waits for it to start, and MCP
  sessions (kept in the server's memory) end, so clients have to reconnect. Set it to `"off"` to keep
  the server running.
- Because sessions live in one server's memory, run a single Machine. Fly's configuration reference
  mentions `fly scale count` for setting how many run; whether `fly launch` creates more than one by
  default is *unverified*, so check how many your app has.
- Optional: to pass `--public-url https://<app>.fly.dev/mcp`, override the image's command in
  `fly.toml` with `[experimental] cmd = ["python", "app/mcp_server.py", "--http", "--host", "0.0.0.0",
  "--port", "8765", "--public-url", "https://<app>.fly.dev/mcp"]` (the configuration reference says
  `cmd` "overrides the `CMD` set by the Dockerfile").
- Unmanaged "Fly Postgres" is archived and unsupported, so use MPG.

## C. Render with Neon or Supabase

Render runs the Docker image as a web service and terminates HTTPS at its edge. We couldn't confirm
from a Render-owned source that Render Postgres offers pgvector, so this setup uses an external
database. Render facts are from [render-oss/skills](https://github.com/render-oss/skills)
(`render-web-services`, including `references/deployments.md`; `render-blueprints`, including
`references/field-reference.md`; `render-postgres`).

1. **Create the database** on Neon or Supabase and enable pgvector (see the
   [table above](#managed-postgres-with-pgvector)). Copy its **direct** connection string and make sure
   it uses TLS (e.g. ends in `?sslmode=require`).
2. **Index from your computer:**
   ```bash
   DATABASE_URL='<connection string>' python app/setup_postgres.py
   ```
3. **Add the Blueprint:** copy `deploy/render.yaml.example` to `render.yaml` at the repository root,
   then commit and push it. It runs the MCP server with `dockerCommand` (which overrides the image's
   command) on port 10000 and sets no `healthCheckPath`: Render's default health check for web services
   is a TCP connection, and an HTTP check needs a 2xx or 3xx answer, which the MCP endpoint doesn't
   give without the token.
4. **Create the Blueprint in Render's Dashboard** from your repository (see Render's docs for the exact
   steps). Render asks for the values of the `sync: false` secrets: `DEWEY_MCP_TOKEN` (generate it with
   the `python3 -c ...` command above), `DATABASE_URL`, `ANTHROPIC_API_KEY` and `VOYAGE_API_KEY`. It asks
   only at this first creation; change them later in the service's environment variables.
5. **Check the port.** Render sends public traffic to the port in its `PORT` variable ("default is
   often 10000"; see the service's Settings) and needs the app on `0.0.0.0`. The Blueprint's command
   listens on `0.0.0.0` port 10000. If Render's `PORT` is different, change `--port` in `dockerCommand`
   to match.
6. Clients connect to `https://<service>.onrender.com/mcp` with the bearer token; check it with the
   `curl` commands in [MCP.md](MCP.md#test-that-it-works).

Free web services spin down after inactivity: the next request waits for a cold start, and open MCP
sessions end. They also run a single instance with no persistent disk (Dewey doesn't need one).

## Why not Netlify

Dewey can't run on Netlify. Netlify runs code only as functions, and its function bundler supports
Node.js, Go and Rust runtimes: there is no Python runtime
([runtimes in netlify/build](https://github.com/netlify/build/tree/main/packages/zip-it-and-ship-it/src/runtimes),
[zip-it-and-ship-it README](https://raw.githubusercontent.com/netlify/build/main/packages/zip-it-and-ship-it/README.md)).
Those function archives target AWS Lambda, which runs per request rather than as a persistent server,
and the Netlify CLI's function templates are JavaScript and TypeScript only
([netlify/cli templates](https://github.com/netlify/cli/tree/main/functions-templates)). Dewey's MCP
server is a long-running Python server. Netlify could host only a static page, for example one that
explains how to connect to wherever Dewey runs.

## Why Hugging Face Spaces is a poor fit

From the [Spaces docs](https://github.com/huggingface/hub-docs/tree/main/docs/hub)
(`spaces-overview.md`, `spaces-config-reference.md`, `spaces-sdks-docker.md`):

- Creating a Gradio or Docker Space requires a paid plan (PRO, or Team/Enterprise).
- Outbound requests are allowed only on ports 80, 443 and 8080, so a Space can't reach a normal
  Postgres on port 5432. That rules out the `anthropic` and `open` stacks unless your database listens
  on one of those ports.
- Free CPU hardware sleeps when unused and its disk isn't persistent; the persistent storage feature
  "is no longer available".

If you use a Space anyway (for example with the `azure` stack, which only makes HTTPS calls): set
`python_version` to 3.11 or later (the default is 3.10), store keys as Space secrets (they arrive as
environment variables), and make the Space **private**. Don't use "protected": it keeps the running app
publicly reachable through its embed URL. Docker Spaces serve `app_port`, 7860 by default, while
Dewey's image serves the MCP server on 8765. Whether MCP clients can reach a private Space at all is
*unverified*, so we don't have a recipe for one.

## Settings reference

Set these in `.env`, the Compose file, `fly secrets` / `fly.toml` `[env]`, or the Render Dashboard.

**Engine**

| Setting | Default | What it does |
|---|---|---|
| `DEWEY_MCP_TOKEN` | — (required for HTTP) | Bearer token clients send to the MCP server. At least 32 characters: letters, digits, `- . _ ~ + /`. |
| `POSTGRES_PASSWORD` | — (required) | Docker Compose only: password of the bundled Postgres. Letters and digits. |
| `INSTALL_LOCAL_MODELS` | `false` | Docker build argument: adds CPU-only PyTorch and the local embedding/reranking models. |
| `DATABASE_URL`, `DEWEY_STACK`, API keys | see the README | The stack the MCP server and the indexer use; both must use the same embedding settings. |

`app/mcp_server.py` options (`python app/mcp_server.py --help`): `--http` (Streamable HTTP instead of
stdio), `--host` (default `127.0.0.1`; the Docker image uses `0.0.0.0`), `--port` (default 8765),
`--public-url` (the URL clients use behind a proxy) and `--no-auth` (loopback testing only). Details
and refusals: [MCP.md](MCP.md#before-you-start-http).

**Demo page** (`app/main.py`)

| Setting | Default | What it does |
|---|---|---|
| `GRADIO_SERVER_NAME` | `127.0.0.1` | Address to listen on. Anything other than 127.0.0.1, `localhost` or `::1` needs one of the two flags below. |
| `GRADIO_SERVER_PORT` | `PORT`, else the first free port from 7860 | Port to listen on. The Compose `demo` service pins it to 7860. |
| `PORT` | — | Used when `GRADIO_SERVER_PORT` is unset. Render and similar platforms set it. |
| `DEWEY_DEMO_BEHIND_AUTH_PROXY` | `false` | `true`: a sign-in proxy in front decides who gets in, and nothing else can reach the port. Allows a network address and accepts any `Host` header. |
| `DEWEY_DEMO_PUBLISHED_ON_LOOPBACK` | `false` | `true`: in a container whose port Docker publishes on the host's `127.0.0.1` only. Allows `0.0.0.0` inside the container; requests must still be addressed to localhost. |
| `DEWEY_CONCURRENCY` | `4` | Questions answered at the same time (1-100). Higher means more parallel API calls (and memory with local models). |
| `GRADIO_ROOT_PATH` | — | Serve the demo under a sub-path behind a proxy, e.g. `/dewey` (a Gradio setting). |

## Security checklist

- [ ] **`DEWEY_MCP_TOKEN`** is random (32+ characters), kept in a secret store, shared only with the
      apps and people that need it, and rotated when someone leaves or a config file leaks.
- [ ] **TLS** in front of the MCP server for anything beyond one machine; port 8765 is not open to the
      network.
- [ ] **Docker Engine 28.0 or newer** on a Compose server, so ports published on `127.0.0.1` stay
      local ([why](#a-one-server-with-docker-compose)).
- [ ] **Postgres is never public.** The Compose file publishes no database port; with a managed
      database, use TLS and the provider's access restrictions (*unverified per provider; check their
      docs*).
- [ ] **The demo page stays on localhost**, or sits behind a proxy that makes people sign in, with
      `DEWEY_DEMO_BEHIND_AUTH_PROXY=true`.
- [ ] **Chat apps limit who gets the Dewey tool** (Dewey itself can't tell users apart).
- [ ] **Secrets in the platform's secret store**: `fly secrets`, Render's `sync: false` variables, or a
      `chmod 600` `.env` on your server. Never in git, `fly.toml` `[env]`, or `render.yaml` values.
      `.env` is git- and docker-ignored; keep it that way.
- [ ] **Agents that also have risky tools** (email, files, shell) are reviewed: archive text is
      untrusted input ([MCP.md](MCP.md#security)).
- [ ] **Backups** with `pg_dump` on a schedule, stored off the server, and a restore tested at least
      once. Keep `data/*.json` too.
- [ ] **Logs are newsroom-confidential:** Dewey no longer logs each search's filters, but its error log
      can still hold text derived from a question: a date the model wrote that couldn't be parsed, the
      `open` stack's search plan when it fails validation, and error tracebacks.
- [ ] **Updates:** `requirements.txt` pins every Python package, so rebuilding the image picks up
      security fixes in the base image only. Library fixes arrive as pull requests from Dependabot
      (`.github/dependabot.yml`; turn on "Dependabot security updates" in your fork's settings): merge
      them, then rebuild (`docker compose up -d --build`, `fly deploy`).

## Troubleshooting

- **`required variable DEWEY_MCP_TOKEN is missing a value`** (or `POSTGRES_PASSWORD`) from Docker
  Compose: set it in `.env`.
- **`dewey-mcp: refusing to start: ...`**: the token is missing, too short, or has characters a header
  can't carry. The message says how to generate one.
- **`401` from the MCP server**: the client isn't sending `Authorization: Bearer <token>`, or the token
  is wrong ([MCP.md](MCP.md#troubleshooting)).
- **`421 Invalid Host header`**: the server listens on `127.0.0.1` (no Docker) behind a proxy that
  forwards its own host name. Start it with `--public-url https://your.host/mcp`.
- **A chat app in Docker can't connect to `127.0.0.1:8765`**: inside a container, `127.0.0.1` is the
  container itself. Join the `dewey-mcp-clients` network and use `http://dewey-mcp:8765/mcp`.
- **`Refusing to start: GRADIO_SERVER_NAME=0.0.0.0 lets other machines reach the demo page`**: the demo
  has no login. Unset `GRADIO_SERVER_NAME`, or see [the demo page](#the-demo-page).
- **Status 400, "Dewey's demo page has no login, so it only answers requests addressed to localhost..."**:
  you opened the demo by a name other than `localhost`/`127.0.0.1`, or a proxy forwarded its own host
  name. Use `http://127.0.0.1:<port>/`; behind a sign-in proxy, set `DEWEY_DEMO_BEHIND_AUTH_PROXY=true`.
- **Render deploy never becomes healthy**: `--port` in `dockerCommand` doesn't match Render's `PORT`.
- **Fly health checks fail**: `internal_port` and the check's `port` must both be 8765, the port in the
  image's command.
- **Clients lose their connection after a quiet period**: the platform stopped the server (Fly
  auto-stop, Render free tier). Clients have to reconnect; keep it running if that's a problem.
- **Narrow date or author searches return fewer articles than expected, or indexing fails with
  prepared-statement errors**: you're probably on a pooled connection string; switch to the direct one.

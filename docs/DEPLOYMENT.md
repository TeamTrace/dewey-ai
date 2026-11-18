# Deploying Dewey

This guide is for a newsroom data team that wants colleagues to use Dewey from
their browsers. It covers what Dewey needs, where it can and can't run, and
step-by-step setups for three options:

- [A. One server (VM/VPS) with Docker Compose](#a-one-server-with-docker-compose)
- [B. Fly.io with Fly Managed Postgres](#b-flyio-with-managed-postgres)
- [C. Render with a Neon or Supabase database](#c-render-with-neon-or-supabase)

followed by [why not Netlify or Hugging Face Spaces](#why-not-netlify),
a [settings reference](#settings-reference), a [security checklist](#security-checklist)
and [troubleshooting](#troubleshooting).

It covers the web app (`app/main.py`, the image's default command). The MCP
server for AI agents (`app/mcp_server.py`) is a separate process with its own
token; see [MCP.md](MCP.md).

> **Status.** The Dockerfile, `docker-compose.yml` and the Fly/Render examples are
> checked by automated tests (they parse, and their key settings line up with the
> app), but they have not yet been built and deployed end to end by us. Expect to
> adjust details, and tell the maintainers what you had to change.
>
> Platform facts below link to the source we checked them against. Anything marked
> *unverified* we could not check: confirm it in the provider's own docs.

## What Dewey needs

- **A long-running process.** `python app/main.py` is a web server (Gradio) that
  stays up and streams answers to the browser as they're written (Server-Sent
  Events on `/gradio_api/queue/data`). Platforms that only run short functions
  per request can't host it.
- **A database**, depending on `DEWEY_STACK`:
  - `azure`: Azure AI Search (no Postgres).
  - `anthropic` and `open`: **Postgres with the pgvector extension**, reachable
    from Dewey on Postgres's port (normally 5432).
- **Outbound HTTPS** to your model providers (Anthropic, Voyage AI, Azure), or for
  the `open` stack a model server such as Ollama that Dewey can reach.
- **Memory:** we haven't measured it. The `azure` and `anthropic` stacks run no
  models locally; `INSTALL_LOCAL_MODELS=true` (local embeddings/reranking) needs
  several GB more. Start small and watch for out-of-memory restarts.

## How Dewey protects itself

Out of the box Dewey listens on `127.0.0.1`, so only the computer it runs on can
reach it. Before anyone else can use it you need two things: **login accounts**
and **HTTPS** in front of it.

- **Dewey refuses to start** (exit status 2) when it listens on a network address
  (for example `GRADIO_SERVER_NAME=0.0.0.0`, which the Docker image sets) and no
  accounts are set up. The one exception is `DEWEY_ALLOW_NO_AUTH=true`, for setups
  where a single-sign-on proxy already checks who people are.
- At startup it prints what it's doing, without secrets:
  ```
  Dewey: Listening on 0.0.0.0:7860 (reachable from other machines).
  Dewey: Login required: 3 account(s) from users file /app/users.txt.
  Dewey: Answers up to 4 question(s) at once (DEWEY_CONCURRENCY).
  Dewey: Telemetry, API page, /monitoring, MCP and share links: off.
  ```
- Always off: Gradio's usage telemetry, the "Use via API" page, the `/monitoring`
  pages, Gradio's MCP server and public share links. Gradio's REST routes
  can't skip the queue, and the generic upload route accepts at most 1 MB (Dewey
  has no upload fields). These are set in `app/main.py`; they are Gradio 5.44.1
  options we checked in its source. (Gradio 5.44.1 still serves
  `/monitoring/summary`, request counts and timings, without a login even with
  monitoring off, so Dewey answers 404 for every `/monitoring` path itself.)
- Not off: Gradio 5.44.1's default theme loads a font from Google Fonts
  (`themes/default.py`, and its page template preconnects to
  `fonts.googleapis.com`), so each browser that opens Dewey contacts Google.
  Questions and answers are not part of that request.
- **On your own computer without accounts** (the default), Dewey answers only
  requests addressed to `localhost` or `127.0.0.1` and refuses others with
  status 400. Without this, a web page you visit could point its own name at
  127.0.0.1 ("DNS rebinding") and ask your Dewey questions on your API keys.
- `DEWEY_CONCURRENCY` (default 4) is how many questions are answered at the same
  time. Gradio's own default is 1, which makes everyone wait in line behind the
  slowest answer.

**Every question costs money on the hosted stacks**, so anyone with an account can
spend your API credits. Use API keys dedicated to Dewey so you can revoke them.

## Login accounts

### Create, list and remove accounts

```bash
python3 app/manage_users.py add alice      # asks for the password twice (hidden)
python3 app/manage_users.py list           # names only
python3 app/manage_users.py remove alice
```

- This writes `users.txt` in the repository root, or the file named by
  `DEWEY_USERS_FILE` (in your shell or in `.env`, as Dewey reads it), or
  `--file PATH`. It needs only Python's standard library, so it works on a server
  where Dewey's dependencies aren't installed.
- If `DEWEY_USERS` is set (in your shell or `.env`), Dewey ignores the users file;
  the tool then warns you that the change won't apply, and on `remove` whether the
  person is still listed in `DEWEY_USERS`.
- Passwords must be at least 12 characters. Only a salted scrypt hash is stored;
  the tool never prints passwords or hashes. New files are created readable by
  you only (`chmod 600`); an existing file keeps its permissions.
- **Dewey reads accounts at startup.** Restart it after adding or removing someone.
  A restart also logs everyone out, which is how you end a removed user's session.
- `users.txt` and `.env` are in `.gitignore` and `.dockerignore`. Never commit them.

### Platforms where secrets are environment variables (Fly.io, Render)

Set the `DEWEY_USERS` secret to the lines of `users.txt` joined with commas.
This prints those lines (password hashes, not passwords, but still treat them as
secret):

```bash
grep -v '^#' users.txt | paste -sd, -
```

Each entry contains only letters, digits, `:`, `-` and `_`, so it's safe to pass
in a shell. When `DEWEY_USERS` is set, Dewey uses it instead of any users file.

### Single sign-on instead of accounts

If your organisation already runs a proxy that makes people sign in (with the
newsroom's Google or Microsoft accounts, for example) before it forwards them to
an app, you can skip Dewey's accounts: set `DEWEY_ALLOW_NO_AUTH=true` and make
sure Dewey's port is reachable **only** through that proxy (listen on
`127.0.0.1`, or on a private network the proxy alone can reach). Dewey can't see
who is signed in when it runs this way. Set the flag even when Dewey listens on
`127.0.0.1`: without it (and without accounts) Dewey answers 400 to any request
whose `Host` isn't `localhost` or `127.0.0.1`, which is what a proxy that passes on
your public host name sends.

### Limits of the built-in login (be aware)

- It's Gradio's login form. Over plain HTTP the password and the session cookie
  travel unencrypted, so **use HTTPS**.
- Gradio sets the session cookie twice: once marked `Secure` and once not
  (`access-token-unsecure-...`, for plain-HTTP use). A browser sends the second
  copy on any visit to the `http://` address, before a redirect to HTTPS can
  help. Dewey drops that copy when the request came over HTTPS, which it knows
  only if TLS ends at Dewey or your proxy sends `X-Forwarded-Proto: https`
  ([step 5 below](#a-one-server-with-docker-compose)). Whether Fly.io's and
  Render's proxies send that header is *unverified*; after logging in, check in
  your browser's developer tools that only one `access-token-...` cookie is set.
  If your proxy supports HSTS, turning it on also stops browsers from trying
  `http://` (*unverified per proxy; check its docs*).
- Failed logins are not rate-limited or locked out. Each attempt costs about
  0.1 s of CPU and 32 MB of memory (scrypt), which slows guessing but doesn't stop
  it. Dewey checks at most two passwords at a time and rejects an attempt that
  waits more than 5 seconds, so a flood of attempts can't exhaust the server's
  memory, but while it lasts real people may fail to log in. Use long passwords;
  if your reverse proxy can rate-limit `POST /login`, turn that on.
- Sessions live in Dewey's memory: they last until Dewey restarts (or until the
  person visits `/logout`), and a restart logs everyone out.
- All accounts are equal; there are no admin roles.

## Where to run Dewey

| Platform | Fits Dewey? | Postgres + pgvector | Login / HTTPS notes | Cost notes (verified only) | Source |
|---|---|---|---|---|---|
| Your own VM/VPS with Docker Compose | **Yes** | `pgvector/pgvector:pg18` container in the same Compose file | You run the HTTPS reverse proxy (Caddy, nginx) | — | [pgvector README](https://github.com/pgvector/pgvector) |
| Fly.io | **Yes** | Fly Managed Postgres (MPG) includes pgvector, enabled per database | TLS at Fly's edge (`force_https`); accounts via `DEWEY_USERS` secret | MPG Basic plan $38/month, storage $0.28/GB-month (Fly docs, Nov 2026; prices change), plus the app's machine | [superfly/docs](https://github.com/superfly/docs) |
| Render | **Yes, with an external database** | pgvector on Render Postgres: *unverified*. Use Neon or Supabase | TLS at Render's edge; set `GRADIO_SERVER_PORT` to Render's `PORT` | Free web services spin down when idle (slow first request) and run one instance | [render-oss/skills](https://github.com/render-oss/skills) |
| Hugging Face Spaces | **Poor fit** | Can't reach Postgres: outbound traffic only on ports 80, 443, 8080 | Use "private" visibility, never "protected" | Gradio and Docker Spaces need a paid plan to create | [hub-docs](https://github.com/huggingface/hub-docs/tree/main/docs/hub) |
| Netlify | **No** | — | — | — | [netlify/build](https://github.com/netlify/build/tree/main/packages/zip-it-and-ship-it/src/runtimes) |
| Railway, Google Cloud Run, AWS, Azure | Also possible; check their docs | *unverified* | *unverified* | *unverified* | — |

### Managed Postgres with pgvector

`app/setup_postgres.py` runs `CREATE EXTENSION IF NOT EXISTS vector` itself, so
where the provider allows that, there's nothing to enable by hand.

| Provider | Enabling pgvector | Source |
|---|---|---|
| Neon | Available on every plan; `CREATE EXTENSION IF NOT EXISTS vector` per database | [Neon docs](https://raw.githubusercontent.com/neondatabase/website/main/content/docs/extensions/pgvector.md) |
| Supabase | Dashboard > Database > Extensions > enable "vector" (or with SQL) | [Supabase docs](https://raw.githubusercontent.com/supabase/supabase/master/apps/docs/content/guides/database/extensions/pgvector.mdx) |
| Fly MPG | The cluster's Extensions page: pick the database, switch on `vector` | [Fly docs](https://github.com/superfly/docs/blob/main/postgres/extensions.mdx) |
| Self-hosted | Docker image `pgvector/pgvector:pg18` (or `pg17`, ...) | [pgvector README](https://github.com/pgvector/pgvector#docker) |

**Pooled vs direct connection strings.** Many providers offer a "pooled"
connection string (through PgBouncer) as well as a direct one. Through a pooler in
transaction mode, two things in Dewey may misbehave: the per-connection setting
it uses for filtered vector search (`SET hnsw.iterative_scan`) can be lost, so
narrow date/author searches may return fewer articles, and the indexer's repeated
inserts may fail with errors about prepared statements. Whether a given pooler
runs in transaction mode is *unverified*, so **use the direct connection string**
for both Dewey and `setup_postgres.py`.

## A. One server with Docker Compose

What you need: a Linux server with Docker Engine and the Compose plugin
(install them following Docker's docs), Python 3 on the host (for
`manage_users.py`), and a domain name pointing at the server. Only ports 80
and 443 should be open to the internet; **7860 and 5432 must not be**.

1. **Get the code and configure it.**
   ```bash
   git clone <your fork of dewey-ai> dewey && cd dewey
   cp .env.template .env && chmod 600 .env
   ```
   In `.env`, set `DEWEY_STACK` and its keys (see the README), and
   `POSTGRES_PASSWORD` to a random string of letters and digits, e.g. the output of
   `openssl rand -hex 24` (it goes inside a URL, so no symbols). `DATABASE_URL` in
   `.env` is ignored: Compose points Dewey at the `db` service.

   Compose reads `.env` with its own rules: single-quoted values (what
   `install.py` writes) are taken literally, but double-quoted and unquoted values
   expand `$NAME`, and backslashes are handled differently from Python's reader.
   Avoid `$` outside single quotes and avoid backslashes
   ([Compose env_file rules](https://github.com/compose-spec/compose-spec/blob/main/spec.md)).

2. **Create accounts** before the first `up`. The Compose file mounts `users.txt`
   with `create_host_path: false`, so Docker won't create an empty directory in
   its place; we expect it to refuse to start the container instead (*not tested
   by us*):
   ```bash
   python3 app/manage_users.py add alice
   ```
   The container runs as user id 1000. If `id -u` doesn't print 1000, make the file
   readable to it: `chmod 644 users.txt`.

3. **Start Dewey and Postgres.**
   ```bash
   docker compose up -d --build
   docker compose logs -f dewey     # look for "Login required: 1 account(s)"
   ```
   For local embedding/reranking models, build with
   `INSTALL_LOCAL_MODELS=true docker compose up -d --build` (CPU-only PyTorch;
   a much bigger image). The models download on first use into the `hf-cache`
   volume.

4. **Index your articles** (put them in `data/` first, e.g. with
   `python3 app/import_archive.py`):
   ```bash
   docker compose run --rm -v ./data:/app/data:ro dewey python app/setup_postgres.py
   ```
   Re-run it whenever you add articles; it updates in place.

5. **Put HTTPS in front.** Run a reverse proxy on the same server that:
   - holds a real TLS certificate for your domain,
   - forwards to `127.0.0.1:7860` (the only place Compose publishes Dewey),
   - passes the `Host` header and `X-Forwarded-Proto`/`X-Forwarded-Host`
     (Gradio uses them to build its links),
   - does not buffer streaming responses and allows long requests: answers stream
     as Server-Sent Events and can take a minute or more.

   Caddy and nginx can both do this. For example, a Caddyfile along these lines
   (*not tested by us; check Caddy's documentation*):
   ```
   dewey.example.org {
       reverse_proxy 127.0.0.1:7860
   }
   ```
   To serve Dewey under a sub-path such as `/dewey`, also set
   `GRADIO_ROOT_PATH=/dewey` in `.env` (a Gradio setting).

6. **Optional: an open-weight model in a container** (`DEWEY_STACK=open`). Set
   `OPEN_LLM_BASE_URL='http://ollama:11434/v1'` in `.env`, then:
   ```bash
   docker compose --profile ollama up -d
   docker compose exec ollama ollama pull qwen3:8b
   ```
   The `ollama/ollama` image, its `/root/.ollama` volume and port 11434 are from
   [Ollama's Docker guide](https://raw.githubusercontent.com/ollama/ollama/main/docs/docker.mdx),
   which also covers GPUs. The `ollama` service isn't published to the host.

7. **Updates:** `git pull && docker compose up -d --build`.

8. **Backups.** Dump the database regularly and keep copies off the server:
   ```bash
   docker compose exec -T db pg_dump -U dewey -Fc dewey > dewey-$(date +%F).dump
   # restore into an empty database:
   docker compose exec -T db pg_restore -U dewey -d dewey < dewey-2026-11-18.dump
   ```
   The index can always be rebuilt from `data/*.json` with `setup_postgres.py`, so
   backups mainly save re-embedding time and cost. Keep `data/`, `.env` and
   `users.txt` somewhere safe too (the last two hold secrets).

## B. Fly.io with Managed Postgres

Fly runs the Docker image on its machines and terminates HTTPS for you. Its
Managed Postgres (MPG) supports pgvector. Facts are from
[superfly/docs](https://github.com/superfly/docs) (`reference/configuration.mdx`,
`apps/secrets.mdx`, `postgres/*.mdx`).

1. **Install `flyctl` and log in** (see Fly's docs).
2. **Create the app without deploying** from the repository root:
   ```bash
   fly launch --no-deploy
   ```
   It detects the Dockerfile and writes a `fly.toml`. Replace its contents with
   `deploy/fly.toml.example`, keeping the `app` name and `primary_region` that
   `fly launch` chose. The example sets `internal_port = 7860`, `force_https`,
   auto stop/start, and a health check on `/`.
3. **Create the database** in the same region and enable pgvector:
   ```bash
   fly mpg create --name dewey-db --region <same region as the app>
   ```
   Then, in the cluster's **Extensions** page, pick the database and switch on
   `vector`. Plans and prices: [Fly MPG](https://github.com/superfly/docs/blob/main/postgres/index.mdx)
   (Basic: $38/month as of Nov 2026).
4. **Connect the app to it.** `fly mpg attach <CLUSTER_ID> -a <app>` sets
   `DATABASE_URL` as a secret, but it uses the **pooled** (PgBouncer) URL; see
   [pooled vs direct](#managed-postgres-with-pgvector). Instead, copy the
   **direct** connection string from the cluster's Connection tab and set it
   yourself:
   ```bash
   fly secrets set DATABASE_URL='postgres://<user>:<password>@<direct host>/<db>'
   ```
5. **Set the other secrets.** They are encrypted, injected as environment
   variables, and `fly secrets list` shows names only. Setting a secret restarts
   the machines.
   ```bash
   fly secrets set ANTHROPIC_API_KEY='...' VOYAGE_API_KEY='...'
   fly secrets set DEWEY_USERS="$(grep -v '^#' users.txt | paste -sd, -)"
   ```
   Typed keys end up in your shell history. `fly secrets set` can also read from
   standard input; see `fly secrets --help`.
6. **Deploy:** `fly deploy`, then open `https://<app>.fly.dev`. You should see
   Dewey's login page.
7. **Index your articles from your computer.** MPG isn't reachable from the
   internet, so tunnel to it. In one terminal:
   ```bash
   fly mpg proxy          # forwards localhost:16380 to the database
   ```
   In another, using the direct connection string with its host replaced by
   `localhost:16380`, and the same embedding settings as the app:
   ```bash
   DATABASE_URL='postgres://<user>:<password>@localhost:16380/<db>' python app/setup_postgres.py
   ```

Notes: with `auto_stop_machines = "stop"` an idle app stops, so the next visitor
waits for it to start and everyone has to log in again. Set it to `"off"` to keep
Dewey running. Unmanaged "Fly Postgres" is archived and unsupported, so use MPG.

## C. Render with Neon or Supabase

Render runs the Docker image as a web service and terminates HTTPS at its edge.
We couldn't confirm from a Render-owned source that Render Postgres offers
pgvector, so this setup uses an external database. Render facts are from
[render-oss/skills](https://github.com/render-oss/skills) (`render-web-services`,
`render-blueprints`, `render-postgres`).

1. **Create the database** on Neon or Supabase and enable pgvector (see the
   [table above](#managed-postgres-with-pgvector)). Copy its **direct** connection
   string and make sure it uses TLS (e.g. ends in `?sslmode=require`).
2. **Index from your computer:**
   ```bash
   DATABASE_URL='<connection string>' python app/setup_postgres.py
   ```
3. **Add the Blueprint:** copy `deploy/render.yaml.example` to `render.yaml` at the
   repository root, then commit and push it.
4. **Create the Blueprint in Render's Dashboard** from your repository (see
   Render's docs for the exact steps). Render asks for the values of the
   `sync: false` secrets: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`
   and `DEWEY_USERS` (from the `grep ... | paste` command above). It asks only
   at this first creation; to change accounts later, edit `DEWEY_USERS` in the
   service's environment variables in the Dashboard.
5. **Check the port.** Render sends traffic to the port in its `PORT` variable
   ("default is often 10000"; see the service's Settings). The Docker image listens
   on `GRADIO_SERVER_PORT`, which the Blueprint sets to `10000`. If Render's `PORT`
   is different, change `GRADIO_SERVER_PORT` to match. Dewey logs a warning when the
   two differ.
6. Open the service's URL. You should see Dewey's login page.

Free web services spin down after inactivity: the next visitor waits for a cold
start, and everyone is logged out. They also run a single instance with no
persistent disk (Dewey doesn't need one).

## Why not Netlify

Dewey can't run on Netlify. Netlify runs code only as functions, and its function
bundler supports Node.js, Go and Rust runtimes: there is no Python runtime
([runtimes in netlify/build](https://github.com/netlify/build/tree/main/packages/zip-it-and-ship-it/src/runtimes),
[zip-it-and-ship-it README](https://raw.githubusercontent.com/netlify/build/main/packages/zip-it-and-ship-it/README.md)).
Those function archives target AWS Lambda, which runs per request rather than as
a persistent server, and the Netlify CLI's function templates are JavaScript and
TypeScript only ([netlify/cli templates](https://github.com/netlify/cli/tree/main/functions-templates)).
Dewey is a long-running Python server. Netlify could host only a static page,
for example one that links to wherever Dewey runs.

## Why Hugging Face Spaces is a poor fit

From the [Spaces docs](https://github.com/huggingface/hub-docs/tree/main/docs/hub)
(`spaces-overview.md`, `spaces-config-reference.md`, `spaces-sdks-docker.md`):

- Creating a Gradio or Docker Space requires a paid plan (PRO, or Team/Enterprise).
- Outbound requests are allowed only on ports 80, 443 and 8080, so a Space can't
  reach a normal Postgres on port 5432. That rules out the `anthropic` and `open`
  stacks unless your database listens on one of those ports.
- Free CPU hardware sleeps when unused and its disk isn't persistent; the
  persistent storage feature "is no longer available".

If you use a Space anyway (for example with the `azure` stack, which only makes
HTTPS calls): set `python_version` to 3.11 or later (the default is 3.10), store
keys as Space secrets (they arrive as environment variables), still create Dewey
accounts (`DEWEY_USERS`), and make the Space **private**. Don't use "protected":
it keeps the running app publicly reachable through its embed URL. Docker Spaces
serve `app_port`, 7860 by default, which matches the Dockerfile.

## Settings reference

Set these in `.env`, the Compose file, `fly.toml` `[env]`/`fly secrets`, or the
Render Dashboard.

| Setting | Default | What it does |
|---|---|---|
| `GRADIO_SERVER_NAME` | `127.0.0.1` (Docker image: `0.0.0.0`) | Address to listen on. Anything other than 127.0.0.1, `localhost` or `::1` requires accounts. |
| `GRADIO_SERVER_PORT` | `PORT`, else the first free port from 7860 (Docker image: `7860`) | Port to listen on. |
| `PORT` | — | Used when `GRADIO_SERVER_PORT` is unset. Render and similar platforms set it. |
| `DEWEY_USERS_FILE` | `users.txt` in the repo root | Accounts file written by `manage_users.py`. An explicitly set file that's missing stops Dewey. |
| `DEWEY_USERS` | — | Accounts as one value, entries separated by commas. Used instead of the file. |
| `DEWEY_ALLOW_NO_AUTH` | `false` | `true` allows a network address with no accounts. Only behind a single-sign-on proxy. |
| `DEWEY_AUTH_MESSAGE` | — | Text on the login page, e.g. who to ask for an account. |
| `DEWEY_CONCURRENCY` | `4` | Questions answered at the same time (1-100). Higher means more parallel API calls (and memory with local models). |
| `GRADIO_ROOT_PATH` | — | Serve under a sub-path behind a proxy, e.g. `/dewey`. |
| `POSTGRES_PASSWORD` | — (required) | Docker Compose only: password of the bundled Postgres. Letters and digits. |
| `INSTALL_LOCAL_MODELS` | `false` | Docker build argument: adds CPU-only PyTorch and the local embedding/reranking models. |

## Security checklist

- [ ] **Accounts are set up** and the startup log says `Login required: N account(s)`,
      or Dewey sits behind a single-sign-on proxy with `DEWEY_ALLOW_NO_AUTH=true`
      and its port is closed to everything else.
- [ ] **HTTPS** in front of Dewey. Never serve it over plain HTTP beyond your own
      computer.
- [ ] **Secrets in the platform's secret store**: `fly secrets`, Render's `sync: false`
      variables, or a `chmod 600` `.env` on your server. Never in git, `fly.toml`
      `[env]`, or `render.yaml` values. `.env` and `users.txt` are git- and
      docker-ignored; keep it that way.
- [ ] **Postgres is never public.** The Compose file publishes no database port;
      with a managed database, use TLS and the provider's access restrictions
      (*unverified per provider; check their docs*).
- [ ] **Backups** with `pg_dump` on a schedule, stored off the server, and a restore
      tested at least once. Keep `data/*.json` too.
- [ ] **Accounts kept current:** remove people who leave (`manage_users.py remove`,
      update `DEWEY_USERS` on Fly/Render) and restart Dewey so their session ends.
- [ ] **Logs are newsroom-confidential:** Dewey prints search filters (dates, author
      names) to its error log.
- [ ] **Updates:** rebuild the image now and then (`docker compose up -d --build`,
      `fly deploy`) to pick up security fixes in the base image and libraries.

## Troubleshooting

- **`Refusing to start: GRADIO_SERVER_NAME=0.0.0.0 lets other machines reach Dewey...`**:
  no accounts. Add one with `manage_users.py` (on Fly/Render, set `DEWEY_USERS`), or
  see [single sign-on](#single-sign-on-instead-of-accounts).
- **Compose won't start, mentioning `./users.txt`** (the file doesn't exist): create it with
  `python3 app/manage_users.py add NAME` first. (If you run behind SSO with no
  accounts, `touch users.txt`.)
- **`... is a directory, not a users file`**: an older Docker command created a
  directory where `users.txt` should be. Remove it, create the file, restart.
- **`Can't read users file ...: permission denied`**: `chmod 644 users.txt` (the
  container's user id is 1000).
- **Status 400, "Dewey is running on this computer without login..."**: you opened
  Dewey by a name other than `localhost`/`127.0.0.1`, or a proxy forwarded its own
  host name. Use `http://127.0.0.1:<port>/`; behind a proxy, add accounts, or set
  `DEWEY_ALLOW_NO_AUTH=true` if it's a single-sign-on proxy.
- **Render deploy never becomes healthy**: `GRADIO_SERVER_PORT` doesn't match
  Render's `PORT`. Look for Dewey's warning in the logs.
- **Narrow date or author searches return fewer articles than expected, or indexing
  fails with prepared-statement errors**: you're probably on a pooled connection
  string; switch to the direct one.
- **Everyone got logged out**: Dewey restarted (a deploy, a secret change, or Fly
  auto-stop). Sessions are kept in memory.

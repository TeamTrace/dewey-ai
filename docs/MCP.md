# Connecting agents and chat apps to Dewey (MCP)

Dewey is a newsroom archive engine: it imports articles from your CMS, indexes them, and answers
searches with date and byline filters. Other tools reach it through `app/mcp_server.py`, an
[MCP (Model Context Protocol)](https://modelcontextprotocol.io) server. MCP is the protocol AI agent
harnesses (Hermes Agent, DeepSeek Harness, Claude Code) and chat apps (Open WebUI, LibreChat, Claude
Desktop) use to call outside tools. Once one is connected, it can search your archive in the middle of
its own work, for example "find our coverage of the 2023 budget vote and draft a timeline".

Dewey has no chat product or login system of its own. You use the app you already have, and Dewey
provides the archive search. (`app/main.py` is a small demo page for trying Dewey on your own
computer; see [DEPLOYMENT.md](DEPLOYMENT.md).)

The server uses your existing setup: the same `.env`, the same `DEWEY_STACK` (`azure`, `anthropic` or
`open`) and the same index. It indexes nothing itself and changes nothing in the archive.

> **How we checked these recipes.** Each client section names the documentation we read in that
> client's own GitHub repository (November 2026) and follows it. We have not connected every client
> to Dewey end to end. Anything we couldn't find in those sources is marked *unverified*. Clients
> change quickly: if a step doesn't match what you see, trust the client's current docs and tell the
> Dewey maintainers.

## The two tools

| Tool | Arguments | Returns | Language model? |
|---|---|---|---|
| `search_archive` | `question`, optional `start_date` / `end_date` (YYYY-MM-DD, inclusive), optional `authors` (exact bylines, up to 10) | Up to 10 excerpts, best match first: `source_id` (SRC1, SRC2, ...), `headline`, `url`, `publish_date`, `authors`, `text` | No |
| `ask_archive` | `question` | `answer` (markdown, citations linked like `[[2]](url)`) and `sources` (same fields as above; `[2]` is SRC2) | Yes: Dewey's full pipeline, the same as one question on the demo page |

**Use `search_archive` from agent harnesses and chat apps.** They bring their own language model,
which reads the excerpts and writes the answer under your instructions, so Dewey doesn't need to call a
model at all: it's faster, and on paid stacks it adds no model cost. The agent passes the date and
byline filters itself. `ask_archive` is optional: it makes Dewey's own model plan the search and write
a cited answer, which can help when the client's model is weak at using tools, but it costs a model
call per question. The client recipes below show how to offer only `search_archive` where the client
supports that.

Things to know:
- `text` is an **excerpt** (a passage of about 380 words on the Postgres stacks), not the whole article.
  The agent should cite the `url`.
- Byline filters are exact and case-sensitive ("Jane Doe" doesn't match "jane doe").
- Each `ask_archive` call starts fresh: Dewey doesn't remember earlier calls, so the agent has to put
  the context into the question. Dewey sometimes answers with a clarifying question ("which time
  period?"); the agent can then ask again more precisely.
- The server sends the client short instructions, and the tool descriptions say all of this, including
  that article text is untrusted (see [Security](#security)). Whether a client passes the server's
  instructions to its model depends on the client (Open WebUI does; LibreChat only with
  `serverInstructions: true`).

### Cost and privacy

- **`search_archive`** calls no language model. Its query still goes to the embedding and reranking
  services your stack uses: Azure OpenAI and Azure AI Search on `azure`; Voyage AI on `anthropic` with
  default settings (the Voyage reranker also receives the candidate excerpts). With
  `EMBEDDING_PROVIDER=local` and `RERANK_PROVIDER=local` or `none`, the question goes only to your own
  Postgres. The excerpts it returns go to whatever model the *client* uses.
- **`ask_archive`** sends the question and the retrieved excerpts to Dewey's configured language model:
  Claude (Anthropic API) on `anthropic`, your Azure OpenAI deployment on `azure`, and on `open` the
  server in `OPEN_LLM_BASE_URL`: your own model server, or, if it points at OpenRouter, OpenRouter and
  the provider it picks for each request (see [MODELS.md](MODELS.md)). It makes at least two model calls
  per question (search planning, then the answer; on `open`, a third when the server rejects
  `json_schema` output), so on paid services **every call costs money**. An agent can call a tool many
  times in one task, and the server has no rate limit or budget. Watch your provider's usage page.

## Two ways to connect

| | stdio | Streamable HTTP |
|---|---|---|
| How | The client starts `python app/mcp_server.py` itself, on the same computer | One shared server; clients connect to its URL with a bearer token |
| Good for | An agent on a computer where Dewey is installed (Hermes Agent, Claude Code, Claude Desktop, DeepSeek Harness) | Web chat apps (Open WebUI supports only Streamable HTTP; LibreChat runs stdio servers only from `librechat.yaml`, as programs on its own server), agents on other machines, a team |
| Setup | [Before you start: stdio](#before-you-start-stdio) | [Before you start: HTTP](#before-you-start-http) |

### Before you start: stdio

1. Dewey works on its own on this computer: `python app/main.py` answers a question.
2. Dependencies are installed: `pip install -r requirements.txt` (it includes `mcp==1.30.0`).
3. You know two **absolute** paths, because the client starts the server from its own folder:
   - the Python in Dewey's virtual environment, e.g. `/home/you/dewey-ai/.venv/bin/python`
     (Windows: `C:\Users\you\dewey-ai\.venv\Scripts\python.exe`)
   - the server script, e.g. `/home/you/dewey-ai/app/mcp_server.py`

Quick check: `/home/you/dewey-ai/.venv/bin/python /home/you/dewey-ai/app/mcp_server.py` should sit there
silently, waiting for a client (press Ctrl+C to stop). If it prints `configuration error` and exits,
fix `.env` first.

The server always reads Dewey's `.env` (the one next to `app/`), wherever the client starts it from.
Variables the client passes in its own config take precedence over `.env`. Usually you don't need to
pass any.

### Before you start: HTTP

1. **Create a token** and add it to Dewey's `.env`. Anyone who has it can search the whole archive and
   call `ask_archive`, so treat it like a password.
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   ```bash
   DEWEY_MCP_TOKEN='paste-the-output-here'
   ```
2. **Start the server.** With Docker Compose (see [DEPLOYMENT.md](DEPLOYMENT.md)) it runs as the
   `dewey` service: `docker compose up -d --build`. Without Docker:
   ```bash
   python app/mcp_server.py --http                 # http://127.0.0.1:8765/mcp
   python app/mcp_server.py --http --port 9000     # another port
   ```
3. **Pick the URL your client uses:**

   | Client runs... | URL |
   |---|---|
   | on the same machine, outside Docker | `http://127.0.0.1:8765/mcp` |
   | in a container that joined the `dewey-mcp-clients` network ([below](#chat-apps-in-docker-the-dewey-mcp-clients-network)) | `http://dewey-mcp:8765/mcp` |
   | on another machine | `https://dewey.example.org/mcp`, through a TLS reverse proxy ([below](#other-machines-tls-in-front)) |

   Clients send the token in a header: `Authorization: Bearer <token>`.

The server refuses to start rather than run unprotected:

| Situation | Result |
|---|---|
| `--http` without `DEWEY_MCP_TOKEN` | refuses, and shows the command above (Docker Compose stops even earlier, naming the variable) |
| token shorter than 32 characters, or with characters other than letters, digits and `- . _ ~ + /` | refuses |
| `--no-auth` with `--host 127.0.0.1` (or `::1`, `localhost`) | runs without a token: anything running on this computer can connect, including a reverse proxy, so never put a proxy in front of it. For trying things out |
| `--no-auth` with any other host | refuses |
| `--no-auth` with `--public-url` | refuses (a public URL means a proxy publishes the server) |

#### Host names and DNS-rebinding protection

Whether the server checks the `Host` header depends on the address it listens on. We checked this in
`app/mcp_server.py` (`transport_security_for`) and in the MCP SDK 1.30.0 source
(`mcp/server/fastmcp/server.py`, `mcp/server/transport_security.py`):

- **Listening on a loopback address** (`127.0.0.1`, the default without Docker): the server answers
  only requests addressed to `127.0.0.1`, `localhost` or `[::1]` (any port), plus the host in
  `--public-url`. Anything else gets `421 Invalid Host header`. This stops a web page from reaching the
  server through DNS rebinding. Behind a reverse proxy that forwards your public host name, start it
  with `--public-url https://dewey.example.org/mcp`.
- **Listening on `0.0.0.0`** (what the Docker image and `docker-compose.yml` do inside the container):
  the SDK applies no Host or Origin check, so `127.0.0.1:8765` from the host, `dewey-mcp:8765` (or
  `dewey:8765`) from other containers, and a proxy's public name all work **without extra flags**. The
  bearer token is the only guard, which is why Compose publishes the port on the host's `127.0.0.1`
  only. `tests/mcp/test_http.py` checks these host names.
- `--public-url` is optional with `0.0.0.0`. There it only changes the address the server names in its
  `401` responses (OAuth metadata, which Dewey's clients don't need because they send the token).

#### Chat apps in Docker: the `dewey-mcp-clients` network

Open WebUI and LibreChat usually run in their own Docker Compose project. Dewey's `docker-compose.yml`
puts the MCP server (and only the MCP server, not Postgres) on a network with the fixed name
`dewey-mcp-clients`, where it answers as `dewey-mcp`. Join it from the chat app's Compose file:

```yaml
services:
  open-webui:              # the chat app's service; use the name from its own compose file
    networks: [default, dewey-mcp-clients]

networks:
  dewey-mcp-clients:
    external: true         # created by Dewey's docker-compose.yml; start Dewey first
```

Then use `http://dewey-mcp:8765/mcp` as the server URL. (`external` networks and network aliases are
from the [Compose specification](https://github.com/compose-spec/compose-spec/blob/main/spec.md).)

Open WebUI's docs suggest `http://host.docker.internal:<port>` for an MCP server on the host. With
Dewey's Compose file that address probably won't work, because Dewey publishes port 8765 on the host's
`127.0.0.1` only (*unverified; it depends on your Docker setup*). The shared network avoids the
question.

#### Other machines: TLS in front

The server speaks plain HTTP. For clients on other machines, put a reverse proxy that handles HTTPS
(Caddy, nginx, your platform's load balancer) in front of it and never publish port 8765 to the
network directly: over plain HTTP the token and the archive text cross the network readable. Responses
are streamed (Server-Sent Events), so turn off response buffering for this location if your proxy
buffers (*unverified; check your proxy's docs*). [DEPLOYMENT.md](DEPLOYMENT.md) has the steps.

## Client recipes

The examples call the server `dewey`; any name works. Paths and URLs are placeholders: replace
`/home/you/dewey-ai` with where Dewey is, and pick the URL from the table above.

### Hermes Agent

Source: Hermes Agent's [MCP guide](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/user-guide/features/mcp.md)
and [MCP config reference](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/reference/mcp-config-reference.md).

Hermes reads MCP servers from `~/.hermes/config.yaml`, under `mcp_servers`.

**stdio** (Hermes and Dewey on the same computer):

```yaml
mcp_servers:
  dewey:
    command: "/home/you/dewey-ai/.venv/bin/python"
    args: ["/home/you/dewey-ai/app/mcp_server.py"]
    tools:
      include: [search_archive]   # recommended; remove these two lines to also offer ask_archive
    # env:                        # optional: settings for this server only (they override Dewey's .env)
    #   DEWEY_STACK: "open"
```

**HTTP** (a shared Dewey server). Put `DEWEY_MCP_TOKEN=...` in `~/.hermes/.env` and reference it, so the
token stays out of `config.yaml`. Hermes resolves `${VAR}` in `url` and `headers`, and if the variable
isn't set it refuses to connect and names the variable, rather than sending the literal text:

```yaml
mcp_servers:
  dewey:
    url: "http://127.0.0.1:8765/mcp"     # or https://dewey.example.org/mcp
    headers:
      Authorization: "Bearer ${DEWEY_MCP_TOKEN}"
    tools:
      include: [search_archive]
```

Then start Hermes (`hermes chat`), or run `/reload-mcp` in a running session. Hermes names the tools
`mcp_<server>_<tool>`, here `mcp_dewey_search_archive` (and `mcp_dewey_ask_archive` if you offer it).
A tool call times out after `timeout` seconds (default 300); on a slow local model `ask_archive` can
need more, so add `timeout: 600` to the entry if you use it.

### DeepSeek Harness (`dsh`)

Sources, all in [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
(branch `master`): the [MCP manager README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/mcp/mcp-manager/README.md),
the [MCP client README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/mcp/mcp-client/README.md),
the [config catalog](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/config-catalog.md)
(`@deepseek-ai/dsh-mcp-client`, `@deepseek-ai/dsh-mcp-manager`) and the
[CLI reference](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/apps/cli/reference/README.md).
DeepSeek Harness says it is a developer preview with compatibility-breaking changes to come, so expect
this to drift.

In the Web and Desktop profiles, MCP servers are managed by the harness's MCP manager and can be added
in **Settings** (the exact screens are *unverified*; we read the docs, not the app). Each server entry
takes these fields, which are also what you'd write in YAML:

| Field | stdio | Streamable HTTP |
|---|---|---|
| `transport` | `stdio` | `streamable-http` |
| | `command`: `/home/you/dewey-ai/.venv/bin/python` | `url`: `http://127.0.0.1:8765/mcp` |
| | `args`: `[/home/you/dewey-ai/app/mcp_server.py]` | `headers`: `Authorization: 'Bearer ${credential:DEWEY_MCP_TOKEN}'` |
| `toolCallTimeoutMs` (optional) | default 60000 (60 s); raise it if you offer `ask_archive` on a slow model | same |

`${credential:DEWEY_MCP_TOKEN}` keeps the token out of the configuration. It is resolved by the
harness's credentials provider, which, per the CLI reference, reads the environment `dsh` was started
with, `$DSH_HOME/.credentials.yaml`, the `.env` of the directory you started it in, and `$DSH_HOME/.env`.

To configure it in YAML instead, the manager README shows a patch like this for a profile's
`cordis.patch.yml` (`$DSH_HOME/profiles/<name>/cordis.patch.yml`; logs under `~/.dsh/logs/` suggest
`$DSH_HOME` defaults to `~/.dsh`):

```yaml
- id: mcp                      # the manager row's id; check yours with: dsh --profile web --dump-config
  config:
    servers:
      dewey:
        transport: streamable-http
        url: http://127.0.0.1:8765/mcp
        headers:
          Authorization: 'Bearer ${credential:DEWEY_MCP_TOKEN}'
        enabled: true
```

Two cautions from those docs: a patch **replaces the manager's whole `servers` map** (use Settings to
add one server and keep the others), and the README's examples use the row id `mcp` while it says the
Web and Desktop profiles mount the manager "as the `mcp-manager` row", so check the id with
`--dump-config` before patching.

The tools appear as `mcp__dewey__search_archive` and `mcp__dewey__ask_archive`. We found no per-server
tool filter in these docs, so `ask_archive` is offered too; tell the agent to use `search_archive`, or
leave `ask_archive` unused (*unverified whether a filter exists*). For stdio, the client README notes
that the harness starts a short-lived probe process before the real one, so Dewey loads its stack twice
at startup.

### Open WebUI

Source: Open WebUI's [MCP page](https://raw.githubusercontent.com/open-webui/docs/main/docs/features/extensibility/mcp.mdx)
(open-webui/docs, `docs/features/extensibility/mcp.mdx`).

Open WebUI supports MCP natively from **v0.6.31**, over **Streamable HTTP only**, so it needs Dewey's
HTTP server. Only administrators can add MCP servers:

1. Open **Settings > Integrations** (admin section).
2. Under **External Tool Servers**, click **+ Add Connection**.
3. Set **Type** to **MCP (Streamable HTTP)** (not OpenAPI).
4. **Server URL**: `http://dewey-mcp:8765/mcp` if Open WebUI runs in Docker and joined the
   [shared network](#chat-apps-in-docker-the-dewey-mcp-clients-network); `http://127.0.0.1:8765/mcp` if
   it runs directly on the same machine; your HTTPS URL otherwise.
5. **Auth**: **Bearer**, and paste the token into the **Key** field. (Bearer with an empty key sends an
   empty `Authorization: Bearer` header, which Dewey rejects with 401.)
6. **Function Name Filter List**: `search_archive`, to offer only that tool (the field "restricts which
   tools are exposed to the LLM"; leave it empty for both).
7. **Save** (restart Open WebUI if it asks). Use **Access Control** to choose which users or groups get
   the tool.

In a chat, the model sees the tool as `<server ID>_search_archive`. Open WebUI adds Dewey's server
instructions to the system prompt. Its docs warn that MCP needs a model with reliable native tool
calling; a model that ignores the tool or sends wrong arguments is a model problem, not a broken
connection. If connecting is slow, `MCP_INITIALIZE_TIMEOUT` (default 10 seconds) is the setting to
raise; tool calls use `AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER`.

### LibreChat

Sources: LibreChat's [MCP servers reference](https://raw.githubusercontent.com/LibreChat-AI/librechat.ai/main/content/docs/configuration/librechat_yaml/object_structure/mcp_servers.mdx)
and [MCP feature page](https://raw.githubusercontent.com/LibreChat-AI/librechat.ai/main/content/docs/features/mcp.mdx)
(repository LibreChat-AI/librechat.ai; the same files are served under `LibreChat-AI/docs`).

Define Dewey in `librechat.yaml` (servers added in LibreChat's UI strip `${...}` values from headers,
so the token reference only works in the file):

```yaml
mcpSettings:
  allowedAddresses:
    - 'dewey-mcp:8765'        # LibreChat blocks internal hosts unless they are listed

mcpServers:
  dewey:
    type: streamable-http
    url: http://dewey-mcp:8765/mcp
    headers:
      Authorization: 'Bearer ${DEWEY_MCP_TOKEN}'
    requiresOAuth: false      # a static token: skip OAuth auto-detection
    serverInstructions: true  # pass Dewey's instructions to the model (default: none)
    timeout: 300000           # milliseconds per request (default 60000); matters for ask_archive
```

- `${DEWEY_MCP_TOKEN}` is replaced with the value of that environment variable on the LibreChat
  server, so set it there.
- `requiresOAuth: false` matters: LibreChat's auto-detection probes the server *without* your headers,
  sees Dewey's `401` with a `Bearer` challenge, and can wrongly decide the server needs OAuth.
- LibreChat refuses internal IP addresses and local names such as `dewey-mcp` or
  `host.docker.internal` unless `mcpSettings.allowedAddresses` lists the exact host and port.
- Changes to `librechat.yaml` take a restart.
- We found no per-server tool filter in these pages, so both tools are offered (*unverified whether
  one exists*).

### Claude Code

Checked against `claude mcp add --help` in Claude Code 2.1.327, run while writing this guide.

**stdio:**

```bash
claude mcp add dewey -- /home/you/dewey-ai/.venv/bin/python /home/you/dewey-ai/app/mcp_server.py

# with a setting that overrides .env:
claude mcp add dewey -e DEWEY_STACK=open -- /home/you/dewey-ai/.venv/bin/python /home/you/dewey-ai/app/mcp_server.py
```

**HTTP** (typing the token here leaves it in your shell history):

```bash
claude mcp add --transport http dewey http://127.0.0.1:8765/mcp --header "Authorization: Bearer YOUR_TOKEN"
```

`--scope` chooses where the entry is saved: `local` (default), `user` or `project`. Project-scoped
servers live in the project's `.mcp.json` (per `claude mcp --help`), a file teams often commit, so keep
tokens out of project scope. `claude mcp list` and `claude mcp get dewey` show whether it connects. For
limiting Claude Code to `search_archive`, see Anthropic's Claude Code documentation (*unverified here*).

### Claude Desktop

Source: the MCP project's guide [Connect to local MCP servers](https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/docs/docs/2026-07-28/develop/connect-local-servers.mdx)
(modelcontextprotocol/modelcontextprotocol), which uses Claude Desktop as its example. We found no
setup guide in Anthropic's own GitHub repositories; see Anthropic's MCP documentation for the current
steps.

1. Open Claude Desktop's settings from the **Claude menu** in your system's menu bar (not the settings
   inside the chat window), then **Developer > Edit Config**. That opens `claude_desktop_config.json`:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - Linux: `~/.config/Claude/claude_desktop_config.json`
2. Add Dewey under `mcpServers` (absolute paths):
   ```json
   {
     "mcpServers": {
       "dewey": {
         "command": "/home/you/dewey-ai/.venv/bin/python",
         "args": ["/home/you/dewey-ai/app/mcp_server.py"]
       }
     }
   }
   ```
3. Quit Claude Desktop completely and start it again. If the server doesn't show up, the same guide
   points to the logs in `~/Library/Logs/Claude` (macOS), `%APPDATA%\Claude\logs` (Windows) or
   `~/.config/Claude/logs` (Linux).

Remote servers in Claude are added as "custom connectors" with a URL and a sign-in step
([MCP project's remote guide](https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/docs/docs/2026-07-28/develop/connect-remote-servers.mdx)).
That guide doesn't mention sending a fixed `Authorization` header, and Dewey has no OAuth sign-in, so we
couldn't confirm that a custom connector can use Dewey's HTTP server (*unverified*). Use stdio.

## Test that it works

1. **The server answers (HTTP only).** Without the token you should get `401`, with it `200`:
   ```bash
   BODY='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/mcp \
     -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d "$BODY"
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/mcp \
     -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
     -H "Authorization: Bearer YOUR_TOKEN" -d "$BODY"
   ```
   (The second command leaves the token in your shell history.)
2. **The client sees the tools.** Most clients show connected servers and their tools (Claude Code:
   `claude mcp get dewey`; Open WebUI: **+ > Tools** in a chat). If Dewey's tools aren't there, see
   [Troubleshooting](#troubleshooting) before asking questions.
3. **Ask a question with a byline and a date range** that you know the answer to, using a real byline
   from your archive, for example:
   > Search the Dewey archive for articles by Jane Doe published between 2023-01-01 and 2023-06-30
   > about the school budget. List each headline with its date and URL.

   Then check:
   - The client shows a call to `search_archive` (under its own name, e.g. `mcp_dewey_search_archive`
     in Hermes) with `start_date: "2023-01-01"`, `end_date: "2023-06-30"` and
     `authors: ["Jane Doe"]`. Most clients let you expand a tool call to see its arguments.
   - The URLs in the answer are ones the tool returned, and they open your articles with those dates
     and that byline.
   - The server log shows the filters it applied. On the Postgres stacks it prints a line ending in
     something like `{'start_date': '2023-01-01', 'end_date': '2023-06-30', 'authors': ['Jane Doe']}`
     (`docker compose logs dewey`; in stdio mode the log goes to the client, see [Security](#security)).
4. **If the filters are missing** (no `authors`, no dates), the client's model didn't use them. Ask
   again more explicitly, give the model an instruction to always pass dates and exact bylines, or try
   a model with better tool calling. If `authors` is set but nothing comes back, check the spelling and
   capitalization of the byline in your archive.

## Security

- **Article text is untrusted input to the agent.** Archives quote people, and imported or syndicated
  copy can contain text written to steer an AI ("ignore your instructions and..."). The tool
  descriptions tell the agent to treat `text` as quoted material, but that is a request, not a
  guarantee. Be careful about giving the same agent session tools that can do damage (sending email,
  editing files, running commands), and review its actions.
- **Anyone with the token can search the whole archive** and, through `ask_archive`, spend your model
  budget. There is one shared token: no per-person accounts. In a chat app, the app decides which of its
  users get the tool (Open WebUI: Access Control). The server logs each HTTP request on stdout (path,
  status and the connecting address, which behind a proxy may be the proxy's), but not who sent it. If
  your index holds unpublished material, everyone with access can read it.
- **Keep HTTP behind TLS** beyond one machine, and keep Postgres unreachable from the network (Docker
  Compose never publishes it).
- **Rotate the token** when someone leaves, a laptop is lost, or a config file is shared by mistake:
  generate a new one, update `.env`, restart the server (`docker compose up -d`), update each client.
- **Logs.** The server's diagnostics (including the search filters it applies, and error tracebacks)
  go to stderr. In stdio mode the client that started the server receives that output; whether and
  where it keeps it differs per client (*unverified; check the client's docs*).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| The client says the server failed to start or disconnected (stdio) | Run the exact command from the client's config in a terminal; configuration errors are printed there |
| `docker compose up` stops with `required variable DEWEY_MCP_TOKEN is missing a value` | Add `DEWEY_MCP_TOKEN` to `.env` ([Before you start: HTTP](#before-you-start-http)) |
| `401` | Token missing or wrong; the header must be `Authorization: Bearer <token>`. In Open WebUI, check the Key field; in Hermes and LibreChat, check that the variable is set |
| The client offers to "log in" or "authenticate" to Dewey | Dewey has no OAuth sign-in; the header is missing or the token is wrong. In LibreChat, set `requiresOAuth: false` |
| `421 Invalid Host header` | The server listens on `127.0.0.1` behind a proxy: start it with `--public-url` |
| A container can't reach `127.0.0.1:8765` | Inside a container, `127.0.0.1` is the container itself. Join the [shared network](#chat-apps-in-docker-the-dewey-mcp-clients-network) and use `http://dewey-mcp:8765/mcp` |
| LibreChat refuses the URL | Add the host and port to `mcpSettings.allowedAddresses` |
| `ask_archive` times out | A slow model. Raise the client's tool timeout, or use `search_archive` |
| A byline filter finds nothing | Bylines must match the archive exactly, including capitalization |
| The first call on the `open` stack is very slow or times out | The reranker model downloads on first use (see the README). Run one search before connecting clients |

## For developers

- **stdout is the protocol.** In stdio mode the client reads JSON-RPC messages from the server's stdout,
  so a single stray `print()` breaks the connection. Code that the server runs (`dewey.py`, `providers/`)
  must print to `sys.stderr` or use `logging`. As a backstop, tool calls run with `print()` redirected
  to stderr.
- **Tests:** `python -m unittest discover -s tests -t .` (from the repo root). `tests/mcp/` starts the
  real server as a subprocess, over stdio and over HTTP, with a fake stack and no network. The fake is
  switched on by `DEWEY_MCP_TEST_STACK=1`, a test-only hook. Never set it in a deployment.

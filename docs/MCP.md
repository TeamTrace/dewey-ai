# Using Dewey from AI agents (MCP)

`app/mcp_server.py` turns Dewey into an [MCP (Model Context Protocol)](https://modelcontextprotocol.io)
server. MCP is the protocol AI agents such as Hermes Agent, Claude Code and Claude Desktop use to call
outside tools. Once an agent is connected, it can search your archive and get cited answers in the
middle of its own work, for example "find our coverage of the 2023 budget vote and draft a timeline".

The server uses your existing setup: the same `.env`, the same `DEWEY_STACK` (`azure`, `anthropic` or
`open`), and the index the web app searches. It indexes nothing itself and changes nothing in the
archive.

## The two tools

| Tool | Arguments | Returns | Language model? |
|---|---|---|---|
| `search_archive` | `question`, optional `start_date` / `end_date` (YYYY-MM-DD, inclusive), optional `authors` (exact bylines, up to 10) | Up to 10 excerpts, best match first: `source_id` (SRC1, SRC2, ...), `headline`, `url`, `publish_date`, `authors`, `text` | No |
| `ask_archive` | `question` | `answer` (markdown, citations linked like `[[2]](url)`) and `sources` (same fields as above; `[2]` is SRC2) | Yes: Dewey's full pipeline, the same as one question in the web app |

Things to know:
- `text` is an **excerpt** (a passage of about 380 words on the Postgres stacks), not the whole article.
  The agent should cite the `url`.
- Byline filters are exact and case-sensitive ("Jane Doe" doesn't match "jane doe").
- Each `ask_archive` call starts fresh: Dewey doesn't remember earlier calls, so the agent has to put
  the context into the question. Dewey sometimes answers with a clarifying question ("which time
  period?"); the agent can then ask again more precisely.
- The tool descriptions tell the agent all of this, including that article text is untrusted (see
  [Security](#security)).

### Cost and privacy

- **`ask_archive`** sends the question and the retrieved excerpts to your configured language model:
  Claude (Anthropic API) on `anthropic`, your Azure OpenAI deployment on `azure`, and on `open` the
  server in `OPEN_LLM_BASE_URL`: your own model server, or, if it points at OpenRouter, OpenRouter and
  the provider it picks for each request (see [MODELS.md](MODELS.md)). It makes at least two model calls
  per question (search planning, then the answer; on `open`, a third when the server rejects
  `json_schema` output), so on paid services (`anthropic`, `azure`, OpenRouter) **every call costs
  money**, just like a question in the web app. An agent can call a tool many times in one task, and
  the server has no rate limit or budget. Watch your provider's usage page.
- **`search_archive`** calls no language model. Its query still goes to the embedding and reranking
  services your stack uses: Azure OpenAI and Azure AI Search on `azure`; Voyage AI on `anthropic` with
  default settings (the Voyage reranker also receives the candidate excerpts). With
  `EMBEDDING_PROVIDER=local` and `RERANK_PROVIDER=local` or `none`, the question goes only to your own
  Postgres.
- On a paid stack, you can give an agent only the cheap tool. Hermes Agent supports this per server
  (`tools: include: [search_archive]`, below).

## Before you start

1. Dewey works on its own: `python app/main.py` answers a question. The MCP server needs the same setup.
2. Dependencies are installed: `pip install -r requirements.txt` (it includes `mcp==1.30.0`).
3. You know two **absolute** paths, because the agent starts the server from its own folder:
   - the Python in Dewey's virtual environment, e.g. `/home/you/dewey-ai/.venv/bin/python`
     (Windows: `C:\Users\you\dewey-ai\.venv\Scripts\python.exe`)
   - the server script, e.g. `/home/you/dewey-ai/app/mcp_server.py`

Quick check: `/home/you/dewey-ai/.venv/bin/python /home/you/dewey-ai/app/mcp_server.py` should sit there
silently, waiting for an agent (press Ctrl+C to stop). If it prints `configuration error` and exits,
fix `.env` first.

**Settings.** The server always reads Dewey's `.env` (the one next to `app/`), wherever the agent starts
it from. Variables the agent passes through its own config (`env:` below) take precedence over `.env`.
Usually you don't need to pass any.

## Hermes Agent (runs Dewey on the same machine)

Add Dewey to `~/.hermes/config.yaml` under `mcp_servers` ([Hermes MCP docs](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/user-guide/features/mcp.md)):

```yaml
mcp_servers:
  dewey:
    command: "/home/you/dewey-ai/.venv/bin/python"
    args: ["/home/you/dewey-ai/app/mcp_server.py"]
    # Optional: settings for this server only (they override Dewey's .env)
    # env:
    #   DEWEY_STACK: "open"
    # Optional on a paid stack: offer only the tool that calls no language model
    # tools:
    #   include: [search_archive]
```

Then start Hermes (`hermes chat`), or run `/reload-mcp` in a running session. Hermes names the tools
`mcp_dewey_search_archive` and `mcp_dewey_ask_archive`. A tool call times out after `timeout` seconds
(Hermes default: 300, per its [config reference](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/reference/mcp-config-reference.md)).
On a slow local model, `ask_archive` can need more: add `timeout: 600` to the entry.

## Claude Code

Checked against `claude mcp add --help` in Claude Code 2.1.327:

```bash
claude mcp add dewey -- /home/you/dewey-ai/.venv/bin/python /home/you/dewey-ai/app/mcp_server.py

# with a setting that overrides .env:
claude mcp add dewey -e DEWEY_STACK=open -- /home/you/dewey-ai/.venv/bin/python /home/you/dewey-ai/app/mcp_server.py
```

`--scope` chooses where the entry is saved: `local` (default), `user` or `project`. Project-scoped
servers live in the project's `.mcp.json` (per `claude mcp --help`), a file teams often commit, so keep
tokens out of project scope. `claude mcp list` and `claude mcp get dewey` show whether it connects.

## Claude Desktop

We couldn't verify Claude Desktop's current setup steps for this guide. Follow Anthropic's MCP
documentation for adding a local (stdio) server, and give it the same command and argument as above:
command `/home/you/dewey-ai/.venv/bin/python`, argument `/home/you/dewey-ai/app/mcp_server.py`.

## One shared server over HTTP

Instead of every agent starting its own copy, you can run one server that agents reach over the network
(Streamable HTTP). Use this when agents run on other machines.

1. **Create a token** and add it to Dewey's `.env`:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   ```bash
   DEWEY_MCP_TOKEN='paste-the-output-here'
   ```
2. **Start the server:**
   ```bash
   python app/mcp_server.py --http                 # http://127.0.0.1:8765/mcp
   python app/mcp_server.py --http --port 9000     # another port
   ```
3. **Make it reachable over TLS.** The server speaks plain HTTP. For other machines, keep it on
   `127.0.0.1` and put a reverse proxy that handles HTTPS (nginx, Caddy, your load balancer) in front,
   forwarding to `http://127.0.0.1:8765/mcp`. Tell Dewey the public address:
   ```bash
   python app/mcp_server.py --http --public-url https://dewey.example.org/mcp
   ```
   Without `--public-url`, a proxy that forwards the public `Host` header gets `421 Invalid Host
   header`. Responses are streamed (Server-Sent Events), so turn off response buffering for this
   location if your proxy buffers (unverified — check your proxy's docs). `--host 0.0.0.0` listens on
   every interface; only do that on a network you trust, and the server will warn you.

The server refuses to start rather than run unprotected:

| Situation | Result |
|---|---|
| `--http` without `DEWEY_MCP_TOKEN` | refuses, and shows the command above |
| token shorter than 32 characters, or with characters other than letters, digits and `- . _ ~ + /` | refuses |
| `--no-auth` with `--host 127.0.0.1` (or `::1`, `localhost`) | runs without a token: anything running on this computer can connect, including a reverse proxy, so never put a proxy in front of it. For trying things out |
| `--no-auth` with any other host | refuses |
| `--no-auth` with `--public-url` | refuses (a public URL means a proxy publishes the server) |

**Connecting agents.** Agents send the token in a header: `Authorization: Bearer <token>`.

Hermes Agent: put `DEWEY_MCP_TOKEN=...` in `~/.hermes/.env` and reference it, so the token stays out of
`config.yaml` (Hermes resolves `${VAR}` in `headers` and refuses to connect when it isn't set):

```yaml
mcp_servers:
  dewey:
    url: "https://dewey.example.org/mcp"
    headers:
      Authorization: "Bearer ${DEWEY_MCP_TOKEN}"
```

Claude Code (typing the token here leaves it in your shell history):

```bash
claude mcp add --transport http dewey https://dewey.example.org/mcp --header "Authorization: Bearer YOUR_TOKEN"
```

A missing or wrong token gets `401`. Dewey has no OAuth sign-in, so if an agent offers to "log in" or
"authenticate" to Dewey, the header is missing or the token is wrong.

## Security

- **Article text is untrusted input to the agent.** Archives quote people, and imported or syndicated
  copy can contain text written to steer an AI ("ignore your instructions and..."). The tool
  descriptions tell the agent to treat `text` as quoted material, but that is a request, not a
  guarantee. Be careful about giving the same agent session tools that can do damage (sending email,
  editing files, running commands), and review its actions.
- **Anyone with the token can search the whole archive** and, through `ask_archive`, spend your model
  budget. There is one shared token: no per-person accounts. The server logs each HTTP request on stdout
  (path, status and the connecting address, which behind a proxy may be the proxy's), but not who sent
  it. If your index holds unpublished material, everyone with access can read it.
- **Keep HTTP behind TLS.** Over plain HTTP the token and the archive text cross the network readable.
- **Rotate the token** when someone leaves, a laptop is lost, or a config file is shared by mistake:
  generate a new one, update `.env`, restart the server, update each agent's config.
- **Logs.** The server's diagnostics (including the search filters it applies, and error tracebacks)
  go to stderr. In stdio mode the agent that started the server receives that output; whether and where
  it keeps it differs per agent (unverified — check the agent's docs).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| The agent says the server failed to start or disconnected | Run the exact command from the agent's config in a terminal; configuration errors are printed there |
| `ask_archive` times out | A slow model. Raise the agent's tool timeout, or use `search_archive` |
| `401` | Token missing or wrong; the header must be `Authorization: Bearer <token>` |
| `421 Invalid Host header` | Behind a proxy: start the server with `--public-url` |
| A byline filter finds nothing | Bylines must match the archive exactly, including capitalization |
| The first call on the `open` stack is very slow or times out | The reranker model downloads on first use (see the README). Ask one question in the web app first |

## For developers

- **stdout is the protocol.** In stdio mode the agent reads JSON-RPC messages from the server's stdout,
  so a single stray `print()` breaks the connection. Code that the server runs (`dewey.py`, `providers/`)
  must print to `sys.stderr` or use `logging`. As a backstop, tool calls run with `print()` redirected
  to stderr.
- **Tests:** `python -m unittest discover -s tests -t .` (from the repo root). `tests/mcp/` starts the
  real server as a subprocess, over stdio and over HTTP, with a fake stack and no network. The fake is
  switched on by `DEWEY_MCP_TEST_STACK=1`, a test-only hook. Never set it in a deployment.

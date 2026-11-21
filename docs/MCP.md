# Connecting agents and chat apps to Dewey (MCP)

**In short:** connect your AI tool to Dewey's [MCP server](GLOSSARY.md#mcp-server), and it can search
your archive in the middle of its own work. Ask it, for example, to "find our coverage of the 2023
budget vote and draft a timeline".

**Who this is for:** anyone connecting an [agent harness](GLOSSARY.md#agent-harness) (Claude Code,
Hermes Agent, DeepSeek Harness) or a chat app (Claude Desktop, Open WebUI, LibreChat) through
[MCP](GLOSSARY.md#mcp) ([Model Context Protocol](https://modelcontextprotocol.io)). Dewey has no chat
product or login of its own; to try it without an AI tool, use the [demo page](INSTALL.md#try-the-demo-page).

**You'll need:** Dewey installed, with your articles indexed ([INSTALL.md](INSTALL.md)). The MCP server
uses the same [`.env`](GLOSSARY.md#env-file), index and [stack](GLOSSARY.md#stack) (`anthropic`:
Claude; `open`: open-weight models, not OpenAI; `azure`: Microsoft's Azure OpenAI), and it indexes and
changes nothing. Commands starting with `python` assume you're in the
[Dewey folder](GLOSSARY.md#dewey-folder) with the
[virtual environment](GLOSSARY.md#virtual-environment) on ([how](INSTALL.md#turn-it-on-in-every-new-terminal-window)).

> [!NOTE]
> Each recipe ends with its sources (GitHub-hosted docs where they exist), read in November 2026;
> *unverified* marks what they didn't confirm. Only Claude Code has been connected to Dewey end to end.
> If a step doesn't match, trust the client's docs and
> [tell the maintainers](https://github.com/TeamTrace/dewey-ai/issues).

**On this page:**

- [The two tools](#the-two-tools), with their [cost and privacy](#cost-and-privacy)
- [Two ways to connect](#two-ways-to-connect) and the [client recipes](#client-recipes)
- [Test that it works](#test-that-it-works), [Security](#security), [Troubleshooting](#troubleshooting)
  and [For developers](#for-developers)

## The two tools

| Tool | What you give it | What you get back | Calls a chat model? |
|---|---|---|---|
| `search_archive` | `question`; optional `start_date` and `end_date` (YYYY-MM-DD, inclusive) and `authors` (whole bylines, up to 10) | Up to 10 [excerpts](GLOSSARY.md#excerpt), best match first, with `source_id` (SRC1, SRC2, ...), `headline`, `url`, `publish_date`, `publisher`, `authors`, `text` | No |
| `ask_archive` | `question` | `answer` (markdown, [citations](GLOSSARY.md#citation) linked like `[[2]](url)` for SRC2) and `sources` (the same fields) | Yes: Dewey's full pipeline |

**`search_archive` is the recommended tool.** Your AI tool's own [chat model](GLOSSARY.md#chat-model)
sets the filters, reads the excerpts and writes the answer, so searches are faster and add no
chat-model cost. **`ask_archive` is optional:** Dewey's own chat model plans the search and writes a
cited answer, which helps when the client's model is weak at using tools. Every question costs model
calls ([offering only `search_archive`](#client-recipes)).

**The server still needs the chat model's settings to start,** though searches never use the model:
without them it stops with `dewey-mcp: configuration error`, naming what's missing. On `open`, your
model server (Ollama, for example) needn't be running for searches.

One case differs on Claude: if the saved sign-in looks signed out (after `ant auth logout`), the
server starts and prints a warning. Searches work; `ask_archive` fails until you sign in again
([STACKS.md](STACKS.md#claude-anthropic-api)).

**Things to know:**

- **`text` is an excerpt,** about 380 words on the [Postgres](GLOSSARY.md#postgres) setups
  (`anthropic` and `open`), so the agent should cite the `url`.
- **[Byline filters](GLOSSARY.md#byline-filter) need the whole name:** "Doe" alone finds nothing. On
  the Postgres setups, capital letters and extra spaces don't matter ("jane doe" finds "Jane Doe"). In
  a database created with the plain `C` locale, though, only A to Z ignore case, so "É" must match
  exactly. On `azure`, bylines must match exactly, capitals included, although the tool description
  says case is ignored.
- **`publisher`** (your newsroom, or another outlet in the archive) comes from the article file's
  optional `publisher` field ([format](IMPORTING.md#article-file-format)), or else from
  `NEWSROOM_NAME` at indexing. On `azure` it's always empty.
- **Each `ask_archive` call starts fresh,** so the question must carry its own context. Dewey may ask
  back ("which time period?"); the agent can then ask more precisely.

> [!IMPORTANT]
> **Set `NEWSROOM_NAME` first: the server won't start without it.** It tells every AI tool whose
> archive it searches, and articles indexed without a `publisher` are labelled with it. With
> `ARCHIVE_START_DATE` set, the server also says how far back the archive goes; without it, it gives
> no date. Set both in the wizard's "Newsroom details"
> step ([how](INSTALL.md#change-your-settings)). Then restart the server or AI tool and, on the
> Postgres setups, [index your articles](IMPORTING.md#index-your-articles) again.

### Cost and privacy

What each hosted provider receives, excerpts included, is in
[STACKS.md](STACKS.md#what-the-hosted-providers-receive).

- **`search_archive` calls no chat model,** but its question still goes to your
  [search models](GLOSSARY.md#search-models): Voyage AI by default on `anthropic` (its
  [reranker](GLOSSARY.md#reranking) also gets up to 50 candidate excerpts), Azure OpenAI and Azure AI
  Search on `azure`, and only your own Postgres with local search models. Its excerpts, about
  6,000 to 8,500 [tokens](GLOSSARY.md#token-billing) per search (an estimate), go to your AI tool's
  own model, on that tool's account.
- **`ask_archive` also sends the question and the excerpts to Dewey's chat model:** Claude through the
  [Anthropic API](GLOSSARY.md#anthropic-api), your Azure OpenAI deployment, or on `open` the server in
  `OPEN_LLM_BASE_URL` (your own, or [OpenRouter](GLOSSARY.md#openrouter) and the provider it picks for
  each request; [MODELS.md](MODELS.md#c-hosted-through-openrouter)). That's at least two model calls
  (plan, then answer), and a third on `open` when the server rejects Dewey's request for
  [structured output](GLOSSARY.md#structured-outputs).
- **On `anthropic`, one `ask_archive` call costs about 4 to 12 US cents** with the default model, Claude
  Opus 5.5 (an estimate, not a measurement; see Sources). It stays under about 70 cents unless Claude
  declines it and the request is rerun on another model. Voyage AI search models charge on top.

> [!WARNING]
> **On paid services, every `ask_archive` call costs money.** An agent can call a tool many times in
> one task, and Dewey's server has no rate limit or budget. Watch your provider's usage page (for
> Claude, the [Claude Console](https://platform.claude.com)).

<details><summary>Sources</summary>

- Prices: Anthropic's [pricing page](https://platform.claude.com/docs/en/about-claude/pricing),
  checked November 2026. Claude Opus 5.5 (`claude-opus-5-5`) costs $4 per million input tokens, $20
  per million output tokens and $5 per million for prompt-cache writes. Dewey marks its answer step
  for caching, so that input is most likely billed at $5 (from Anthropic's caching rules; not
  measured).
- The estimate assumes questions of 15 to 60 words, answers of 150 to 500 words and 10 excerpts of 380
  words, at about 1.8 tokens per word (Anthropic's figure of about 555,000 words per million tokens,
  [Opus 5.5 overview](https://platform.claude.com/docs/en/models/opus-5-5/overview)). It allows up to
  2,500 tokens of "thinking", billed as output.
- The ceiling: each of the two calls stops at 16,000 output tokens. It leaves out the refusal
  fallback, which reruns a declined request on Anthropic's recommended model, at that model's rates
  ([refusals and fallback](https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback)).
- Tokens per search: 10 excerpts of 380 words at 1.3 to 1.8 tokens per word (depending on your AI
  tool's model), plus 100 to 150 tokens of other fields each.

</details>

## Two ways to connect

Your AI tool (the [MCP client](GLOSSARY.md#mcp-client)) reaches Dewey's server over
[stdio](GLOSSARY.md#stdio) or [Streamable HTTP](GLOSSARY.md#streamable-http):

| | stdio | Streamable HTTP |
|---|---|---|
| How | The client starts `python app/mcp_server.py` itself, on the same computer | One shared server keeps running; clients connect to its URL with a [bearer token](GLOSSARY.md#bearer-token), a shared password |
| Good for | Claude Code, Claude Desktop, Hermes Agent or DeepSeek Harness on Dewey's computer | Web chat apps, other machines, a team. Open WebUI supports only this; LibreChat runs stdio servers only from `librechat.yaml`, on its own server |
| Setup | [Before you start: stdio](#before-you-start-stdio) | [Before you start: HTTP](#before-you-start-http) |

### Before you start: stdio

1. **Check that Dewey works on its own.** The [demo page](INSTALL.md#try-the-demo-page)
   (`python app/main.py`) should answer a question. The wizard also installs the MCP library
   (`mcp==1.30.0`).
2. **Print the two full paths your AI tool needs.** It starts the server from its own folder, so it
   needs [absolute paths](GLOSSARY.md#path). In the Dewey folder, on macOS and Linux:

   ```bash
   echo "$PWD/.venv/bin/python"
   ```

   ```bash
   echo "$PWD/app/mcp_server.py"
   ```

   Together they print something like:

   ```text
   /Users/you/dewey-ai/.venv/bin/python
   /Users/you/dewey-ai/app/mcp_server.py
   ```

   On Windows ([PowerShell](GLOSSARY.md#powershell)), *not tested by us*:

   ```powershell
   echo "$PWD\.venv\Scripts\python.exe"
   ```

   ```powershell
   echo "$PWD\app\mcp_server.py"
   ```

3. **Start the server once by hand.** On macOS and Linux:

   ```bash
   "$PWD/.venv/bin/python" "$PWD/app/mcp_server.py"
   ```

   On Windows (PowerShell), *not tested by us*:

   ```powershell
   & "$PWD\.venv\Scripts\python.exe" "$PWD\app\mcp_server.py"
   ```

   If it prints nothing and waits, it works: press Control+D once to stop it (Control+C doesn't). On
   Windows, close the window instead (*not tested by us*). If it prints
   `dewey-mcp: configuration error` and exits, fix `.env` first ([common errors](INSTALL.md#troubleshooting)).

Examples on this page use macOS paths. Elsewhere they look like this:

| Computer | Dewey's Python | Server script |
|---|---|---|
| macOS | `/Users/you/dewey-ai/.venv/bin/python` | `/Users/you/dewey-ai/app/mcp_server.py` |
| Linux | `/home/you/dewey-ai/.venv/bin/python` | `/home/you/dewey-ai/app/mcp_server.py` |
| Windows | `C:\Users\you\dewey-ai\.venv\Scripts\python.exe` | `C:\Users\you\dewey-ai\app\mcp_server.py` |

The server always reads Dewey's own `.env`. [Environment variables](GLOSSARY.md#environment-variable)
in a client's config override it.

### Before you start: HTTP

1. **Get a token,** and treat it like a password: anyone who has it can search the whole archive and
   call `ask_archive`. The [setup wizard](GLOSSARY.md#setup-wizard) saves one in `.env` and never
   prints it. To see it, run this in the Dewey folder, but not while you share your screen:

   ```bash
   grep DEWEY_MCP_TOKEN .env
   ```

   On Windows (PowerShell), *not tested by us*:

   ```powershell
   Select-String -Path .env -Pattern DEWEY_MCP_TOKEN
   ```

   Copy the part between the quotes. No token yet? Make one (with the virtual environment on, this
   works in PowerShell too):

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   Then add this line to `.env` (a file in the Dewey folder; edit it in a text editor, don't paste it
   into the terminal), with the output in place of `PASTE_THE_OUTPUT_HERE`:

   ```dotenv
   DEWEY_MCP_TOKEN='PASTE_THE_OUTPUT_HERE'
   ```

2. **Start the server.** With [Docker Compose](GLOSSARY.md#docker-compose), `docker compose up -d --build`
   runs it as the `dewey` service ([DEPLOYMENT.md](DEPLOYMENT.md#a-one-server-with-docker-compose)).
   Without Docker, in the Dewey folder:

   ```bash
   python app/mcp_server.py --http
   ```

   You should see something like:

   ```text
   dewey-mcp: listening on http://127.0.0.1:8765/mcp (bearer token required)
   ```

   Leave it running while clients use it (Control+C stops it). For another
   [port](GLOSSARY.md#port), such as 9000:

   ```bash
   python app/mcp_server.py --http --port 9000
   ```

3. **Pick the URL your client uses.** Clients send the token in the header
   `Authorization: Bearer YOUR_TOKEN`.

   | Client runs... | URL |
   |---|---|
   | on the same machine, outside Docker | `http://127.0.0.1:8765/mcp` |
   | in a container on the [`dewey-mcp-clients` network](#chat-apps-in-docker-the-dewey-mcp-clients-network) | `http://dewey-mcp:8765/mcp` |
   | on another machine | `https://dewey.example.org/mcp`, behind a [TLS reverse proxy](#other-machines-tls-in-front) |

The server refuses to start without a valid token, rather than run unprotected. `--no-auth` runs it
without one, for trying things out, on a loopback `--host` only. Anything on this computer can then
connect, so never put a reverse proxy in front of it.

<details><summary>Every case</summary>

| Situation | Result |
|---|---|
| `--http` without `DEWEY_MCP_TOKEN` | Refuses and shows the command that makes a token |
| A token under 32 characters, or with characters other than letters, digits and `- . _ ~ + /` (and `=` at the end) | Refuses |
| `--no-auth` with `--host 127.0.0.1` (or `::1`, `localhost`) | Runs without a token |
| `--no-auth` with any other host, or with `--public-url` | Refuses |

</details>

#### Host names and DNS-rebinding protection

Skip this unless you use a reverse proxy or see `421 Invalid Host header`.

- **On a [loopback address](GLOSSARY.md#localhost)** (`127.0.0.1`, the default without Docker), the
  server answers only requests addressed to `127.0.0.1`, `localhost` or `[::1]`, or to the host in
  `--public-url`. Others get `421`, which blocks [DNS rebinding](GLOSSARY.md#dns-rebinding). Behind a
  [reverse proxy](GLOSSARY.md#reverse-proxy), start the server with
  `--public-url https://dewey.example.org/mcp`.
- **On `0.0.0.0`** (inside the container, as Docker runs it), there is no such check and no flag is
  needed. The bearer token is the only guard, which is why Compose publishes the port on the host's
  `127.0.0.1` only.

<details><summary>Details and sources</summary>

The loopback check reads the `Host` header (the name a request is addressed to), on any port. On
`0.0.0.0`, the MCP software library (the SDK) applies no Host or Origin check, so `127.0.0.1:8765`,
`dewey-mcp:8765`, `dewey:8765` and a proxy's public name all work. There, `--public-url` only changes
the address named in `401` responses: OAuth metadata, which Dewey's clients don't need.

Checked in `app/mcp_server.py` (`transport_security_for`) and the MCP SDK 1.30.0 source
(`mcp/server/fastmcp/server.py`, `mcp/server/transport_security.py`). `tests/mcp/test_http.py` checks
these host names.

</details>

#### Chat apps in Docker: the `dewey-mcp-clients` network

Open WebUI and LibreChat usually run in their own Docker Compose project. Dewey's
`docker-compose.yml` puts the MCP server, and not Postgres, on a network with the fixed name
`dewey-mcp-clients`, where it answers as `dewey-mcp`. Join it from the chat app's Compose file:

```yaml
services:
  open-webui:              # the chat app's service; use the name from its own compose file
    networks: [default, dewey-mcp-clients]

networks:
  dewey-mcp-clients:
    external: true         # created by Dewey's docker-compose.yml; start Dewey first
```

Then use `http://dewey-mcp:8765/mcp` as the server URL
([Compose specification](https://github.com/compose-spec/compose-spec/blob/main/spec.md)). Open WebUI's
docs suggest `http://host.docker.internal:PORT` instead. That probably won't reach Dewey, whose port is
published on the host's `127.0.0.1` only (*unverified; it depends on your Docker setup*).

#### Other machines: TLS in front

> [!WARNING]
> **Never publish port 8765 to the network directly.** The server speaks plain HTTP, so the token and
> the archive text would cross the network readable.

For clients on other machines, put a reverse proxy that handles HTTPS
([TLS](GLOSSARY.md#tls-and-https)) in front: Caddy, nginx or your platform's load balancer
([DEPLOYMENT.md](DEPLOYMENT.md) has the steps). Responses are streamed (Server-Sent Events), so turn off
response buffering if your proxy buffers (*unverified; check your proxy's docs*).

## Client recipes

The examples name the server `dewey` (any name works). Use your own paths from
[Before you start: stdio](#before-you-start-stdio) and the URL from
[Before you start: HTTP](#before-you-start-http). The wizard's last screen also prints the paths,
correctly if you ran it with the virtual environment on.

On Windows, put paths in YAML in single quotes (`'C:\Users\you\dewey-ai\app\mcp_server.py'`): in double
quotes, `\` starts an escape and breaks the path. Change the wizard's printed Hermes example too. In
[JSON](GLOSSARY.md#json), double every backslash ([Claude Desktop](#claude-desktop) shows how).

### Claude Code

Anthropic's coding agent for the terminal. It needs its own Claude subscription (Pro, Max, Ultra, Team
or Enterprise) or Claude Console account, separate from Dewey's Claude key
([setup page](https://code.claude.com/docs/en/setup), checked November 2026).

**stdio** (Claude Code and Dewey on the same computer): [INSTALL.md](INSTALL.md#claude-code) has the
steps, with the Windows commands, what you should see and a first question. In short, go to the Dewey
folder (`$PWD` below stands for the folder you're in):

```bash
cd ~/dewey-ai
```

Then register Dewey for every folder you use Claude Code in:

```bash
claude mcp add --scope user dewey -- "$PWD/.venv/bin/python" "$PWD/app/mcp_server.py"
```

Then check that it connects (this makes no paid calls):

```bash
claude mcp list
```

The `dewey` line should say `✔ Connected`. In [WSL](GLOSSARY.md#wsl), install and register Claude Code inside Linux, with the macOS and Linux
commands.

**HTTP** (a shared Dewey server), in place of the stdio entry: both are named `dewey`, so if you
added that one, run `claude mcp remove dewey --scope user` first. Typing the token in the next command
leaves it in your shell history, and Claude Code saves it in plain text in `~/.claude.json`. Never add
a token with `--scope project`: that saves it in the project's `.mcp.json`, a file teams often commit.
Put your token in place of `YOUR_TOKEN`:

```bash
claude mcp add --scope user --transport http dewey http://127.0.0.1:8765/mcp --header "Authorization: Bearer YOUR_TOKEN"
```

**Offer only `search_archive`.** To block `ask_archive`, which costs money on paid setups, put a deny
rule in `~/.claude/settings.json` (Windows: `%USERPROFILE%\.claude\settings.json`, *unverified*).
Edit it in a text editor, but not TextEdit, which can curl the quotes. On macOS and Linux, these
create the folder if needed and open the file in nano (Control+O then Enter saves, Control+X quits):

```bash
mkdir -p ~/.claude
```

```bash
nano ~/.claude/settings.json
```

A new file gets exactly this:

```json
{
  "permissions": {
    "deny": ["mcp__dewey__ask_archive"]
  }
}
```

If the file exists, add `"mcp__dewey__ask_archive"` to its `"deny"` list instead; it allows no
comments and no trailing commas. The tool leaves Claude's list without a restart. To also skip the
permission prompt for searches, the whole file is:

```json
{
  "permissions": {
    "allow": ["mcp__dewey__search_archive"],
    "deny": ["mcp__dewey__ask_archive"]
  }
}
```

For one session only, start Claude Code with `claude --disallowedTools mcp__dewey__ask_archive`.
`/permissions` inside Claude Code also adds rules (*steps unverified*). Claude Code enforces these
rules; asking Claude not to use the tool doesn't. They also apply in the VS Code and JetBrains
extensions and in the desktop app's Code tab, but not in its chat.

<details><summary>Scopes, and a setting for this server only</summary>

`--scope` sets where Claude Code saves the entry. To change it, remove the entry
(`claude mcp remove dewey`) and add it again.

| Scope | Where Dewey is available | Saved in |
|---|---|---|
| `user` | Every folder, only for you | `~/.claude.json` (Windows: `%USERPROFILE%\.claude.json`) |
| `local` (the default) | Only in the folder where you added it | `~/.claude.json`, under that folder's path |
| `project` | That project, for everyone on it | `.mcp.json` in the project folder |

To override a `.env` setting for this entry, add `-e` after the name, in place of the stdio command.
The name must come first: right after `-e`, Claude Code reads it as another setting and rejects it.

```bash
claude mcp add --scope user dewey -e DEWEY_STACK=open -- "$PWD/.venv/bin/python" "$PWD/app/mcp_server.py"
```

</details>

<details><summary>Sources</summary>

`claude mcp add --help` and `claude mcp --help` in Claude Code 2.1.329, and Anthropic's Claude Code
pages (not on GitHub), all read in November 2026:
[MCP](https://code.claude.com/docs/en/mcp) (scopes, `-e`, `.mcp.json`),
[MCP quickstart](https://code.claude.com/docs/en/mcp-quickstart) (`claude mcp list`, the first
permission prompt, the Windows location of `.claude.json`),
[permissions](https://code.claude.com/docs/en/permissions) and
[settings](https://code.claude.com/docs/en/settings) (deny and allow rules), and
[setup](https://code.claude.com/docs/en/setup) (installing, and the account it needs).

</details>

### Claude Desktop

> [!NOTE]
> Claude Desktop for Linux is a beta for Ubuntu 22.04 or later and Debian 12 or later (not Fedora),
> checked November 2026. Whether its local MCP servers work as described here is *unverified*.

Anthropic's chat app for macOS 13 or later and Windows 10 or later
([download](https://claude.com/download)). It starts Dewey over stdio, so Dewey must be on the same
computer; servers added here also work in its Code tab.

1. **Print your two paths** ([Before you start: stdio](#before-you-start-stdio), step 2). On Windows,
   these print them ready for JSON, quoted with every `\` doubled; paste them as they are (*not tested
   by us*):

   ```powershell
   "$PWD\.venv\Scripts\python.exe" | ConvertTo-Json
   ```

   ```powershell
   "$PWD\app\mcp_server.py" | ConvertTo-Json
   ```

2. **Open the config file.** On a Mac, click the **Claude** menu in the menu bar (not the settings
   inside the chat window), then **Settings…**, **Developer** and **Edit Config**, which opens the file
   (creating it if needed). On Windows and Linux, look for the same **Settings…**, **Developer** and
   **Edit Config** (how to reach Settings there is *unverified*).

   In Anthropic's newer layout, local MCP servers are under **Settings > Desktop app**, then
   **Manage** next to Extensions; whether that has an Edit Config button is *unverified*. The file is
   `claude_desktop_config.json`:

   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`. Open it with **Edit Config**: a file you
     create by hand might not be the one the app reads (*unverified*).
   - Linux: `~/.config/Claude/claude_desktop_config.json` (no Edit Config button? See below.)

   On a Mac, this shows the folder in Finder:

   ```bash
   open "$HOME/Library/Application Support/Claude"
   ```

<details><summary>On Linux, without an Edit Config button</summary>

Create the file in a terminal. In nano, paste with Ctrl+Shift+V, save with Ctrl+O and then Enter, and
quit with Ctrl+X.

```bash
mkdir -p ~/.config/Claude
```

```bash
nano ~/.config/Claude/claude_desktop_config.json
```

</details>

3. **Add Dewey, with your two paths.** If the file is empty or holds only `{}`, replace its contents
   with this, putting the two paths you printed in place of the examples. Usually only `you` changes,
   to your user name (on Linux, the paths start with `/home/you`):

   ```json
   {
     "mcpServers": {
       "dewey": {
         "command": "/Users/you/dewey-ai/.venv/bin/python",
         "args": ["/Users/you/dewey-ai/app/mcp_server.py"]
       }
     }
   }
   ```

   On Windows, with every `\` doubled:

   ```json
   {
     "mcpServers": {
       "dewey": {
         "command": "C:\\Users\\you\\dewey-ai\\.venv\\Scripts\\python.exe",
         "args": ["C:\\Users\\you\\dewey-ai\\app\\mcp_server.py"]
       }
     }
   }
   ```

   If the file has other settings, add the `"mcpServers"` part inside the outer braces, after a comma;
   if it has `"mcpServers"` already, add only the `"dewey"` entry inside it. In TextEdit, check that
   the quotes stay straight (`"`): curly quotes break the file.

4. **Quit Claude Desktop completely and open it again** (on a Mac: the Claude menu, then **Quit
   Claude**), since it reads the file only at startup. How to quit fully on Windows and Linux is
   *unverified*: if `dewey` isn't listed after reopening, restart the computer and check again.

5. **Check that Dewey is there.** Click **+** at the bottom of the message box, then **Connectors**:
   `dewey` and its two tools should be listed. Claude asks before it uses a tool (the button labels are
   *unverified*).

6. **If `dewey` isn't listed, read Dewey's messages** in `mcp-server-dewey.log` (`mcp.log`, in the
   same folder, records connection failures). On macOS:

   ```bash
   tail -n 20 ~/Library/Logs/Claude/mcp-server-dewey.log
   ```

   On Linux:

   ```bash
   tail -n 20 ~/.config/Claude/logs/mcp-server-dewey.log
   ```

   On Windows (PowerShell), *not tested by us*:

   ```powershell
   Get-Content -Path "$env:APPDATA\Claude\logs\mcp-server-dewey.log" -Tail 20
   ```

Also good to know:

- **Only `search_archive`?** Claude Code's permission rules don't reach this chat, and whether
  Connectors can switch a tool off is *unverified*. Claude asks before each tool use, so you can
  decline `ask_archive` there.
- **Company accounts:** on a Team or Enterprise plan, an owner can turn off "Allow local MCP servers"
  (Claude Desktop 4.29.0 or later; not personal accounts). Dewey then never starts: ask whoever
  manages your Claude account.
- **The Free plan:** whether it runs local MCP servers is *unverified*.
- **Claude Code too?** On a Mac or in WSL, `claude mcp add-from-claude-desktop --scope user` copies
  Dewey into Claude Code.
- **HTTP:** Claude adds remote servers as "custom connectors" with a URL and a sign-in step
  ([MCP project's remote guide](https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/docs/docs/2026-07-28/develop/connect-remote-servers.mdx)).
  The guide doesn't mention a fixed `Authorization` header, and Dewey has no OAuth sign-in, so whether
  this works with Dewey is *unverified*. Use stdio.

<details><summary>Sources</summary>

- The MCP project's guide
  [Connect to local MCP servers](https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/docs/docs/2026-07-28/develop/connect-local-servers.mdx)
  (changed 18 November 2026) uses Claude Desktop as its example: the menu path, file and log
  locations, JSON shape, restart and Windows escaping.
- Anthropic's own pages (we found no setup guide in Anthropic's GitHub repositories), read November
  2026: [Getting started with local MCP servers on Claude Desktop](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)
  (the newer layout, the Connectors check),
  [Install Claude Desktop](https://support.claude.com/en/articles/10065433-install-claude-desktop)
  (requirements),
  [Enterprise configuration for Claude Desktop](https://support.claude.com/en/articles/12622667-enterprise-configuration-for-claude-desktop)
  (company accounts), and Claude Code's [desktop](https://code.claude.com/docs/en/desktop) and
  [Linux desktop](https://code.claude.com/docs/en/desktop-linux) pages (the Code tab, the Linux beta).
- Why Edit Config matters on Windows: the app's MSIX package can keep its own copy of files under
  AppData (Microsoft's
  [MSIX virtualization](https://github.com/MicrosoftDocs/msix-docs/blob/main/msix-src/desktop/flexible-virtualization.md)
  page).

</details>

### Hermes Agent

Hermes reads MCP servers from `~/.hermes/config.yaml`, under `mcp_servers`. For **stdio** (Hermes and
Dewey on the same computer):

```yaml
mcp_servers:
  dewey:
    command: "/Users/you/dewey-ai/.venv/bin/python"
    args: ["/Users/you/dewey-ai/app/mcp_server.py"]
    tools:
      include: [search_archive]   # recommended; remove these two lines to also offer ask_archive
    # env:                        # optional: settings for this server only (they override Dewey's .env)
    #   DEWEY_STACK: "open"
```

For **HTTP**, put the line `DEWEY_MCP_TOKEN=YOUR_TOKEN` in `~/.hermes/.env`, which keeps the token out
of `config.yaml`. Hermes fills in `${VAR}` in `url` and `headers`; if the variable isn't set, it
refuses to connect rather than send the literal text.

```yaml
mcp_servers:
  dewey:
    url: "http://127.0.0.1:8765/mcp"     # or https://dewey.example.org/mcp
    headers:
      Authorization: "Bearer ${DEWEY_MCP_TOKEN}"
    tools:
      include: [search_archive]
```

Then start Hermes (`hermes chat`), or run `/reload-mcp` in a running session. The tools appear as
`mcp_dewey_search_archive` (and `mcp_dewey_ask_archive` if offered). Calls time out after `timeout`
seconds (default 300); for `ask_archive` on a slow local model, add `timeout: 600` to the entry.

<details><summary>Sources</summary>

Hermes Agent's
[MCP guide](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/user-guide/features/mcp.md)
and
[MCP config reference](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/reference/mcp-config-reference.md)
(NousResearch/hermes-agent on GitHub), read November 2026.

</details>

### DeepSeek Harness (`dsh`)

> [!NOTE]
> DeepSeek Harness says it is a developer preview with compatibility-breaking changes to come, so
> expect this recipe to drift.

In the Web and Desktop profiles, add Dewey in **Settings**, through the harness's MCP manager (the
exact screens are *unverified*: we read the docs, not the app). An entry takes these fields:

| Field | stdio | Streamable HTTP |
|---|---|---|
| `transport` | `stdio` | `streamable-http` |
| `command` (stdio) or `url` (HTTP) | `/Users/you/dewey-ai/.venv/bin/python` | `http://127.0.0.1:8765/mcp` |
| `args` (stdio) or `headers` (HTTP) | `[/Users/you/dewey-ai/app/mcp_server.py]` | `Authorization: 'Bearer ${credential:DEWEY_MCP_TOKEN}'` |
| `toolCallTimeoutMs` (optional) | Default 60000 (60 seconds); raise it for `ask_archive` on a slow model | The same |

`${credential:DEWEY_MCP_TOKEN}` keeps the token out of the configuration. Per the CLI reference, the
harness fills it in from the environment `dsh` was started with, `$DSH_HOME/.credentials.yaml`, the
`.env` of the folder you started it in, or `$DSH_HOME/.env`.

The tools appear as `mcp__dewey__search_archive` and `mcp__dewey__ask_archive`. We found no
per-server tool filter, so tell the agent to use `search_archive` (*unverified whether a filter
exists*). Over stdio, a short-lived probe process starts first, so Dewey loads its stack twice.

<details><summary>Configure it in YAML instead</summary>

The manager README shows a patch like this for a profile's `$DSH_HOME/profiles/NAME/cordis.patch.yml`
(logs under `~/.dsh/logs/` suggest `$DSH_HOME` defaults to `~/.dsh`):

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

A patch **replaces the manager's whole `servers` map**, so use Settings to add one server and keep the
others. The README's examples use the row id `mcp`, but it says the Web and Desktop profiles mount the
manager "as the `mcp-manager` row": check the id with `--dump-config` before patching.

</details>

<details><summary>Sources</summary>

All in [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) (branch
`master`), read November 2026:
the [MCP manager README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/mcp/mcp-manager/README.md),
the [MCP client README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/mcp/mcp-client/README.md),
the [config catalog](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/config-catalog.md)
(`@deepseek-ai/dsh-mcp-client`, `@deepseek-ai/dsh-mcp-manager`) and the
[CLI reference](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/apps/cli/reference/README.md).

</details>

### Open WebUI

Open WebUI supports MCP natively from **v0.6.31**, over **Streamable HTTP only**, so it needs Dewey's
HTTP server. Only administrators can add MCP servers:

1. **Open Settings > Integrations**, in the admin section.
2. **Click + Add Connection** under **External Tool Servers**.
3. **Set Type to MCP (Streamable HTTP)**, not OpenAPI.
4. **Enter the Server URL:** `http://dewey-mcp:8765/mcp` from Docker on the
   [shared network](#chat-apps-in-docker-the-dewey-mcp-clients-network), `http://127.0.0.1:8765/mcp`
   directly on the same machine, otherwise your HTTPS URL.
5. **Set Auth to Bearer** and paste the token into **Key** (an empty key sends an empty
   `Authorization: Bearer` header, which Dewey rejects with 401).
6. **Put `search_archive` in the Function Name Filter List** to offer only that tool (empty offers
   both).
7. **Click Save**, restarting Open WebUI if it asks. Then choose in **Access Control** which users or
   groups get the tool.

To check, open **+ > Tools** in a chat. The model sees the tool as the connection's ID followed by
`_search_archive`, with Dewey's server instructions added to the system prompt. Open WebUI's docs warn
that MCP needs a model with reliable native tool calling. A model that ignores the tool or sends wrong
arguments is a model problem, not a broken connection. If connecting is slow, raise
`MCP_INITIALIZE_TIMEOUT` (default 10 seconds); tool calls use `AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER`.

<details><summary>Sources</summary>

Open WebUI's
[MCP page](https://raw.githubusercontent.com/open-webui/docs/main/docs/features/extensibility/mcp.mdx)
(open-webui/docs, `docs/features/extensibility/mcp.mdx`), read November 2026.

</details>

### LibreChat

Define Dewey in `librechat.yaml`. Servers added in LibreChat's UI strip `${...}` values from headers,
so the token reference only works in the file:

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

- Set `DEWEY_MCP_TOKEN` in the LibreChat server's environment.
- Keep `requiresOAuth: false`: LibreChat's auto-detection probes *without* your headers, sees Dewey's
  `401` with a `Bearer` challenge, and can wrongly decide the server needs OAuth.
- `allowedAddresses` must list the exact host and port of internal IP addresses and local names such
  as `dewey-mcp` or `host.docker.internal`.
- Restart LibreChat after changing the file. We found no per-server tool filter, so both tools are
  offered (*unverified whether one exists*).

<details><summary>Sources</summary>

LibreChat's
[MCP servers reference](https://raw.githubusercontent.com/LibreChat-AI/librechat.ai/main/content/docs/configuration/librechat_yaml/object_structure/mcp_servers.mdx)
and
[MCP feature page](https://raw.githubusercontent.com/LibreChat-AI/librechat.ai/main/content/docs/features/mcp.mdx)
(repository LibreChat-AI/librechat.ai; the same files are served under `LibreChat-AI/docs`), read
November 2026.

</details>

## Test that it works

1. **Check that the server answers (HTTP only; with stdio, skip to step 2).** With the server running,
   open a second terminal window (macOS and Linux; we have no tested PowerShell version) and store a
   test request:

   ```bash
   BODY='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
   ```

   Send it without the token:

   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d "$BODY"
   ```

   You should see:

   ```text
   401
   ```

   Now give the shell your token without saving it in your history: run this, paste the token from
   `.env` and press Enter (nothing appears, not even a new line).

   ```bash
   read -rs DEWEY_MCP_TOKEN
   ```

   Send the request again, with the token:

   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -H "Authorization: Bearer $DEWEY_MCP_TOKEN" -d "$BODY"
   ```

   You should see:

   ```text
   200
   ```

   Another `401` means the token didn't match: copy all of it, without the quotes.

2. **Check that the client lists Dewey's tools,** as its recipe shows (in Claude Code,
   `claude mcp get dewey` shows whether it connected). If they're missing, see
   [Troubleshooting](#troubleshooting) first.
3. **Ask something you know the answer to, with a real byline and date range from your archive.** For
   example:

   > Search the Dewey archive for articles by Jane Doe published between 2023-01-01 and 2023-06-30
   > about the school budget. List each headline with its date and URL.

   Then check (most clients let you expand a tool call to see its arguments):

   - The call to `search_archive` (in Hermes, `mcp_dewey_search_archive`) has
     `start_date: "2023-01-01"`, `end_date: "2023-06-30"` and `authors: ["Jane Doe"]`.
   - Every source's `publish_date` is in the range and its `authors` include that byline. The server
     logs these filters only on `azure`, so check them here.
   - The answer's URLs are ones the tool returned, and they open your articles with those dates and
     that byline.

4. **If the filters are missing,** the model didn't use them: ask more explicitly, give the model an
   instruction to always pass dates and whole bylines, or try a model with better tool calling. If
   `authors` is set but nothing comes back, check the byline against the [rules](#the-two-tools).

## Security

> [!WARNING]
> **Article text is untrusted input to the agent.** Archived, imported or syndicated copy can contain
> text written to steer an AI ("ignore your instructions and..."), called
> [prompt injection](GLOSSARY.md#prompt-injection). The tool descriptions ask the agent to treat
> `text` as quoted material, but that's a request, not a guarantee. Be careful giving the same agent
> session tools that can do damage (sending email, editing files, running commands), and review its
> actions.

- **Anyone with the token can search the whole archive,** including any unpublished material in the
  index, and spend your model budget through `ask_archive`. It's one shared token, with no per-person
  accounts: a chat app decides which of its users get the tool (Open WebUI: Access Control).
- **Keep HTTP behind TLS** beyond one machine, and keep Postgres unreachable from the network (Docker
  Compose never publishes it).
- **Rotate the token** when someone leaves, a laptop is lost or a config file is shared by mistake.
  Re-run the setup wizard and answer `n` at "Keep the existing MCP token?", or
  [make one](#before-you-start-http) and update `.env`. Then restart the server (`docker compose up -d`
  with Compose) and update each client; for Claude Code, remove the entry and add it again.
- **Logs.** In HTTP mode the server logs each request's path, status and connecting address (maybe a
  proxy's) on stdout, not who sent it. Error tracebacks go to stderr, with warnings that can quote a
  date or search plan the model wrote from a question. On `azure`, every search also prints its filter
  there, bylines and dates included. In stdio mode the client receives stderr: Claude Desktop keeps it
  in `mcp-server-dewey.log`; other clients differ (*unverified; check the client's docs*).
- **Errors.** When a search fails on the server (the database is down, a key is refused,
  `DATABASE_URL` is mistyped), clients get only "a problem on the Dewey server" and a reference. The
  details, which can quote `DATABASE_URL` with its password, stay in the server's log. Bad arguments
  and index or OpenRouter routing problems are explained to the client.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| A stdio client says the server failed to start or disconnected, or `claude mcp list` shows `✘ Failed to connect` | Run the server by hand ([Before you start: stdio](#before-you-start-stdio), step 3) to see its message |
| `dewey-mcp: configuration error` names a chat-model setting, though you only search | The server needs those settings to start ([The two tools](#the-two-tools)) |
| A tool call fails with "a problem on the Dewey server ... reference ..." | Find that reference in the server's log (stderr; `docker compose logs dewey`). On your own computer after a restart, first check that the database is running ([INSTALL.md](INSTALL.md#start-dewey-again-later)). On Claude, also look for `could not confirm a Claude credential` where the server started |
| `docker compose up` stops with `required variable DEWEY_MCP_TOKEN is missing a value` | Add `DEWEY_MCP_TOKEN` to `.env` ([how](#before-you-start-http)) |
| `401` | The token is missing or wrong (header: `Authorization: Bearer YOUR_TOKEN`). Check Open WebUI's Key field, or the variable Hermes or LibreChat reads |
| The client offers to "log in" or "authenticate" to Dewey | Dewey has no OAuth sign-in, so the header is missing or the token is wrong. In LibreChat, set `requiresOAuth: false` |
| `421 Invalid Host header` | Behind a proxy, start the server with `--public-url` ([details](#host-names-and-dns-rebinding-protection)) |
| A container can't reach `127.0.0.1:8765` | That's the container itself. Join the [shared network](#chat-apps-in-docker-the-dewey-mcp-clients-network) and use `http://dewey-mcp:8765/mcp` |
| LibreChat refuses the URL | Add the host and port to `mcpSettings.allowedAddresses` |
| Claude Code finds Dewey only inside the Dewey folder | Run `claude mcp remove dewey --scope local`, then add it again with `--scope user` |
| Claude Code says "Server already exists" | Run `claude mcp remove dewey --scope user`, then add it again |
| Claude Desktop doesn't list `dewey` | Read its log ([Claude Desktop](#claude-desktop), step 6). Check the file for missing commas, curly quotes and (Windows) single backslashes, then quit the app completely and reopen it |
| `ask_archive` times out | A slow model: raise the client's tool timeout, or use `search_archive` |
| A byline filter finds nothing | Use the whole byline as your archive prints it ([rules](#the-two-tools)) |
| A date or byline search returns fewer stories than you expect | Look in the server's log for `Dewey: warning: the pgvector extension in this database is version`: pgvector older than 0.8 can miss stories ([STACKS.md](STACKS.md#upgrade-pgvector)). Through a connection pooler, see [DEPLOYMENT.md](DEPLOYMENT.md#managed-postgres-with-pgvector) |
| The first call with local search models is very slow or times out | The models load, and the reranker downloads, the first time ([STACKS.md](STACKS.md#local-search-models)). Run one search before you connect clients |

## For developers

- **stdout is the protocol.** In stdio mode the client reads JSON-RPC from stdout, so one stray
  `print()` breaks the connection. Code the server runs (`dewey.py`, `providers/`) must print to
  `sys.stderr` or use `logging`; as a backstop, tool calls run with `print()` redirected to stderr.
- **Tests:** `python -m unittest discover -s tests -t .` from the repo root. `tests/mcp/` runs the real
  server over stdio and HTTP with a fake stack and no network, switched on by the test-only hook
  `DEWEY_MCP_TEST_STACK=1`: never set it in a deployment.

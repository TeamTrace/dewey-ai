# Dewey
[![Python 3.11–3.13](https://img.shields.io/badge/Python-3.11%E2%80%933.13-blue.svg)](docs/INSTALL.md)
[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](https://choosealicense.com/licenses/mit/)

**In short:** Dewey lets reporters search their newsroom's archive from the AI tools they already use, with every result linked to the original article. It imports stories from your [CMS](docs/GLOSSARY.md#cms), indexes them for search by meaning, keyword, date and byline, and serves them to AI tools through [MCP](docs/GLOSSARY.md#mcp). It began on Microsoft Azure at [The Philadelphia Inquirer](https://github.com/phillymedia/dewey-ai); this [fork](docs/GLOSSARY.md#fork) adds setups that don't need Azure, including one that runs entirely on your computer.

**Who this is for:** anyone setting Dewey up, from a reporter trying it on a laptop to a data team running it for a newsroom. You'll paste a few commands into a [terminal](docs/GLOSSARY.md#terminal), and the [install guide](docs/INSTALL.md) explains each one.

**What it isn't:** Dewey has no login system and no chat app of its own. Access is controlled by its [MCP server](docs/GLOSSARY.md#mcp-server)'s [bearer token](docs/GLOSSARY.md#bearer-token), [TLS](docs/GLOSSARY.md#tls-and-https) and where you run it ([docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)). Its [demo page](docs/GLOSSARY.md#demo-page) only listens on your own computer, unless you tell it a sign-in proxy is in front ([why](docs/DEPLOYMENT.md#the-demo-page)).

## How it works

```mermaid
flowchart LR
    A["Your CMS, feed or spreadsheet"] -->|importer| B["Article files in data/"]
    B -->|indexer| C[("Postgres with pgvector<br/>(Azure AI Search on Azure)")]
    C --> D["Dewey MCP server"]
    D --> E["Your AI tool: Claude Code,<br/>Claude Desktop, Open WebUI ..."]
    C -.-> F["Demo page<br/>(your computer only)"]
```

- **Import.** An [importer](docs/GLOSSARY.md#importer) reads your site, export file, [feed](docs/GLOSSARY.md#rss-and-atom-feeds) or spreadsheet and saves the articles as files in `data/`. Importers only read: they never change your site ([importing guide](docs/IMPORTING.md)).
- **Index.** The indexer splits each article into [chunks](docs/GLOSSARY.md#chunk) and stores them with their dates and bylines. Each chunk gets an [embedding](docs/GLOSSARY.md#embedding) for search by meaning ([how to index](docs/IMPORTING.md#index-your-articles); on Azure, [Azure AI Search does it](docs/AZURE.md#build-the-search-index)).
- **Search.** Your AI tool calls Dewey's [two tools](docs/MCP.md#the-two-tools). `search_archive` returns matching [excerpts](docs/GLOSSARY.md#excerpt) with their links, optionally filtered by date and byline, and calls no [chat model](docs/GLOSSARY.md#chat-model). `ask_archive` asks Dewey's own chat model for an answer with [citations](docs/GLOSSARY.md#citation), which costs money on setups with a paid model.

## What you need

- **A computer:** a Mac, a Windows 10 or 11 PC, or a Linux computer such as Ubuntu, Debian or Fedora. You may need an administrator account to install Python and Docker.
- **[Python](docs/GLOSSARY.md#python) 3.11, 3.12 or 3.13.** The guides use 3.13. Python 3.14 has to compile three packages and isn't tested; 3.15 doesn't work.
- **[git](docs/GLOSSARY.md#git)**, to download Dewey's code.
- **A [Postgres](docs/GLOSSARY.md#postgres) database with [pgvector](docs/GLOSSARY.md#pgvector)**, for every setup except Azure. The [setup wizard](docs/GLOSSARY.md#setup-wizard) can give you a [Docker](docs/GLOSSARY.md#docker) command that starts one. Docker Desktop is free for personal use, education, non-commercial open source projects and businesses with fewer than 250 employees and under $10 million in yearly revenue. Other professional use, and any government use, needs a paid plan ([Docker's terms](https://github.com/docker/docs/blob/main/content/includes/desktop-license-update.md), checked November 2026); the [install guide](docs/INSTALL.md) lists free alternatives.
- **Accounts, depending on the setup.** Dewey has [five setups](docs/STACKS.md#the-setups-at-a-glance); the fully open one needs no accounts, but a capable computer, such as an Apple silicon Mac with 32 GB of memory (an estimate). In the wizard, the [stack](docs/GLOSSARY.md#stack) called `anthropic` means Claude, `open` means [open-weight models](docs/GLOSSARY.md#open-weight-model) (not OpenAI), and `azure` means Microsoft's [Azure OpenAI](docs/GLOSSARY.md#azure-openai) Service.
- **Your archive**, from WordPress, an RSS or Atom feed, a [CSV](docs/GLOSSARY.md#csv) file, Drupal or Ghost, or as [JSON](docs/GLOSSARY.md#json) files you make ([choose an importer](docs/IMPORTING.md#choose-an-importer)).
- **An AI tool (optional):** any [MCP client](docs/GLOSSARY.md#mcp-client), such as Claude Code, Claude Desktop, Open WebUI or LibreChat; the demo page works without one.
- **Time:** about an hour for the steps (a rough estimate), plus downloads and indexing ([how long those take](docs/INSTALL.md#the-slow-steps)).

| Computer | How it's been tested (November 2026) |
|---|---|
| Mac | Installed and run end to end on an Apple silicon Mac, with one of the five setups |
| Linux | The automated tests run on Ubuntu for every pull request; nobody has run the install end to end |
| Windows | Not tested by us |

Two limits (checked November 2026): Intel Macs can't install the [local search models](docs/GLOSSARY.md#search-models) that three of the five setups use. On an Arm-based Windows PC, the Arm version of Python can't install seven of Dewey's packages; [WSL](docs/GLOSSARY.md#wsl) might work (untested).

## Quick start

Six steps get Dewey running on one computer. If any of this is new to you, follow the [install guide](docs/INSTALL.md) instead. Copy one command box at a time, paste it into the terminal (Command+V on a Mac, Ctrl+V on Windows, Ctrl+Shift+V in most Linux terminals) and press Enter. We haven't tested the Windows commands.

1. **Open a terminal and check for Python, git and Docker.** On a Mac, press Command+Space, type Terminal and press Enter. On Windows, click Start, type PowerShell and open Windows [PowerShell](docs/GLOSSARY.md#powershell) (not Command Prompt). On Linux, press the Super (Windows) key, type Terminal and press Enter.

   On macOS and Linux:
   ```bash
   python3.13 --version
   ```
   On Windows, in PowerShell:
   ```powershell
   py -3.13 --version
   ```
   It should print `Python 3.13.17` or later on a Mac or Windows, or any 3.13 on Linux (on Ubuntu 24.04, use `python3.12` wherever this page says `python3.13`). Then check git, on every computer:
   ```bash
   git --version
   ```
   It should print a version such as `git version 2.50.1`. On a Mac, if a box offers the command line developer tools, click Install.

   Missing either one? Get your [Mac](docs/INSTALL.md#get-your-mac-ready), [Windows PC](docs/INSTALL.md#get-your-windows-pc-ready) or [Linux computer](docs/INSTALL.md#get-your-linux-computer-ready) ready first. Don't use python.org's main Download button: it gives you Python 3.15, which can't install Dewey.

   Every setup except Azure needs a database. The wizard can give you a command that starts one, if Docker is installed and running before step 5. To check, on every computer:
   ```bash
   docker version
   ```
   A section starting `Server:` means it's running. If not, the same three pages cover Docker and other databases. For the fully open setup, install Ollama from them and raise its context length before your first question ([how](docs/INSTALL.md#what-the-wizard-asks)).

2. **Download Dewey's code.** This makes a folder called `dewey-ai` in your home folder, where new terminal windows start ([help](docs/INSTALL.md#get-the-dewey-code)). On every computer:
   ```bash
   git clone https://github.com/TeamTrace/dewey-ai.git
   ```
   ```bash
   cd dewey-ai
   ```

3. **Create a virtual environment.** This gives Dewey its own private set of Python packages, in a folder called `.venv` ([what's that?](docs/GLOSSARY.md#virtual-environment)).

   On macOS and Linux:
   ```bash
   python3.13 -m venv .venv
   ```
   On Windows, in PowerShell:
   ```powershell
   py -3.13 -m venv .venv
   ```
   It prints nothing when it works. On Ubuntu or Debian, if it mentions `ensurepip`, see [Troubleshooting](docs/INSTALL.md#troubleshooting).

4. **Turn the virtual environment on.**

   On macOS and Linux:
   ```bash
   source .venv/bin/activate
   ```
   On Windows, in PowerShell, first let PowerShell run the environment's script, which Windows blocks by default ([details](docs/INSTALL.md#get-your-windows-pc-ready), step 5):
   ```powershell
   Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
   ```
   At `Do you want to change the execution policy?`, type `Y` and press Enter. Then:
   ```powershell
   .venv\Scripts\Activate.ps1
   ```
   Your prompt now starts with `(.venv)`. Turn it on again in every new terminal window ([how](docs/INSTALL.md#turn-it-on-in-every-new-terminal-window)).

> [!WARNING]
> Type your newsroom's details when the wizard asks. Left blank, they default to The Philadelphia Inquirer's, and Dewey presents your articles to journalists as the Inquirer's.

5. **Run the setup wizard.** Not sure which setup to choose? [Compare the setups](docs/STACKS.md#the-setups-at-a-glance) first. On every computer:
   ```bash
   python install.py
   ```
   It asks for your setup's stack and search models ([what to answer](docs/INSTALL.md#which-setup-to-choose)), shows where to get any accounts and [keys](docs/GLOSSARY.md#api-key), and hides secrets as you type them. It saves your settings in `.env`, a [settings file](docs/GLOSSARY.md#env-file) in the Dewey folder. It can also install Dewey's packages, import your archive (on most setups) and index it. **When it asks `Index them now?`, type `y`:** the default is No.

   On Windows, some commands the wizard prints work only on Mac and Linux (a known problem: [#25](https://github.com/TeamTrace/dewey-ai/issues/25)); the install guide's [wizard section](docs/INSTALL.md#what-the-wizard-asks) gives PowerShell versions.

6. **Try it on the demo page.** On every computer:
   ```bash
   python app/main.py
   ```
   You should see something like:
   ```text
   Dewey: Listening on 127.0.0.1, first free port from 7860 (this computer only).
   ...
   * Running on local URL:  http://127.0.0.1:7860
   ```
   Open that `Running on local URL` address in your browser. Ignore the `share=True` hint after it: Dewey keeps public links off on purpose. Press Control+C in the terminal to stop the page.

   Ask questions you already know the answers to first, and check each citation against its article before you rely on an answer. On setups with a paid chat model, each question costs money. More help: [Try the demo page](docs/INSTALL.md#try-the-demo-page). To use Claude Code or another AI tool instead, see [Connect an AI tool](docs/INSTALL.md#connect-an-ai-tool).

**After a restart,** start Docker again: open Docker Desktop, or run `colima start`; on Linux it starts by itself. A database made with the wizard's Docker command is set to start again with Docker, unless you stopped it yourself. If Dewey can't reach its database, start it. On every computer:

```bash
docker start dewey-db
```

More, including databases made by an earlier version of the wizard, which stay stopped: [Start Dewey again later](docs/INSTALL.md#start-dewey-again-later).

## Where to go next

| I want to… | Read |
|---|---|
| Set Dewey up step by step on a Mac, Windows PC or Linux computer | [docs/INSTALL.md](docs/INSTALL.md) |
| Compare the five setups: cost, computer, accounts and downloads | [docs/STACKS.md](docs/STACKS.md) |
| Know which outside companies see our questions and articles | [What the hosted providers receive](docs/STACKS.md#what-the-hosted-providers-receive) |
| Import my archive, or make article files myself | [docs/IMPORTING.md](docs/IMPORTING.md) |
| Run the original Azure setup | [docs/AZURE.md](docs/AZURE.md) |
| Connect Claude Code, Claude Desktop, Hermes Agent, DeepSeek Harness, Open WebUI or LibreChat | [docs/MCP.md](docs/MCP.md) |
| Run Dewey on a server for a team, with access control and a security checklist | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) |
| Choose a chat model for the `open` stack: Ollama, vLLM or OpenRouter | [docs/MODELS.md](docs/MODELS.md) |
| Look up a word these pages use | [docs/GLOSSARY.md](docs/GLOSSARY.md) |

## Acknowledgements
Special thank you to the [Lenfest Institute AI Collaborative and Fellowship Program](https://www.lenfestinstitute.org/institute-news/lenfest-institute-openai-microsoft-ai-collaborative-fellowship/) for making this project happen.
- Patrick Kerkstra, Ross Maghielse - newsroom guidance, support, and tester recruiting
- Tommy Rowan, Nick Vidala, Jennifer Friedman-Perez - Alpha testing users
- Lenfest Institute - for providing and securing the grant that made this project possible
- Microsoft - for co-funding the grant and jumpstarting our progress
- OpenAI - for co-funding the grant and providing technical support

## Contributing

All contributions are welcome! More details to come. In the meantime, please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

### Running the tests

The same checks run on every pull request and push to main (`.github/workflows/tests.yml`, on Ubuntu with Python 3.11, 3.12 and 3.13). They need no API keys, network access or database. Run them from the Dewey folder with the virtual environment on.

1. **Install Dewey's packages and pexpect.** Without pexpect, the `tests/install/` tests that drive the setup wizard are skipped.
   ```bash
   pip install -r requirements.txt pexpect
   ```
2. **Check the code with ruff.** ruff is pinned in `requirements.txt`, so the step above installs it.
   ```bash
   ruff check app/ install.py tests/ --select F
   ```
3. **Run the tests.** Keep `-t .`: without it, `tests/importers` and `tests/mcp` take the place of Dewey's `app/importers` and the `mcp` package.
   ```bash
   python -m unittest discover -s tests -t .
   ```

<details><summary>Postgres tests (skipped by default)</summary>

The Postgres filter tests in `tests/providers/` are skipped unless `DEWEY_TEST_DATABASE_URL` points at a throwaway Postgres with pgvector. They build Dewey's tables in a schema of their own, adding the `vector` extension there if it's missing. They drop the schema afterwards, so an existing index is untouched. Replace PASSWORD with your test database's password:

```bash
DEWEY_TEST_DATABASE_URL='postgresql://postgres:PASSWORD@localhost:5432/postgres' python -m unittest tests.providers.test_pgvector_store
```

</details>

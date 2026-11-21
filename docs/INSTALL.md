# Install Dewey on your computer

**In short:** This guide takes you from a computer without Dewey to Dewey answering questions about your archive, on its [demo page](GLOSSARY.md#demo-page) and in your AI tool. You install [Python](GLOSSARY.md#python) and [git](GLOSSARY.md#git) and download Dewey; the [setup wizard](GLOSSARY.md#setup-wizard) does most of the rest.

**Who this is for:** anyone who has typed a few commands in a [terminal](GLOSSARY.md#terminal) but doesn't do it every day.

**You'll need:** a Mac, Windows PC or Linux computer you can install software on, an internet connection, and the accounts your [setup](GLOSSARY.md#setup) needs ([details](#what-you-need)).

**Time:** about an hour (an estimate), plus downloads, importing and [indexing](GLOSSARY.md#indexing).

> [!NOTE]
> **How this guide has been tested (November 2026).** Mac: Dewey has been installed and run on real articles, though not by following every step of this guide. Linux: Dewey's automated tests run on Ubuntu; nobody has run this install end to end. Windows: not tested; those steps follow each tool's own documentation.

**On this page:**

- [Before you start](#before-you-start)
- [Get your Mac ready](#get-your-mac-ready), [Get your Windows PC ready](#get-your-windows-pc-ready) or [Get your Linux computer ready](#get-your-linux-computer-ready)
- [Get the Dewey code](#get-the-dewey-code)
- [Create a virtual environment](#create-a-virtual-environment)
- [Run the setup wizard](#run-the-setup-wizard)
- [Check that it worked](#check-that-it-worked)
- [Connect an AI tool](#connect-an-ai-tool)
- [Start Dewey again later](#start-dewey-again-later)
- [Change your settings](#change-your-settings)
- [Update Dewey](#update-dewey)
- [Troubleshooting](#troubleshooting)
- [Where to go next](#where-to-go-next)

## Before you start

### What you need

- **A computer you can install software on,** with administrator rights (your password): a Mac, a Windows 10 or 11 PC, or Linux (Ubuntu 24.04 or 26.04, Debian 13, Fedora 44 or 45). On a computer your IT team manages, you may need their help.
- **The accounts your setup needs** ([STACKS.md](STACKS.md#the-setups-at-a-glance)): [Anthropic API](GLOSSARY.md#anthropic-api) for the Claude setups, billed per [token](GLOSSARY.md#token-billing); MongoDB Atlas, with a payment method, for [Voyage AI](GLOSSARY.md#voyage-ai); [OpenRouter](GLOSSARY.md#openrouter) or Azure for those setups.
- **Your archive, or a way to reach it:** a WordPress site or export file, a feed, a CSV spreadsheet, or a Drupal or Ghost site. [IMPORTING.md](IMPORTING.md#choose-an-importer) says which [importer](GLOSSARY.md#importer) fits.
- **Optional: an AI tool that supports [MCP](GLOSSARY.md#mcp),** such as Claude Code or Claude Desktop.
- **Free disk space:** about 0.5 GB to 19 GB, depending on your setup (estimates; details below).
- **For the fully open setup, a capable computer,** such as an Apple silicon Mac with 32 GB of memory (an estimate; [STACKS.md](STACKS.md#fully-open-on-your-own-computer)).

<details><summary>Disk space for each setup</summary>

Estimates, added up from this guide's download sizes:

| Setup | Disk space, about |
|---|---|
| [Azure](STACKS.md#azure), or [Claude with Voyage AI](STACKS.md#claude-with-voyage-ai) | 0.5 GB |
| [Claude with local search models](STACKS.md#claude-with-local-search-models), or [Open-weight models through OpenRouter](STACKS.md#open-weight-models-through-openrouter) | 4.5 GB |
| [Fully open on your own computer](STACKS.md#fully-open-on-your-own-computer) | 10 GB with the qwen3:8b [chat model](GLOSSARY.md#chat-model), 19 GB with gpt-oss:20b |

Add about 0.7 GB for a database in Docker, plus Docker Desktop (Docker doesn't publish its size), and 1.9 GB on a Mac without Apple's developer tools.

</details>

### How to use this guide

- **Copy one grey box at a time** with its copy button (top right, on GitHub). Paste it into your terminal (Command+V on a Mac, Ctrl+V in Windows Terminal, Ctrl+Shift+V on Linux) and press Enter (Return on a Mac).
- **Wait for the prompt before the next box:** the line ending in `%` on a Mac, `$` on Linux or `>` in Windows [PowerShell](GLOSSARY.md#powershell).
- **Use the boxes labelled for your computer.** Unlabelled ones work everywhere.
- **Replace words in CAPITALS**, such as `PASSWORD_FROM_WIZARD`, with your own value before pressing Enter: after pasting, move back with the left arrow key and replace it. Don't type the "You should see" boxes.
- **Control+C stops a running command.** Password prompts show nothing as you type.

<details><summary>Why the boxes have no notes</summary>

On a Mac, Terminal runs a [shell](GLOSSARY.md#shell) called zsh, the default since macOS 10.15 ([Apple](https://support.apple.com/en-us/102360), checked November 2026). It doesn't treat `#` as the start of a note, so a note pasted after a command reaches the command as extra words. It also gives `[ ]`, `?`, `!` and `< >` special meanings, so the boxes quote those or leave them out. Linux terminals run bash, which accepts the same boxes.

</details>

## Get your Mac ready

Do these steps in order. If a check shows you already have something, skip to the step it names.

1. **Open Terminal.** Press Command+Space, type `Terminal` and press Enter (or, in the Finder, double-click Terminal in Applications, then Utilities). The prompt looks like `you@your-Mac ~ %`.

2. **Check which Mac you have.**
   ```bash
   uname -m
   ```
   `arm64` means Apple silicon (M1 or later), `x86_64` an Intel Mac. Intel Macs can't install Dewey's local [search models](GLOSSARY.md#search-models), so they need Azure or Claude with Voyage AI. Your macOS version, which later steps need:
   ```bash
   sw_vers -productVersion
   ```
   It prints a number such as `26.6.2`.

3. **Check for Python 3.13.**
   ```bash
   python3.13 --version
   ```
   `Python 3.13.17` or later: skip to step 7. An older 3.13, or `zsh: command not found: python3.13`: go on to step 4. Ignore a `python3` that says `Python 3.9.6`: it's Apple's own, too old for Dewey.

<details><summary>Already use Homebrew?</summary>

[Homebrew](GLOSSARY.md#homebrew) can replace steps 4 to 6. On Apple silicon with macOS 15 or later, it installs a ready-made Python 3.13.18, with every security fix so far:

```bash
brew install python@3.13
```

Plain `brew install python` gives Python 3.15, which can't install Dewey yet. Homebrew's Python normally needs no certificates step (*unverified* on a fresh install). Outside a [virtual environment](GLOSSARY.md#virtual-environment), its [pip](GLOSSARY.md#pip) refuses to install packages (`externally-managed-environment`), which is expected. Then skip to step 7.

</details>

4. **Download Python 3.13.** On Python's [3.13.17 release page](https://www.python.org/downloads/release/python-31317/), click **Download macOS installer** (`python-3.13.17-macos11.pkg`, about 70 MB, for macOS 10.13 or later). Not the big Download button on python.org's front page: it gives Python 3.15, which can't install Dewey yet.

5. **Run the installer, then the certificates step.** Open the downloaded file, click Continue, Agree and Install, and type your Mac password (you need an administrator account). In the Python 3.13 folder that opens, double-click **Install Certificates.command**. A Terminal window opens; wait until it shows something like:
   ```text
    -- pip install --upgrade certifi
   ...
    -- update complete
   ```
   Without this step, importing from your website fails with `CERTIFICATE_VERIFY_FAILED`.

6. **Check Python in a new window.** Quit Terminal (Command+Q), open it again and run:
   ```bash
   python3.13 --version
   ```
   You should see:
   ```text
   Python 3.13.17
   ```
   This is the last 3.13 release with a Mac installer (October 2026). Its pip (26.2.2) and OpenSSL (3.5.9, which handles secure connections) are current. It lacks one later fix to Python's `ssl` module, from an October 2026 certificate incident: 3.13.18 has it, but python.org offers that only as source code.

7. **Check for git.**
   ```bash
   git --version
   ```
   If a box says `The “git” command requires the command line developer tools. Would you like to install the tools now?`, click **Install**, then **Agree**. The tools download 942 MB (Apple's software-update catalog) and take 1.9 GB on disk (measured once on an Apple M3 Max with 36 GB, November 2026). If the box doesn't appear, `xcode-select --install` opens it. Then run `git --version` again. You should see something like:
   ```text
   git version 2.50.1 (Apple Git-155)
   ```

8. **Install Docker, if you want the wizard's command to start your database:** a [Postgres](GLOSSARY.md#postgres) database with [pgvector](GLOSSARY.md#pgvector), where Dewey keeps your archive. Install one of the two below. Without Docker, see the box after this list, or use a hosted database ([DEPLOYMENT.md](DEPLOYMENT.md#managed-postgres-with-pgvector)).

   **Docker Desktop**, Docker's own app, is free for personal use, education, non-commercial [open source](GLOSSARY.md#open-source) projects and businesses with "fewer than 250 employees AND less than $10 million in annual revenue". Others, including any government entity, need a paid plan ([Docker's terms](https://github.com/docker/docs/blob/main/content/manuals/desktop/setup/install/mac-install.md), checked November 2026). It needs macOS 15, 26 or 27 (the three newest, November 2026) and 4 GB of memory; its virtual machine may take up to half of yours.

   Download it from [Docker's Mac page](https://docs.docker.com/desktop/setup/install/mac-install/) (about 570 MB for Apple silicon, 630 MB for Intel). Open the downloaded `Docker.dmg`, drag Docker to Applications, open it from there and click **Accept**. Then check in a new Terminal window:
   ```bash
   docker version
   ```
   A section starting `Server:` means Docker is running.

   **Colima**, free and open source (MIT), runs Docker in a small virtual machine. Install it with Homebrew ([brew.sh](https://brew.sh)) and start it. A new Homebrew install prints directions at the end: follow them, or `brew` won't work ([Homebrew's guide](https://github.com/Homebrew/brew/blob/main/docs/Installation.md#post-installation-steps)):
   ```bash
   brew install colima docker
   ```
   ```bash
   colima start
   ```
   Then check with `docker version`.

   Tested so far: the wizard's database command, with Colima on a Mac (November 2026). Docker Desktop hasn't been tried with Dewey.

9. **Fully open setup only: install [Ollama](GLOSSARY.md#ollama),** which runs the chat model on the Mac's [GPU](GLOSSARY.md#gpu). Download it from [ollama.com/download](https://ollama.com/download) (about 230 MB; macOS 14 or newer) and open it once; it should set up the `ollama` command (*unverified*: check with `ollama -v`). You'll set its context length in [What the wizard asks](#what-the-wizard-asks).

<details><summary>Postgres without Docker on a Mac</summary>

Two free options, neither tested with Dewey. Use PostgreSQL 18, the version Dewey was tested with in its Docker image.

With Homebrew, on Apple silicon Macs only (Homebrew no longer builds packages for Intel Macs):

```bash
brew install postgresql@18 pgvector
```

```bash
brew services start postgresql@18
```

```bash
/opt/homebrew/opt/postgresql@18/bin/createdb dewey
```

`brew install pgvector` alone isn't enough: it adds pgvector only to `postgresql@18` and `postgresql@19`, and doesn't install Postgres. Plain `postgresql` is now version 19, which Dewey hasn't been tested with. `brew services start` starts Postgres now and at every login.

With Postgres.app: download "Postgres.app with PostgreSQL 18" from [postgresapp.com](https://postgresapp.com/downloads.html) (about 123 MB; the default download, PostgreSQL 19, is untested). Drag it to Applications, open it and click Initialize; pgvector is included. Then create Dewey's database, and allow Dewey when Postgres.app first asks:

```bash
/Applications/Postgres.app/Contents/Versions/latest/bin/createdb dewey
```

For both: in the wizard, answer `yes` at the Postgres question and enter `postgresql://localhost:5432/dewey` (no password: these builds trust connections from your own Mac). Run only one Postgres on [port](GLOSSARY.md#port) 5432: the Docker database, Homebrew's Postgres and Postgres.app all use it. After a restart, open Postgres.app again, or turn on its setting to start at login.

</details>

<details><summary>Sources</summary>

All checked November 2026:

- Python's [3.13.17 release page](https://www.python.org/downloads/release/python-31317/) and its [Mac guide](https://github.com/python/cpython/blob/3.14/Doc/using/mac.rst), for the installer screens and Install Certificates.
- Homebrew's [python@3.13](https://formulae.brew.sh/formula/python@3.13) and its [requirements](https://github.com/Homebrew/brew/blob/main/docs/Installation.md).
- Apple's [Terminal guide](https://support.apple.com/guide/terminal/open-or-quit-terminal-apd5265185d-f365-44cb-8b09-71a064a42125/mac).
- Docker's [Mac install page and license](https://github.com/docker/docs/blob/main/content/manuals/desktop/setup/install/mac-install.md). Its paid plans cost $9 (Pro), $15 (Team) or $24 (Business) per user per month, billed annually ([Docker pricing](https://www.docker.com/pricing/)).
- Colima's defaults: 2 CPUs, 2 GiB of memory and 100 GiB of disk ([Colima](https://github.com/abiosoft/colima)).
- [Ollama for macOS](https://github.com/ollama/ollama/blob/main/docs/macos.mdx).
- pgvector's [Homebrew note](https://github.com/pgvector/pgvector/blob/master/README.md#homebrew) and [Postgres.app](https://github.com/PostgresApp/PostgresApp).
- Intel Macs: the newest [PyTorch](GLOSSARY.md#pytorch) for them is 2.2.2 ([PyTorch's index](https://download.pytorch.org/whl/cpu/torch/)), and Dewey's local models need 2.9 or later ([voyageai 0.6.0](https://pypi.org/pypi/voyageai/0.6.0/json)).

</details>

## Get your Windows PC ready

> [!NOTE]
> **Not tested by us** (November 2026). These steps follow Microsoft's, Python's, Git's and Docker's documentation (Sources, below). If something doesn't match what you see, tell the maintainers ([issue list](https://github.com/TeamTrace/dewey-ai/issues)).

These steps are for Windows 11 and Windows PowerShell, with Windows 10 differences noted; some need an administrator once. **On Windows on Arm** (such as Snapdragon PCs), the Arm version of Python can't install seven of Dewey's packages; the [WSL](GLOSSARY.md#wsl) route below might work (every package has a Linux Arm version), but nobody has tried it.

1. **Open PowerShell.** Click Start, type `PowerShell` and open **Windows PowerShell** (Windows 11 opens it in Windows Terminal; Windows 10's older console works too). Paste with Ctrl+V. The prompt looks like `PS C:\Users\you>`. Use it for every Windows box, not Command Prompt or Git Bash.

2. **Check for Python 3.13.**
   ```powershell
   py -3.13 --version
   ```
   `Python 3.13.17` or a later 3.13: skip to step 4. `is not recognized` or `No suitable Python runtime found`: go on to step 3.

3. **Install Python 3.13.** On Python's [3.13.17 release page](https://www.python.org/downloads/release/python-31317/), under Files, skip the **Download Python install manager** button and download **Windows installer (64-bit)** (`python-3.13.17-amd64.exe`, about 30 MB) from the table below it. Not python.org's big front-page button (Python 3.15), nor the Microsoft Store version (it has no `py` command). On the installer's first screen:
   - Tick **Add python.exe to PATH**, so PowerShell finds Python (see [path](GLOSSARY.md#path)).
   - If you aren't an administrator, untick **Use admin privileges when installing py.exe**.
   - Click **Install Now**.

   At **Installation was successful**, close it and check again in a new PowerShell window:
   ```powershell
   py -3.13 --version
   ```
   You should see:
   ```text
   Python 3.13.17
   ```
   It already has the pip fix for the October 2026 certificate incident and uses Windows' certificates: no extra step.

4. **Check for git.**
   ```powershell
   git --version
   ```
   If it says `is not recognized`, install Git for Windows from [git-scm.com](https://git-scm.com/install/windows) (about 67 MB; accept every default) or with winget (a package the Git for Windows project doesn't maintain):
   ```powershell
   winget install --id Git.Git -e --source winget
   ```
   Then check again in a new PowerShell window.

5. **Let PowerShell run the virtual environment's script,** which Windows blocks by default. This allows scripts written on your own computer, for your account only, without an administrator:
   ```powershell
   Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
   ```
   At `Do you want to change the execution policy?` (default: No), type `Y` and press Enter. `overridden by a policy defined at a more specific scope` means your organization blocks this ([Troubleshooting](#troubleshooting)).

6. **Install Docker Desktop, if you want the wizard's command to start your database.** It's free for personal use, education, non-commercial open source projects and businesses under 250 employees and $10 million in annual revenue; others need a paid subscription ([Docker's terms](https://github.com/docker/docs/blob/main/content/manuals/desktop/setup/install/windows-install.md), checked November 2026).

   Docker lists Windows 11 23H2 or later, or Windows 10 22H2, in the Enterprise, Pro or Education editions. Home was listed until October 2025, and other Docker pages still mention it, so whether Docker supports Home today is unclear. It also needs WSL 2 (an administrator allows it once), hardware virtualization and 8 GB of memory.

   Download it from [Docker's Windows page](https://docs.docker.com/desktop/setup/install/windows-install/) (about 625 MB) and run it. Then open it from Start (it doesn't start by itself), click **Accept**, and check in a new PowerShell window:
   ```powershell
   docker version
   ```
   A section starting `Server:` means Docker is running.

7. **Fully open setup only: install Ollama** from [ollama.com/download](https://ollama.com/download) (about 1.9 GB; no administrator needed) or with winget:
   ```powershell
   winget install --id Ollama.Ollama -e --source winget
   ```
   It runs in the notification area, by the clock, and starts each time you sign in. Check in a new PowerShell window that `ollama -v` prints a version. You'll set its context length in [What the wizard asks](#what-the-wizard-asks).

<details><summary>Free alternatives to Docker Desktop</summary>

Neither has been tested with Dewey.

- **Rancher Desktop** (Apache 2.0) supports Windows 11, including Home (its page doesn't list Windows 10), and needs WSL installed first. Choose the **dockerd (moby)** container engine, which gives you the `docker` command the wizard looks for. Kubernetes is on by default; Dewey doesn't need it, and turning it off saves memory.
- **Podman Desktop** (Apache 2.0) supports Windows 10 build 19043 or later and Windows 11, and needs administrator rights. It has no `docker` command for the wizard to find, so start the database yourself: `podman run` with the wizard's options and the image `docker.io/pgvector/pgvector:pg18`. Then answer `yes` at the wizard's Postgres question and enter `postgresql://dewey:YOURPASSWORD@127.0.0.1:5432/dewey`, with letters and digits only in YOURPASSWORD.

</details>

<details><summary>Postgres without Docker on Windows</summary>

- **A hosted database** is the most direct route: Neon's and Supabase's free plans include pgvector ([DEPLOYMENT.md](DEPLOYMENT.md#managed-postgres-with-pgvector)). Answer `yes` at the wizard's Postgres question and paste its address.
- **Postgres inside WSL** works too: follow the Linux steps inside Ubuntu; Windows programs reach it at `localhost`.
- **Postgres on Windows itself** is hard: pgvector has no Windows installer, and its own instructions compile it with Visual Studio's C++ tools, as an administrator.

</details>

<details><summary>Prefer WSL?</summary>

WSL runs Linux inside Windows, so you follow the [Linux steps](#get-your-linux-computer-ready) instead. It's also the route where every Dewey package has a version for Windows on Arm. Not tested by us. In PowerShell opened with **Run as administrator**:

```powershell
wsl --install
```

Restart the computer, open **Ubuntu** from Start, create a Linux user name and password, and follow the Linux steps inside Ubuntu. Keep Dewey in your Linux home folder, not under `/mnt/c`. `wsl --install` installs Ubuntu 26.04, which comes with Python 3.14, so get Python 3.13 as the Ubuntu 26.04 steps describe.

If Docker Desktop is installed on Windows, its `docker` command works inside Ubuntu by itself, so skip Linux steps 5 and 6; whether Dewey inside Ubuntu can reach the database it publishes on `127.0.0.1:5432` is *unverified*. Without Docker Desktop, Linux step 5 installs the free Docker Engine inside Ubuntu instead. Don't install both: Docker says "Running both can cause conflicts" ([Docker's WSL page](https://github.com/docker/docs/blob/main/content/manuals/desktop/features/wsl/_index.md)). Ollama for Windows listens only on Windows' own `127.0.0.1`, so Dewey inside Ubuntu can't reach it unless Windows 11 runs WSL in "mirrored" networking mode; otherwise, install Ollama inside Ubuntu. If WSL says `0x80370102`, virtualization is turned off in your computer's BIOS or UEFI settings.

</details>

<details><summary>Sources</summary>

All checked November 2026:

- Python's [Windows guide](https://github.com/python/cpython/blob/3.13/Doc/using/windows.rst) and [venv guide](https://github.com/python/cpython/blob/3.13/Doc/library/venv.rst).
- Microsoft's pages on [execution policies](https://github.com/MicrosoftDocs/PowerShell-Docs/blob/main/reference/5.1/Microsoft.PowerShell.Core/About/about_Execution_Policies.md), [Windows Terminal](https://github.com/microsoft/terminal/blob/main/README.md) and [installing WSL](https://github.com/MicrosoftDocs/WSL/blob/main/WSL/install.md).
- [Git for Windows](https://git-scm.com/install/windows).
- Docker's [Windows install page](https://github.com/docker/docs/blob/main/content/manuals/desktop/setup/install/windows-install.md).
- [Ollama for Windows](https://github.com/ollama/ollama/blob/main/docs/windows.mdx).
- [Rancher Desktop](https://github.com/rancher-sandbox/docs.rancherdesktop.io/blob/main/docs/getting-started/installation.md), [Podman Desktop](https://github.com/podman-desktop/podman-desktop/blob/main/website/docs/installation/windows-install/index.mdx) and [pgvector](https://github.com/pgvector/pgvector/blob/master/README.md).

</details>

## Get your Linux computer ready

These steps are for Ubuntu 24.04 and 26.04, Debian 13, and Fedora 44 and 45, in a terminal running bash. *Not tested by us on Linux:* the automated tests run on Ubuntu, but nobody has followed these steps end to end. Linux Mint and Pop!_OS can follow the Ubuntu steps (Docker supports them only unofficially).

1. **Open a terminal.** Press the Super (Windows logo) key, type `Terminal` and press Enter. Paste with Ctrl+Shift+V, or right-click and choose Paste. The prompt ends in `$`. To see which Linux and version you have:
   ```bash
   grep PRETTY_NAME /etc/os-release
   ```
   You should see something like `PRETTY_NAME="Ubuntu 24.04.5 LTS"`.

2. **Update the system,** which also brings the October 2026 certificate fixes. `sudo` runs a command as the computer's administrator. It asks for your own password, which doesn't show as you type. Never put it in front of Dewey's commands. On Ubuntu or Debian, one at a time (press Enter or `Y` if apt asks):
   ```bash
   sudo apt update
   ```
   ```bash
   sudo apt upgrade
   ```
   On Fedora, answer `y` whenever dnf asks `Is this ok [y/N]:` (Enter alone cancels):
   ```bash
   sudo dnf upgrade
   ```

<details><summary>Debian says "is not in the sudoers file"</summary>

That happens when a root password was set while installing Debian. Run these one at a time: the first asks for the root password, and in the third, replace YOURUSERNAME with your own user name.

```bash
su --login
```

```bash
apt install sudo
```

```bash
adduser YOURUSERNAME sudo
```

```bash
exit
```

Then log out of your desktop and log back in.

</details>

3. **Install Python.** Dewey works with Python 3.11 to 3.13. Don't use plain `python3` on Ubuntu 26.04 or Fedora: it's 3.14 on Ubuntu 26.04 and Fedora 44 (three packages must build from source) and 3.15 on Fedora 45 (it can't install Dewey).

   On Ubuntu 24.04, use Ubuntu's own Python 3.12:
   ```bash
   sudo apt install python3.12-venv
   ```
   ```bash
   python3.12 --version
   ```
   You should see `Python 3.12.3`. Wherever this guide says `python3.13`, type `python3.12`.

   On Ubuntu 26.04, Python 3.13 comes from deadsnakes, a community archive whose page warns of "no guarantee of timely updates in case of security problems" ([deadsnakes](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa)). Press Enter when the first command asks you to continue.
   ```bash
   sudo add-apt-repository ppa:deadsnakes/ppa
   ```
   ```bash
   sudo apt install python3.13 python3.13-venv
   ```
   ```bash
   python3.13 --version
   ```
   You should see `Python 3.13.` and a number (3.13.18 in November 2026).

   On Debian 13:
   ```bash
   sudo apt install python3.13-venv
   ```
   ```bash
   python3.13 --version
   ```
   You should see `Python 3.13.5`.

   On Fedora 44 or 45, Fedora's extra `python3.13` package, which Fedora calls "intended solely for development" ([Fedora](https://developer.fedoraproject.org/tech/languages/python/multiple-pythons.html)):
   ```bash
   sudo dnf install python3.13
   ```
   ```bash
   python3.13 --version
   ```
   You should see `Python 3.13.18`.

<details><summary>Another way to get Python 3.13: uv</summary>

uv is a free Python tool that can install Python 3.13 on any Linux, including Ubuntu 26.10, where deadsnakes has no build ([uv's guide](https://github.com/astral-sh/uv/blob/main/docs/guides/install-python.md)). Its Pythons haven't been tested with Dewey; in particular, nobody has checked how their certificates behave with Dewey's importers.

This uses `curl`, which Ubuntu doesn't install by default: on Ubuntu, run step 4's `sudo apt install git curl zstd` first.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new terminal window, then:

```bash
uv python install 3.13
```

This puts a `python3.13` command in `~/.local/bin`. If the terminal can't find it, run `uv python update-shell` and open a new window.

</details>

4. **Install git and two download tools:** `curl` for the Ollama and Claude Code installers, and `zstd` for Ollama's. On Ubuntu or Debian:
   ```bash
   sudo apt install git curl zstd
   ```
   On Fedora:
   ```bash
   sudo dnf install git zstd
   ```
   Then check:
   ```bash
   git --version
   ```

> [!WARNING]
> **Two Docker security points on Linux.** Use Docker's own archive (step 5), not Debian's `docker.io` package (version 26.1.5): Docker before 28.0 can let other computers on your local network reach ports published on 127.0.0.1, including your database ([why](DEPLOYMENT.md#why-docker-engine-280-or-newer)). And Docker says the `docker` group, which step 6 adds you to, "grants root-level privileges to the user."

5. **Install Docker Engine, if you want the wizard's command to start your database.** It's free and open source (Apache 2.0). Run these one at a time.

   On Ubuntu or Debian, first remove your distribution's own Docker packages (lines saying `dpkg: no packages found matching` are fine):
   ```bash
   sudo apt remove $(dpkg --get-selections docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc | cut -f1)
   ```
   Then, on Ubuntu or Debian:
   ```bash
   sudo apt install ca-certificates curl
   ```
   ```bash
   sudo install -m 0755 -d /etc/apt/keyrings
   ```
   On Ubuntu:
   ```bash
   sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
   ```
   On Debian:
   ```bash
   sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
   ```
   Then, on both:
   ```bash
   sudo chmod a+r /etc/apt/keyrings/docker.asc
   ```
   Next, paste only your system's box, all at once, and press Enter. **On Ubuntu only:**
   ```bash
   sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
   Types: deb
   URIs: https://download.docker.com/linux/ubuntu
   Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
   Components: stable
   Architectures: $(dpkg --print-architecture)
   Signed-By: /etc/apt/keyrings/docker.asc
   EOF
   ```
   **On Debian only:**
   ```bash
   sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
   Types: deb
   URIs: https://download.docker.com/linux/debian
   Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
   Components: stable
   Architectures: $(dpkg --print-architecture)
   Signed-By: /etc/apt/keyrings/docker.asc
   EOF
   ```
   It prints the lines back. Then, on both:
   ```bash
   sudo apt update
   ```
   ```bash
   sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
   ```
   Docker then starts by itself, now and whenever the computer starts.

   On Fedora:
   ```bash
   sudo dnf config-manager addrepo --from-repofile https://download.docker.com/linux/fedora/docker-ce.repo
   ```
   ```bash
   sudo dnf install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
   ```
   If dnf asks about a key, check that its `Fingerprint:` line shows `060A61C51B558A7F742B77AAC52FEB6B621E9F35`, then type `y`. This starts Docker now and whenever the computer starts:
   ```bash
   sudo systemctl enable --now docker
   ```
   On every distribution, check it:
   ```bash
   sudo docker run hello-world
   ```
   You should see:
   ```text
   Hello from Docker!
   ```

6. **Let your account use Docker without sudo,** because the wizard's database command doesn't use sudo:
   ```bash
   sudo usermod -aG docker $USER
   ```
   Log out and back in (restart instead, in a virtual machine), then check:
   ```bash
   docker run hello-world
   ```
   To stay out of the `docker` group (see the warning), skip this step and put `sudo ` in front of every docker command, the wizard's too.

7. **Fully open setup only: install Ollama.** Don't type `sudo` in front: the script uses sudo itself.
   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ```
   It asks for your password and downloads about 1.6 GB. With an NVIDIA card but no NVIDIA driver, it may install the driver and ask you to restart. Check:
   ```bash
   ollama -v
   ```
   You should see something like `ollama version is 0.40.16`. Ollama starts with the computer. You'll set its context length in [What the wizard asks](#what-the-wizard-asks).

<details><summary>Docker Desktop for Linux (optional)</summary>

Docker Desktop exists for Linux only on x86_64 computers with a GNOME, KDE or MATE desktop and KVM virtualization. It runs Docker inside a virtual machine, so containers made with Docker Engine don't appear in it. Its page lists Debian 12 but not Debian 13. Its license is the Mac's: paid for organizations with more than 250 employees or more than $10 million in annual revenue ([Docker's Linux page](https://github.com/docker/docs/blob/main/content/manuals/desktop/setup/install/linux/_index.md)). Set up Docker's archive as in step 5, then follow that page.

</details>

<details><summary>Postgres without Docker on Linux</summary>

Use PostgreSQL 18 with pgvector 0.8 or newer. Ubuntu 24.04's own pgvector is 0.6.0, which makes date and author filters return fewer articles, so these steps use PostgreSQL's own package archive. Not tested by us.

On Ubuntu or Debian, run these one at a time. Press Enter when the second says "Press Enter to continue, or Ctrl-C to abort."

```bash
sudo apt install -y postgresql-common
```

```bash
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh
```

```bash
sudo apt install postgresql-18 postgresql-18-pgvector
```

On Ubuntu 26.04, the last command works without the first two (Ubuntu's own packages there are PostgreSQL 18.6 and pgvector 0.8.1). `pg_lsclusters` shows which port the new database uses; if it isn't 5432, use that number in the address below.

On Fedora:

```bash
sudo dnf install postgresql-server pgvector
```

```bash
sudo env PGSETUP_INITDB_OPTIONS='--auth-host=scram-sha-256' postgresql-setup --initdb
```

```bash
sudo systemctl enable --now postgresql
```

The `PGSETUP_INITDB_OPTIONS` part makes Postgres accept the password Dewey sends; without it, Fedora refuses Dewey with `Ident authentication failed for user "dewey"`.

Then, on every distribution, create Dewey's user and database. Make up a password of letters and digits only; you type it twice, and nothing appears as you type.

```bash
sudo -u postgres createuser --pwprompt dewey
```

```bash
sudo -u postgres createdb --owner=dewey dewey
```

```bash
sudo -u postgres psql -d dewey -c 'CREATE EXTENSION IF NOT EXISTS vector'
```

It should print `CREATE EXTENSION`. In the wizard, answer `yes` at the Postgres question and enter `postgresql://dewey:YOURPASSWORD@127.0.0.1:5432/dewey`, with your password in place of YOURPASSWORD. This Postgres starts by itself with the computer. Run only one Postgres on port 5432: this one or the wizard's Docker database.

</details>

<details><summary>Sources</summary>

All checked November 2026:

- Docker's install pages for [Ubuntu](https://github.com/docker/docs/blob/main/content/manuals/engine/install/ubuntu.md), [Debian](https://github.com/docker/docs/blob/main/content/manuals/engine/install/debian.md) and [Fedora](https://github.com/docker/docs/blob/main/content/manuals/engine/install/fedora.md), and its [steps after installing](https://github.com/docker/docs/blob/main/content/manuals/engine/install/linux-postinstall.md).
- [git for Linux](https://git-scm.com/install/linux) and the [deadsnakes archive](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa).
- [Ollama for Linux](https://github.com/ollama/ollama/blob/main/docs/linux.mdx).
- PostgreSQL's [apt archive](https://www.postgresql.org/download/linux/ubuntu/) and Debian's [sudo page](https://wiki.debian.org/sudo).
- Package versions come from each distribution's package index.

</details>

## Get the Dewey code

This downloads Dewey's code (its [repository](GLOSSARY.md#repository)) into `dewey-ai` in your home folder: the [Dewey folder](GLOSSARY.md#dewey-folder).

1. **Go to your home folder.**

   On macOS and Linux:
   ```bash
   cd ~
   ```
   On Windows (PowerShell):
   ```powershell
   cd $HOME
   ```

2. **Download Dewey.**
   ```bash
   git clone https://github.com/TeamTrace/dewey-ai.git
   ```
   You should see something like:
   ```text
   Cloning into 'dewey-ai'...
   ```

3. **Go into the Dewey folder,** where every command from here on runs, and list what's in it:
   ```bash
   cd dewey-ai
   ```
   ```bash
   ls
   ```
   You should see something like this (Windows shows a table):
   ```text
   CLAUDE.md  Dockerfile  LICENSE  README.md  app  data  deploy  docker-compose.yml  docs  install.py ...
   ```

Your Dewey folder is now `/Users/you/dewey-ai` on a Mac, `/home/you/dewey-ai` on Linux or `C:\Users\you\dewey-ai` on Windows, with your user name in place of `you`, as in this guide's other examples. `open .` shows it in the Finder (`xdg-open .` on Linux).

On Windows, keep it there: not in Documents or on the Desktop, which may sync to OneDrive (*unverified on your PC*), and not in a shared folder. The file with your keys gets its folder's permissions.

## Create a virtual environment

A [virtual environment](GLOSSARY.md#virtual-environment), the `.venv` folder, keeps Dewey's Python and packages apart from the rest of your computer. You make it once, then turn it on in each new terminal window.

1. **Create it.** It takes a few seconds and prints nothing.

   On macOS, Debian, Fedora and Ubuntu 26.04:
   ```bash
   python3.13 -m venv .venv
   ```
   On Ubuntu 24.04:
   ```bash
   python3.12 -m venv .venv
   ```
   On Windows (PowerShell):
   ```powershell
   py -3.13 -m venv .venv
   ```
   On Linux, an error that mentions `ensurepip` means the venv package is missing ([Troubleshooting](#troubleshooting)).

2. **Turn it on.**

   On macOS and Linux:
   ```bash
   source .venv/bin/activate
   ```
   On Windows (PowerShell):
   ```powershell
   .venv\Scripts\Activate.ps1
   ```
   The prompt now starts with `(.venv)`. On Windows, `running scripts is disabled on this system` means step 5 of [Get your Windows PC ready](#get-your-windows-pc-ready) still needs doing.

3. **Check the Python version.** From now on, type `python`, not `python3.13`.
   ```bash
   python --version
   ```
   You should see something like:
   ```text
   Python 3.13.17
   ```

4. **On Linux, update pip.** New environments on Ubuntu 24.04, Debian and Fedora have a pip older than 26.2.2, the version Python's October 2026 [security advisory](https://discuss.python.org/t/information-for-python-users-regarding-emudhra-emsign-ca-certificate-security-incident/109576) asks for. Running this where pip is new enough does no harm; Macs and Windows already have 26.2.2.
   ```bash
   python -m pip install "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"
   ```
   You should see something like:
   ```text
   Successfully installed pip-26.2.2
   ```

`deactivate` turns the environment off.

### Turn it on in every new terminal window

The environment is on only in the window where you turned it on. In each new window, run these one at a time. On macOS and Linux:

```bash
cd ~/dewey-ai
```

```bash
source .venv/bin/activate
```

On Windows (PowerShell):

```powershell
cd $HOME\dewey-ai
```

```powershell
.venv\Scripts\Activate.ps1
```

Signs you forgot: no `(.venv)` in the prompt, `zsh: command not found: python` (Linux: `bash: python: command not found`; Windows: `Python was not found`) or `ModuleNotFoundError`. Your AI tool doesn't need it: it starts Dewey with the environment's own Python.

## Run the setup wizard

The setup wizard writes your settings to a [.env file](GLOSSARY.md#env-file) in the Dewey folder. Secrets are typed hidden and stored only there, with a backup of any earlier file. On macOS and Linux only your account can read it; on Windows it gets the Dewey folder's permissions. From here on, commands need the virtual environment on.

1. **Choose your setup** ([below](#which-setup-to-choose)). The wizard shows where to get each key except OpenRouter's ([MODELS.md](MODELS.md#c-hosted-through-openrouter)). You can add a key later.

2. **Start the wizard** (this works in PowerShell too):
   ```bash
   python install.py
   ```
   You should see:
   ```text
     DEWEY  ·  newsroom archive setup
   ...
   This walks you through choosing a stack, getting any accounts and keys it needs,
   ```

3. **Answer each question and press Enter** ([What the wizard asks](#what-the-wizard-asks)).

`python install.py --dry-run` asks the same questions but only prints the result, with secrets masked: a [dry run](GLOSSARY.md#dry-run). It writes no `.env` and imports nothing, though a chat-model download you accept still happens. `--no-browser` suits a server without a desktop; it also skips the offer to download the chat model.

On Windows, the wizard prints a few macOS and Linux commands; the screens below give the ones to use ([a known problem](https://github.com/TeamTrace/dewey-ai/issues/25)).

### Which setup to choose

This guide doesn't choose for you: [STACKS.md](STACKS.md#the-setups-at-a-glance) describes the five setups. If someone else runs Dewey in your newsroom, ask them which you use.

The first question is the [stack](GLOSSARY.md#stack):

- `anthropic` is Claude.
- `open` is [open-weight models](GLOSSARY.md#open-weight-model) (Ollama, or a hosted service such as OpenRouter). It is not OpenAI: its two setups don't use OpenAI's own service.
- `azure` is Microsoft's [Azure OpenAI](GLOSSARY.md#azure-openai) Service with [Azure AI Search](GLOSSARY.md#azure-ai-search), not OpenAI's own API either.

Answer from your setup's row (a dash means the wizard doesn't ask):

| Setup | Which stack? | Embeddings | Reranking | Server URL |
|---|---|---|---|---|
| [Azure](STACKS.md#azure) | `azure` | – | – | – |
| [Claude with Voyage AI](STACKS.md#claude-with-voyage-ai) | `anthropic` | `voyage` | `voyage` | – |
| [Claude with local search models](STACKS.md#claude-with-local-search-models) | `anthropic` | `local` | `local` or `none` | – |
| [Fully open on your own computer](STACKS.md#fully-open-on-your-own-computer) | `open` | `local` | `local` or `none` | press Enter (Ollama) |
| [Open-weight models through OpenRouter](STACKS.md#open-weight-models-through-openrouter) | `open` | `local` | `local` or `none` | `https://openrouter.ai/api/v1` |

Only the [embedding](GLOSSARY.md#embedding) choice is costly to change later ([why](STACKS.md#indexes-are-tied-to-one-embedding-model)); [reranking](GLOSSARY.md#reranking) and the chat model can change any time. On a first run, "(current)" by `anthropic` is only the wizard's starting point.

### What the wizard asks

You see only the screens your setup needs, in this order. A bracketed value such as `[anthropic]` is the default (Enter picks it); `(Y/n)` means Enter answers yes, `(y/N)` no. Pasted secrets stay hidden: press Enter after. At a hidden question, `-` clears the saved value.

1. **The Python check.** `Dewey needs Python 3.11 to 3.13; this is 3.9.6.` means the environment uses Apple's old Python ([Troubleshooting](#troubleshooting)).

2. **Which stack?** Type the stack from [your row](#which-setup-to-choose). You should see something like:
   ```text
     1. anthropic (current): Claude writes the answers; your archive lives in Postgres. ...
   ...
   Choose a number or name [anthropic]:
   ```

3. **Claude (Anthropic API).** Create an [API key](GLOSSARY.md#api-key) in the Claude Console (https://platform.claude.com) and paste it; keys start with `sk-ant-`. Press Enter to skip it for now, or if you sign in with `ant auth login` ([STACKS.md](STACKS.md#claude-anthropic-api)).

4. **Local chat model (Ollama or another OpenAI-compatible server).**
   - **Server URL:** press Enter for Ollama on this computer (`http://localhost:11434/v1`), or type `https://openrouter.ai/api/v1`.
   - **Model name:** keep `qwen3:8b` (5.2 GB) or type `gpt-oss:20b` (13.8 GB, "needs much more memory"); on OpenRouter, an id such as `openai/gpt-oss-20b` ([MODELS.md](MODELS.md#c-hosted-through-openrouter)).
   - **Download** (only if the wizard found Ollama): type `y` to get the model now (several GB), or run `ollama pull qwen3:8b` later.
   - **API key (OpenRouter):** paste it when asked. Your questions and article [excerpts](GLOSSARY.md#excerpt) then leave your network ([STACKS.md](STACKS.md#what-the-hosted-providers-receive)).

5. **Not a wizard question: set Ollama's context length to 32k** (fully open setup) before your first question. Dewey sends about 7,100 tokens per answer. Ollama's default [context window](GLOSSARY.md#context-window) on most computers is 4,096, and it silently keeps only the end, so the model never sees Dewey's instructions, your question or the best articles.

   On a Mac or Windows, in Ollama's **Settings**, move **Context length** to **32k** (16k with 16 GB of memory; never 4k or 8k). On Linux, once the wizard has finished, run these one at a time:
   ```bash
   sudo mkdir -p /etc/systemd/system/ollama.service.d
   ```
   ```bash
   printf '[Service]\nEnvironment="OLLAMA_CONTEXT_LENGTH=32768"\n' | sudo tee /etc/systemd/system/ollama.service.d/dewey.conf
   ```
   ```bash
   sudo systemctl daemon-reload
   ```
   ```bash
   sudo systemctl restart ollama
   ```
   After your first question on the demo page, check with `ollama ps` within five minutes: CONTEXT should say 32768 (or 16384). Where Settings is, and other ways: [MODELS.md](MODELS.md#a-self-hosted-with-ollama).

6. **Search models: embeddings and reranking.** Answer from your row; `none` skips reranking.

7. **Voyage AI (MongoDB).** Paste your Voyage AI (Atlas model) key. **Before you index, add a payment method to your Atlas account.** Without one, Atlas allows 3 requests and 10,000 tokens a minute, so indexing stops with a rate-limit error after a few articles (our reading of MongoDB's limits; not tested). More: [STACKS.md](STACKS.md#voyage-ai-search-models).

8. **Postgres with pgvector (where your archive is stored and searched).** Answer `yes` and paste its address (typed hidden) if you have a database, `docker` to get a command that starts one, or `later` (Dewey can't index or answer without one). `docker` is offered only if the wizard finds Docker. If you installed Docker after opening this window, press Control+C (nothing is saved yet), open a new window, [turn the environment on](#turn-it-on-in-every-new-terminal-window) and run the wizard again.

   **If you choose `docker`:** the wizard prints four lines, starting with `docker run -d --name dewey-db`, with a password it made up. On macOS and Linux, select all four with your mouse, copy them (Command+C on a Mac; on Linux, right-click and Copy), paste them into a new terminal window (Command+N on a Mac; Ctrl+Shift+N in GNOME Terminal and Ptyxis on Linux) and press Enter.

   On Windows, that form fails in PowerShell. In a new tab (Ctrl+Shift+T; on Windows 10, a second window), paste this line, with the 24 letters and digits after `POSTGRES_PASSWORD=` in the wizard's printout in place of PASSWORD_FROM_WIZARD:
   ```powershell
   docker run -d --name dewey-db -e POSTGRES_USER=dewey -e POSTGRES_DB=dewey -e POSTGRES_PASSWORD=PASSWORD_FROM_WIZARD -p 127.0.0.1:5432:5432 pgvector/pgvector:pg18
   ```
   The first time, Docker downloads the database image (about 165 MB). You should see something like:
   ```text
   Unable to find image 'pgvector/pgvector:pg18' locally
   pg18: Pulling from pgvector/pgvector
   ...
   ```
   When one long line of letters and digits appears, check in the same window:
   ```bash
   docker logs dewey-db
   ```
   Wait until `PostgreSQL init process complete; ready for start up.` is followed by `database system is ready to accept connections` (if not, run it again a few seconds later). Then go back to the wizard's window and answer `Y` at `Is it running? (Y/n):`. **Don't answer `n`:** the next run's new password won't match this container. The wizard saves the address and password (your shell's history also keeps the pasted password). The database listens on 127.0.0.1 only, so your network can't reach it (on Linux, with Docker 28.0 or newer).

   Tested so far: this command made the November 2026 test database, with Colima on a Mac. No automated test covers it.

9. **Azure.** Its 11 questions are explained in [AZURE.md](AZURE.md#with-the-setup-wizard), which also covers importing ([AZURE.md](AZURE.md#prepare-your-articles)).

> [!WARNING]
> **Don't leave the newsroom details blank.** Blank answers keep the defaults of The Philadelphia Inquirer: prompts assume an archive from January 2, 1978, and the Greater Philadelphia Region. Articles indexed without a [publisher](GLOSSARY.md#publisher) field are labelled as the Inquirer's, and your AI tool is told it searches that archive. If this happened, fill in the name and index again (indexing replaces the label).

10. **Newsroom details (used in Dewey's prompts).** Type your newsroom's name, the earliest article date in your archive, and your region (`none` for a national outlet).

11. **Connect AI tools (MCP).** The wizard saves a secret [bearer token](GLOSSARY.md#bearer-token) for Dewey's [MCP server](GLOSSARY.md#mcp-server) in `.env`, without showing it; a re-run asks `Keep the existing MCP token? (Y/n)`: press Enter. Only AI tools that connect over HTTP use it ([MCP.md](MCP.md#before-you-start-http)).

12. **Summary.** Secrets show as `'********'`, and `Still missing:` lists any settings Dewey still needs.

13. **Write this to .env? (Y/n)** Press Enter. You should see something like:
    ```text
      Backed up your previous .env to .env.bak-20261120-143000 (gitignored).
      Wrote /Users/you/dewey-ai/.env
    ```

14. **Install Python dependencies.** Press Enter at `Install requirements.txt now? (Y/n):` (the `$` lines are commands it runs itself: don't type them). For local search models, answer the double negative `Does this machine lack an NVIDIA GPU?` with `Y` on a Mac or Windows. On Linux, answer `Y` unless you have an NVIDIA RTX 20 or GTX 16 series card or newer with driver 580 or newer ([details](STACKS.md#local-search-models)).

    If it says `You're not in a virtual environment.` instead, press Enter (no), then Control+C: your answers are already in `.env`. [Turn the environment on](#turn-it-on-in-every-new-terminal-window), run the wizard again and press Enter to keep each answer.

15. **Check credentials and services** (only when nothing is missing): one line per service ([what to look for](#check-that-it-worked)). It skips a Voyage key (checking "would make a billed API call"), Azure and `ant auth login`.

16. **Get your archive into Dewey.** At `Where is your archive?`, type your source's name or number (`json`: files already in `data/`); with an empty `data/`, Enter picks `skip`. Accept the 20-article [trial run](GLOSSARY.md#trial-run), check its report and a few stories in the sample file, then choose `full` at `What next?` ([IMPORTING.md](IMPORTING.md#run-an-import)). After a finding marked `needs a look`, the wizard suggests `change` instead. On Windows, the printed `read -rs` and `export` lines work only on macOS and Linux ([the PowerShell way](IMPORTING.md#passwords-and-keys)).

    If you choose `change`, the wizard also asks for extra importer options, such as `--date-format '%m/%d/%Y'`. On Windows, type them without quotes (`--date-format %m/%d/%Y`); there, options with spaces work only if you run the importer yourself ([a known problem](https://github.com/TeamTrace/dewey-ai/issues/25)).

17. **Index your archive** (asked only when no setting is missing and `data/` has article files). **`Index them now? (y/N):` defaults to no: type `y`.** [Indexing](GLOSSARY.md#indexing) takes a while, and with Voyage AI it costs money. Answer no if you skipped installing the packages. On `azure`, the same question runs `python app/setup.py` ([AZURE.md](AZURE.md#build-the-search-index)).

18. **Next steps.** It lists what's left to do, but not indexing you turned down at step 17 ([Check that it worked](#check-that-it-worked), step 2). On Windows, its Hermes Agent example needs single quotes around your paths, not double ([a known problem](https://github.com/TeamTrace/dewey-ai/issues/25)); [MCP.md](MCP.md#client-recipes) has complete recipes.

<details><summary>Sources</summary>

The screen titles and questions come from `install.py`. The rest was checked November 2026:

- Ollama's context length: [Ollama's context-length page](https://github.com/ollama/ollama/blob/main/docs/context-length.mdx), its [server code](https://github.com/ollama/ollama/blob/v0.40.16/server/routes.go#L2570-L2586) (4,096 tokens below about 23 GiB of GPU memory), its [FAQ](https://github.com/ollama/ollama/blob/main/docs/faq.mdx) and its [Linux page](https://github.com/ollama/ollama/blob/main/docs/linux.mdx). The prompt size was measured with the models' own tokenizers.
- Voyage AI's limits without a payment method: [MongoDB's API overview](https://www.mongodb.com/docs/voyageai/api-reference/overview/).
- OpenRouter's providers (`qwen/qwen3-8b` has none today): [OpenRouter's model list](https://openrouter.ai/api/v1/models).
- The Docker messages: [moby](https://github.com/moby/moby) and the [Postgres image](https://github.com/docker-library/postgres).

</details>

### The slow steps

Most steps take seconds. Downloads, importing and the first index take longer: leave the terminal window open until each finishes.

<details><summary>Sizes and times</summary>

Your computer's section and the wizard's screens give the other download sizes.

| Step | Download (disk) | Time |
|---|---|---|
| Python packages | about 90 MB (400 MB) | about half a minute, measured once |
| PyTorch and the other local-model packages | Mac: about 190 MB more (`.venv` grows to about 1.5 GB). Linux: about 0.3 GB more with Y at the NVIDIA question, 3 GB more with N. Windows: PyTorch alone, 127 MB | about 35 seconds, measured once |
| Importing | depends on your archive | depends mostly on your site's speed (WordPress and Ghost: 100 posts per request; Drupal: 50) |
| First index, local search models | voyage-4-nano, 0.70 GB, from [Hugging Face](GLOSSARY.md#hugging-face) | about 9 minutes per 1,000 articles of about 1,300 words, measured once (embedding only; database writes not timed) |
| First question, local search models | the reranker, bge-reranker-v2-m3, 2.29 GB | the embedding model loaded in about 5 seconds (35 the very first time), measured once; the reranker's download and loading weren't timed |

"Measured once" means on an Apple M3 Max with 36 GB of memory, November 2026, with Python 3.12 and a fast connection; yours will differ. At that speed, 10,000 articles take about an hour and a half (an extrapolation). The search models download only once ([STACKS.md](STACKS.md#local-search-models)).

</details>

### If you stop halfway

- **Control+C** stops the wizard at any question (`Cancelled; nothing further was changed.`). Nothing is saved before `Write this to .env?`; after it, re-run the wizard and press Enter to keep each answer.
- **Stopping an import** leaves `data/` as it was. **Stopping the indexer** keeps finished articles, but re-running it embeds every article again (time, or money with Voyage AI).
- **Stopping after starting the Docker database,** before answering `Y`, leaves its password unsaved ([Troubleshooting](#troubleshooting): "The database container already exists").

## Check that it worked

1. **Read the wizard's check lines.** On Claude with a Docker database, you should see something like:
   ```text
     Anthropic API key: ok
     Postgres: ok, pgvector 0.8.7 available
   ```
   On the fully open setup, look for `Chat model server: ok, 'qwen3:8b' is available`. A `FAILED` or `NOT` line says what's wrong ([Troubleshooting](#troubleshooting)); re-run the wizard to check again.

2. **Make sure your archive is indexed.** The indexer's last line looks something like:
   ```text
   🎉 Indexed 41 article(s) as 188 chunk(s).
   ```
   If you skipped it, run it now. On every setup except Azure:
   ```bash
   python app/setup_postgres.py
   ```
   On Azure, run `python app/setup.py` ([AZURE.md](AZURE.md#build-the-search-index)). On a Mac, `caffeinate -i python app/setup_postgres.py` keeps it awake during a long index. More: [IMPORTING.md](IMPORTING.md#index-your-articles).

3. **Ask a question on the demo page** (next section).

### Try the demo page

The demo page is a small [Gradio](https://www.gradio.app/) chat page for trying Dewey. It has no login, so it listens only on this computer ([DEPLOYMENT.md](DEPLOYMENT.md#the-demo-page)).

> [!WARNING]
> **Each question can cost money.** On the Claude, Azure and OpenRouter setups, each question calls a paid chat model: about 4 to 12 US cents with Claude (an estimate; [MCP.md](MCP.md#cost-and-privacy)). Dewey has no budget or rate limit, so watch your provider's usage page. Voyage AI also bills a little per search.

1. **Start it:**
   ```bash
   python app/main.py
   ```
   You should see something like:
   ```text
   Dewey: Listening on 127.0.0.1, first free port from 7860 (this computer only).
   ...
   * Running on local URL:  http://127.0.0.1:7860
   * To create a public link, set `share=True` in `launch()`.
   ```
   Ignore the `share=True` line: Dewey keeps public links off. Leave the window open.

2. **Open the address** on the `Running on local URL` line, usually http://127.0.0.1:7860 (7861 or higher if that port is busy).

3. **Ask questions you already know the answers to,** with a real byline in place of Jane Doe:
   - "What has Jane Doe written about school funding since 2020?"
   - "Summarize our coverage of the city budget."

   Each [citation](GLOSSARY.md#citation) links to an article: check a few before you rely on Dewey. A [byline filter](GLOSSARY.md#byline-filter) needs the whole name ([MCP.md](MCP.md#the-two-tools)). **Generating metadata**, above the answer, shows the search text, dates and bylines Dewey used.

4. **Stop it** with Control+C. You should see:
   ```text
   Keyboard interruption in main thread... closing server.
   ```

## Connect an AI tool

Your AI tool starts Dewey itself over [stdio](GLOSSARY.md#stdio) when needed, with the same `.env` and index as the demo page. No terminal window has to stay open, but the database must be running ([after a restart](#start-dewey-again-later)). It gets two tools ([MCP.md](MCP.md#the-two-tools)):

- [search_archive](GLOSSARY.md#search_archive), the one to use, returns article excerpts with links and calls no chat model: your tool's model writes the answer. With Voyage AI search models, each search still makes two small Voyage calls.
- [ask_archive](GLOSSARY.md#ask_archive), optional, has Dewey's chat model write a cited answer, costing as much as a demo-page question on paid setups.

> [!WARNING]
> **Article text the tools return is untrusted input to your AI tool.** An article, or a syndicated story, can contain text written to steer an AI ([prompt injection](GLOSSARY.md#prompt-injection)). Be careful about giving the same AI session tools that send email, edit files or run commands ([MCP.md](MCP.md#security)).

### Claude Code

On Windows, these steps are *not tested by us*.

1. **Install Claude Code, if you don't have it.** It needs a Claude subscription (Pro, Max, Ultra, Team or Enterprise) or a Claude Console account, separate from any key Dewey uses ([setup](https://code.claude.com/docs/en/setup), checked November 2026).

   On macOS and Linux (on Linux, never put `sudo` in front):
   ```bash
   curl -fsSL https://claude.ai/install.sh | bash
   ```
   On Windows (PowerShell):
   ```powershell
   irm https://claude.ai/install.ps1 | iex
   ```
   Then, in a new terminal window, check that it prints a version (on Debian and Ubuntu, `claude` may work only after you log out and back in):
   ```bash
   claude --version
   ```
   Homebrew's `brew install --cask claude-code` also works, but that copy doesn't update itself.

2. **Register Dewey,** from the Dewey folder.

   On macOS and Linux:
   ```bash
   cd ~/dewey-ai
   ```
   ```bash
   claude mcp add --scope user dewey -- "$PWD/.venv/bin/python" "$PWD/app/mcp_server.py"
   ```
   On Windows (PowerShell):
   ```powershell
   cd $HOME\dewey-ai
   ```
   ```powershell
   claude mcp add --scope user dewey -- "$PWD\.venv\Scripts\python.exe" "$PWD\app\mcp_server.py"
   ```
   `$PWD` fills in this folder's path; `--scope user` makes Dewey available to you in every folder (saved in `.claude.json` in your home folder). You should see something like this (inferred from Claude Code's documentation, not captured):
   ```text
   Added stdio MCP server dewey with command: /Users/you/dewey-ai/.venv/bin/python /Users/you/dewey-ai/app/mcp_server.py to user config
   File modified: /Users/you/.claude.json
   ```

3. **Check that it connects.** The exact layout isn't documented, but look for `✔ Connected` (`√` in Windows 10's older console):
   ```bash
   claude mcp list
   ```
   You should see something like:
   ```text
   dewey: /Users/you/dewey-ai/.venv/bin/python /Users/you/dewey-ai/app/mcp_server.py - ✔ Connected
   ```
   This starts Dewey for a moment and makes no paid calls. On `✘ Failed to connect`, run Dewey yourself to read its message.

   On macOS and Linux:
   ```bash
   "$PWD/.venv/bin/python" "$PWD/app/mcp_server.py"
   ```
   On Windows (PowerShell):
   ```powershell
   & "$PWD\.venv\Scripts\python.exe" "$PWD\app\mcp_server.py"
   ```
   If it waits silently, Dewey is fine: press Control+D once to stop it (Control+C doesn't); on Windows, close the window instead. `dewey-mcp: configuration error:` says which setting to fix.

4. **Ask it something.** Start `claude` in any folder and name the server, for example:

   > Use the dewey server's search_archive tool to find our articles about the city budget, and list each headline with its date and link.

   The first time, Claude Code may ask permission to use the tool, labelled `dewey`: choose Yes. The exact options aren't documented (*unverified*).

To stop Claude Code from calling `ask_archive`, which costs money on paid setups, add a deny rule ([Claude Code permissions](https://code.claude.com/docs/en/permissions), checked November 2026). [MCP.md](MCP.md#claude-code) shows how, and how to connect over HTTP.

### Claude Desktop

1. **Install Claude Desktop, if you don't have it,** from [claude.com/download](https://claude.com/download) (macOS 13 or later, Windows 10 or later), then open it and sign in. On Linux it's a beta for Ubuntu 22.04 or later and Debian 12 or later ([Anthropic's Linux page](https://code.claude.com/docs/en/desktop-linux)); there's none for Fedora.

2. **Print the two paths Dewey needs,** from the Dewey folder. On macOS and Linux:
   ```bash
   cd ~/dewey-ai
   ```
   ```bash
   echo "$PWD/.venv/bin/python"
   ```
   ```bash
   echo "$PWD/app/mcp_server.py"
   ```
   You should see something like:
   ```text
   /Users/you/dewey-ai/.venv/bin/python
   /Users/you/dewey-ai/app/mcp_server.py
   ```
   On Windows (PowerShell):
   ```powershell
   cd $HOME\dewey-ai
   ```
   Then these print the paths quoted with every `\` doubled, as the settings file needs (*unverified on Windows*):
   ```powershell
   "$PWD\.venv\Scripts\python.exe" | ConvertTo-Json
   ```
   ```powershell
   "$PWD\app\mcp_server.py" | ConvertTo-Json
   ```

3. **Add Dewey to Claude Desktop's settings file** with the rest of [MCP.md's recipe](MCP.md#claude-desktop), from its step 2: where the file is, what to paste, the restart, and how to check that `dewey` is listed. Keep this window open for the paths.

<details><summary>Sources</summary>

All checked November 2026:

- Anthropic's Help Center: [Install Claude Desktop](https://support.claude.com/en/articles/10065433-install-claude-desktop) and [local MCP servers on Claude Desktop](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop).
- The MCP project's guide [Connect to local MCP servers](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/docs/2026-07-28/develop/connect-local-servers.mdx).
- Claude Code's [setup](https://code.claude.com/docs/en/setup), [MCP](https://code.claude.com/docs/en/mcp) and [permissions](https://code.claude.com/docs/en/permissions) pages, and `claude mcp --help` in Claude Code 2.1.329.

</details>

### Other AI tools

- **[Agent harnesses](GLOSSARY.md#agent-harness)** such as Hermes Agent and DeepSeek Harness also use stdio: see [MCP.md](MCP.md#client-recipes).
- **Chat apps** such as Open WebUI and LibreChat connect over HTTP to one shared server (`python app/mcp_server.py --http`, at http://127.0.0.1:8765/mcp on this computer), with the token from `.env` ([MCP.md](MCP.md#before-you-start-http)).
- **For a team**, run Dewey on a server ([DEPLOYMENT.md](DEPLOYMENT.md)). To test any client, see [MCP.md](MCP.md#test-that-it-works).

## Start Dewey again later

After a restart, start the parts that don't come back by themselves:

1. **Docker:** open Docker Desktop and wait until it's running (to have it open at login, turn on Settings > General > "Start Docker Desktop when you sign in to your computer"), or run `colima start`. Linux's Docker Engine starts by itself.

2. **The database,** which stays stopped after a restart (this works in PowerShell too):
   ```bash
   docker start dewey-db
   ```
   It prints `dewey-db`, and `docker ps` should show it `Up` (your archive is kept). To start it with Docker from now on, run `docker update --restart unless-stopped dewey-db` once.

3. **Ollama** (fully open setup): open the app on a Mac; Windows and Linux start it by themselves. `ollama -v` prints `Warning: could not connect to a running Ollama instance` when it isn't running.

4. **The demo page,** if you want it: [turn on the virtual environment](#turn-it-on-in-every-new-terminal-window) and run `python app/main.py`. Your AI tool needs nothing.

**Keep your archive safe.** To bring the database back, use `docker start dewey-db`, never the wizard's `docker run` command again: a new container starts empty. These delete your indexed archive, or leave it where Dewey can't find it:

- `docker rm -v dewey-db`
- `docker system prune` while `dewey-db` is stopped
- `docker volume prune` after the container is gone
- Docker Desktop's Clean up data, and Reset to factory defaults
- uninstalling Docker Desktop
- `colima delete` (its help warns "This deletes everything"), above all with `--data`

If that happens, start a new database and index again.

<details><summary>Sources</summary>

Checked November 2026: Docker's pages on [Docker Desktop's settings](https://github.com/docker/docs/blob/main/content/manuals/desktop/settings-and-maintenance/settings.md) (start at sign-in, off by default), [starting containers automatically](https://github.com/docker/docs/blob/main/content/manuals/engine/containers/start-containers-automatically.md) (`docker update --restart`), [`docker rm`](https://github.com/docker/cli/blob/master/docs/reference/commandline/container_rm.md), [`docker system prune`](https://github.com/docker/cli/blob/master/docs/reference/commandline/system_prune.md), [`docker volume prune`](https://github.com/docker/cli/blob/master/docs/reference/commandline/volume_prune.md), [troubleshooting](https://github.com/docker/docs/blob/main/content/manuals/desktop/troubleshoot-and-support/troubleshoot/_index.md) (Clean up data), the [Mac FAQ](https://github.com/docker/docs/blob/main/content/manuals/desktop/troubleshoot-and-support/faqs/macfaqs.md) and [uninstalling](https://github.com/docker/docs/blob/main/content/manuals/desktop/uninstall.md); and `colima delete --help` (Colima 0.10.3).

</details>

## Change your settings

1. **Run the wizard again** (`python install.py`, with the environment on) and press Enter to keep each saved answer; `-` clears a hidden one. The old `.env` is kept as `.env.bak-` plus the date and time.

2. **Restart what uses the settings:** the demo page, Claude Desktop (quit and reopen) and Claude Code (a new session).

Changing the embeddings needs a new, empty database and a fresh index ([STACKS.md](STACKS.md#indexes-are-tied-to-one-embedding-model)). Switching stacks removes the old stack's keys from `.env`; the backup keeps them.

<details><summary>Editing .env by hand</summary>

The wizard is the safer way. `.env` is a hidden file in the Dewey folder. `.env.template` explains the usual settings, [STACKS.md](STACKS.md#configuring-by-hand) has each setup's lines, and server settings are in [DEPLOYMENT.md](DEPLOYMENT.md#settings-reference). On macOS and Linux, you can edit it in Terminal:

```bash
nano .env
```

Control+O then Enter saves, and Control+X quits. Keep the single quotes around each value, as the wizard writes them. Don't use TextEdit: it can turn straight quotes into curly ones and save the file under another name. In the Finder, Command+Shift+. (period) shows hidden files, a shortcut that isn't documented by Apple.

On Windows (PowerShell), open it in Notepad (*not tested by us*):

```powershell
notepad .env
```

</details>

## Update Dewey

1. **Go to the Dewey folder and turn on the virtual environment** ([how](#turn-it-on-in-every-new-terminal-window)).

2. **Download the new version.** This works in PowerShell too, and leaves `.env` and `data/` alone (git ignores both):
   ```bash
   git pull
   ```

3. **Install any new packages.**
   ```bash
   python -m pip install -r requirements.txt
   ```
   With local search models, also:
   ```bash
   python -m pip install -r requirements-local.txt
   ```
   On Linux, if you ever reinstall PyTorch, keep the `--index-url https://download.pytorch.org/whl/cpu` part, or pip fetches the 3 GB NVIDIA build.

4. **Index again** (`python app/setup_postgres.py`) when Dewey says the database `was set up by an earlier version of Dewey`, or the update's notes say a new field (such as `publisher`) needs it. It never deletes articles but embeds every one again: time with local search models, money with Voyage AI.

5. **Restart what runs Dewey** ([Change your settings](#change-your-settings), step 2).

On Linux, `sudo apt update` then `sudo apt upgrade` (or `sudo dnf upgrade`) updates Python, Docker Engine and Postgres; re-run Ollama's install command to update it. Claude Code updates itself, except a Homebrew copy (`brew upgrade claude-code`).

## Troubleshooting

Find the message you see. Fixes with several commands come after the table.

| You see | What to do |
|---|---|
| `zsh: command not found: python` (Linux: `bash: python: command not found`) | The virtual environment is off: [turn it on](#turn-it-on-in-every-new-terminal-window). |
| `zsh: command not found: python3.13` (Linux: `bash: python3.13: command not found`) | Open a new window; if it persists, install Python 3.13 ([Mac](#get-your-mac-ready); [Linux](#get-your-linux-computer-ready), step 3). |
| `Dewey needs Python 3.11 to 3.13; this is 3.9.6.` | The environment uses Apple's old Python: recreate it (below). |
| `No matching distribution found for voyageai==0.6.0`, or `Building wheel for pandas` then a long wait or a compiler error (Linux: `metadata-generation-failed`) | The environment uses Python 3.15 or 3.14: recreate it with 3.13 (Ubuntu 24.04: 3.12). |
| `error: externally-managed-environment` | pip ran outside the environment: turn it on. Never add `--break-system-packages`. |
| `CERTIFICATE_VERIFY_FAILED` | Mac with python.org's Python: in the Finder, open Applications, then Python 3.13, and double-click Install Certificates.command. |
| `[notice] A new release of pip is available: 26.2.2 -> 26.3` | Ignore it if the first number is 26.2.2 or higher; if lower, run step 4 of [Create a virtual environment](#create-a-virtual-environment). |
| `ModuleNotFoundError` | Turn the environment on; if it's on, run `python -m pip install -r requirements.txt`. For `torch` or `sentence_transformers`, also install the local search models ([STACKS.md](STACKS.md#local-search-models)). |
| `running scripts is disabled on this system` (Windows) | Do step 5 of [Get your Windows PC ready](#get-your-windows-pc-ready), then activate again. |
| `py`, `git` or `docker` `is not recognized` (Windows) | Open a new PowerShell window; if it persists, install it. |
| `ensurepip is not available`, or `ensurepip` ... `returned non-zero exit status 1.` (Linux) | Run `sudo apt install python3.13-venv` (Ubuntu 24.04: `python3.12-venv`), then recreate the environment (below). |
| `permission denied while trying to connect to the` Docker API or daemon socket (Linux) | Do step 6 of [Get your Linux computer ready](#get-your-linux-computer-ready) and log in again, or put `sudo` in front. |
| `Cannot connect to the Docker daemon at` ... `Is the docker daemon running?`, or (Docker 29 and later) `failed to connect to the docker API at` ... `check if the path is correct and if the daemon is running` | Start Docker (Docker Desktop, `colima start`, or on Linux `sudo systemctl enable --now docker`), wait, then try again. |
| `Conflict. The container name "/dewey-db" is already in use` | See "The database container already exists" below. |
| `password authentication failed for user "dewey"` | `.env` has a newer password than the container: see "The database password doesn't match" below. |
| `port is already allocated`, `ports are not available` or `address already in use` | Another program, often another Postgres, uses port 5432: see "Port 5432 is taken" below. |
| `Postgres: FAILED:` in the wizard's check | The database isn't running or its address is wrong: [start it](#start-dewey-again-later), then check again. |
| `This configuration (DEWEY_STACK=anthropic) needs these environment variables:`, or `DEWEY_STACK=anthropic needs a Claude credential.` | A setting or key is missing: run the wizard again and fill it in (for `ant auth login`, see [STACKS.md](STACKS.md#claude-anthropic-api)). |
| An error about Azure, or `DEWEY_STACK is 'azure' (or unset)`, when you didn't choose Azure | `.env` is missing or has no `DEWEY_STACK` (Dewey then uses `azure`), for example because the wizard stopped before `Write this to .env?`: go to the Dewey folder and run the wizard again. |
| `Chat model server: FAILED to reach` ... `Is Ollama running?` | Open the Ollama app; on Linux, run `sudo systemctl restart ollama`. |
| `reachable, but 'qwen3:8b' isn't listed` | Run `ollama pull qwen3:8b`, with your model's name, or fix `OPEN_LLM_MODEL` in `.env`: it must match a name after `found:` exactly. |
| Answers ignore your question or the sources (fully open setup) | Ollama's context is probably still 4,096 tokens: set 32k ([What the wizard asks](#what-the-wizard-asks), step 5), then check with `ollama ps`. |
| The indexer stops with a rate-limit error (Voyage AI) | Add a payment method to your Atlas account, wait a minute and index again (finished articles are kept, but every article is sent again). |
| `No Dewey index found in this database.` | Run `python app/setup_postgres.py`. |

<details><summary>More problems</summary>

| You see | What to do |
|---|---|
| `torch` errors installing local search models on an Intel Mac | PyTorch has no Intel Mac version new enough: use Voyage AI ([STACKS.md](STACKS.md#claude-with-voyage-ai)), or an Apple silicon Mac. |
| `DATABASE_URL is not a valid Postgres connection URL` | Paste the address again in the wizard. In a password, write `@ : / # ?` as codes, such as `%40` for `@`. |
| `connected, but the pgvector extension is NOT installed on this server` | Install pgvector for that Postgres ([STACKS.md](STACKS.md#postgres-with-pgvector)), or use the wizard's Docker database. |
| A box saying `The “git” command requires the command line developer tools` | Click Install, wait, then run the command again. |
| `Install into the current Python anyway?` | The wizard isn't in the virtual environment: press Enter (no), turn it on and run the wizard again. |
| `zsh: no matches found` | Brackets, `?` or `*` weren't quoted: copy the box with its copy button. |
| `zsh: command not found: #` or `cd: too many arguments` | A note was pasted with the command: paste only the command. |
| `destination path 'dewey-ai' already exists` | You downloaded Dewey before: run `cd dewey-ai`, then [update it](#update-dewey). |
| `local changes to the following files would be overwritten` (from `git pull`) | Run `git stash` to set the change aside, then `git pull` again. |
| `zsh: command not found: docker` | Open Docker Desktop once, then a new Terminal window. |
| `Refusing to start: GRADIO_SERVER_NAME=` | That setting would let other computers reach the demo page, which has no login: remove it from `.env` ([DEPLOYMENT.md](DEPLOYMENT.md#troubleshooting)). |
| The demo page address ends in 7861 or another number | Port 7860 was busy: open the address on the `Running on local URL` line. |
| The first question is slow | Local search models load on the first question, and the local reranker downloads first. Later questions are faster. |
| `Embedding settings don't match this database` | Change them back, or index into a new, empty database ([STACKS.md](STACKS.md#indexes-are-tied-to-one-embedding-model)). |
| `set up by an earlier version of Dewey` | Run `python app/setup_postgres.py` again. It embeds every article again. |
| `claude mcp list` shows `✘ Failed to connect` | Run Dewey's command yourself ([Claude Code](#claude-code), step 3), and see [MCP.md](MCP.md#troubleshooting). |
| `Server already exists` (from `claude mcp add`) | Run `claude mcp remove dewey --scope user`, then add it again. |
| Dewey works in Claude Code only in the Dewey folder | It was added without `--scope`: run `claude mcp remove dewey --scope local`, then add it again with `--scope user`. |
| `overridden by a policy defined at a more specific scope` (Windows) | Your organization blocks scripts: skip activating, and type `.venv\Scripts\python.exe` wherever this guide says `python`. |
| `No suitable Python runtime found` (Windows) | Another Python is installed, but not 3.13: do step 3 of [Get your Windows PC ready](#get-your-windows-pc-ready). |
| `Python was not found; run without arguments to install from the Microsoft Store` (Windows) | `python` is Windows' shortcut to the Store: turn the virtual environment on, or use `py -3.13`. |
| `Microsoft Visual C++ Redistributable is not installed` (Windows, from PyTorch) | Install Microsoft's runtime (below), then try again. |
| A warning about symlinks and `Developer Mode` from `huggingface_hub` (Windows) | Nothing: it's harmless. |
| `failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine` (Windows) | Docker Desktop isn't running: open it from Start, wait, then try again. |
| `Unexpected WSL error` with `HCS_E_HYPERV_NOT_INSTALLED` (Windows) | Turn on hardware virtualization in your BIOS or UEFI settings. Task Manager's Performance tab shows whether it's on. |
| The wizard doesn't offer `docker` | Docker was installed after this window opened: open a new window and run the wizard again. |
| `is not in the sudoers file.` (Debian) | See "Debian says is not in the sudoers file" in [Get your Linux computer ready](#get-your-linux-computer-ready). |
| `Ident authentication failed for user "dewey"` (Fedora) | On a database with nothing in it, set Postgres up again with the `PGSETUP_INITDB_OPTIONS` line. Or change `ident` to `scram-sha-256` in `/var/lib/pgsql/data/pg_hba.conf` and restart Postgres (not tested by us). |
| `permission denied to create extension "vector"` (Linux) | Run `sudo -u postgres psql -d dewey -c 'CREATE EXTENSION IF NOT EXISTS vector'`. |
| `Error: do not run this installer with sudo.` | Run Claude Code's installer without `sudo`. |
| `bash: claude: command not found` right after installing (Linux) | Log out and back in, or see "Claude Code on Linux" below. |
| `Hint: You are currently not seeing messages from other users and the system.` | Put `sudo` in front of `journalctl`. |

</details>

**Recreate the virtual environment.** If the prompt starts with `(.venv)`, type `deactivate` first. Then make it again with the right Python (Ubuntu 24.04: `python3.12`), turn it on, update pip again on Linux (step 4 of [Create a virtual environment](#create-a-virtual-environment)), and run the wizard again.

On macOS and Linux:

```bash
python3.13 -m venv --clear .venv
```

```bash
source .venv/bin/activate
```

On Windows (PowerShell):

```powershell
py -3.13 -m venv --clear .venv
```

```powershell
.venv\Scripts\Activate.ps1
```

<details><summary>The database container already exists</summary>

Not tested by us: these steps come from Docker's documentation and source code. List your containers:

```bash
docker ps -a
```

If the `dewey-db` STATUS says `Up`, it's already running and you don't need the wizard's command. If it says `Exited` or `Created`, start it:

```bash
docker start dewey-db
```

That container keeps the password from the run that created it, not the one the wizard printed this time. If the wizard saved a database address that time, run the wizard again, answer `yes` at the Postgres question and press Enter to keep the saved address. If it didn't, use "The database password doesn't match". Or, only if nothing has been indexed yet, delete that database with `docker rm -v dewey-db` and paste the wizard's new command.

</details>

<details><summary>The database password doesn't match</summary>

This sets the container's password to the one in `.env` and keeps your data. It hasn't been tested by us. First, in the Dewey folder, show the password in `.env` (it appears on screen). On macOS and Linux:

```bash
grep POSTGRES_PASSWORD .env
```

On Windows (PowerShell):

```powershell
Select-String -Path .env -Pattern POSTGRES_PASSWORD
```

Then run this, with the value between the quotes in place of PASTE_PASSWORD_FROM_ENV. It works in PowerShell too.

```bash
docker exec dewey-db psql -U dewey -d dewey -c "ALTER USER dewey PASSWORD 'PASTE_PASSWORD_FROM_ENV'"
```

Postgres replies `ALTER ROLE`.

</details>

<details><summary>Port 5432 is taken</summary>

Not tested by us. First see whether another container uses it: in `docker ps`, look for `5432->5432` in the PORTS column. Then find the program holding the port.

On a Mac:

```bash
lsof -nP -iTCP:5432 -sTCP:LISTEN
```

On Linux:

```bash
sudo ss -ltnp | grep 5432
```

On Windows (PowerShell):

```powershell
Get-NetTCPConnection -LocalPort 5432 -State Listen
```

On Windows, `Get-Process -Id NUMBER`, with the OwningProcess number, shows the program's name. A program called `postgres` is another Postgres (Homebrew's, Postgres.app or Linux's own): stop it, or use it instead of Docker. Then run `docker start dewey-db`: the failed command already created the container.

</details>

<details><summary>Microsoft's runtime (Windows, for PyTorch)</summary>

```powershell
winget install --id 'Microsoft.VCRedist.2015+.x64' -e --source winget
```

Or download it from https://aka.ms/vc14/vc_redist.x64.exe (26 MB).

</details>

<details><summary>Claude Code on Linux</summary>

If you'd rather not log out, run these two, one at a time:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

```bash
source ~/.bashrc
```

</details>

## Where to go next

| I want to... | Read |
|---|---|
| Import my archive from my [CMS](GLOSSARY.md#cms), a feed or a spreadsheet | [IMPORTING.md](IMPORTING.md) |
| Compare the setups, their costs and who receives my data | [STACKS.md](STACKS.md) |
| Connect Hermes Agent, DeepSeek Harness, Open WebUI or LibreChat | [MCP.md](MCP.md) |
| Choose or change the chat model on the `open` stack | [MODELS.md](MODELS.md) |
| Set up the Azure stack | [AZURE.md](AZURE.md) |
| Run Dewey for a team, on a server | [DEPLOYMENT.md](DEPLOYMENT.md) |
| Look up a word | [GLOSSARY.md](GLOSSARY.md) |
| Go back to the overview | [README](../README.md) |

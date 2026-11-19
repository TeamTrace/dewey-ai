#!/usr/bin/env python3
"""
Interactive installer for Dewey.

    python install.py            # walk through setup and write .env
    python install.py --dry-run  # same questions; print the .env instead of writing it

Uses only the Python standard library, so it runs before any dependencies are
installed. It:

  1. asks which stack to run (azure / anthropic / open) and, for the Postgres
     stacks, where embeddings and reranking should run;
  2. for every account or tool that choice needs, says where to get it and
     asks for the credential (secrets are typed hidden and never printed);
  3. writes .env (backing up any existing one), readable only by you;
  4. optionally installs Python dependencies and checks the credentials and
     services it can reach;
  5. for the Postgres stacks, helps get the archive into data/ with one of
     the importers in app/importers/ (a small trial run first), then
     optionally indexes the articles in data/.

Re-running it is safe: values already in .env are offered as defaults, so you
can change one option without re-entering everything.
"""
import argparse
import getpass
import ipaddress
import json
import os
import re
import shlex
import secrets
import shutil
import subprocess
import textwrap
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
MIN_PYTHON = (3, 11)  # README badge: Python 3.11+

# Where users get each account or tool. Sources: Anthropic's embeddings guide
# (Voyage keys come from MongoDB Atlas), Ollama's README, pgvector's README.
LINKS = {
    "anthropic": "https://platform.claude.com",
    "voyage": "https://www.mongodb.com/docs/voyageai/quickstart/",
    "azure": "https://portal.azure.com",
    "ollama": "https://ollama.com/download",
    "pgvector": "https://github.com/pgvector/pgvector#installation",
    "docker": "https://docs.docker.com/get-started/get-docker/",
    "pytorch_cpu": "https://download.pytorch.org/whl/cpu",
}
PGVECTOR_DOCKER_IMAGE = "pgvector/pgvector:pg18"  # tag list: pgvector README, Docker section
PGVECTOR_CONTAINER = "dewey-db"  # the container name the Postgres step suggests
OLLAMA_DEFAULT_URL = "http://localhost:11434/v1"
OLLAMA_MODEL_SUGGESTIONS = [
    ("qwen3:8b", "Qwen3-8B, 8B parameters, Apache 2.0. Smaller; a reasonable start on a laptop."),
    ("gpt-oss:20b", "gpt-oss-20b, 21B parameters, Apache 2.0. Stronger, needs much more memory."),
]

# Keys written to .env, in this order, grouped under comments.
SECTIONS = [
    ("Stack", ["DEWEY_STACK"]),
    ("Newsroom", ["NEWSROOM_NAME", "ARCHIVE_START_DATE", "DEFAULT_REGION"]),
    ("Claude (DEWEY_STACK=anthropic)", ["ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"]),
    ("Open-weight chat model (DEWEY_STACK=open)", ["OPEN_LLM_BASE_URL", "OPEN_LLM_MODEL", "OPEN_LLM_API_KEY"]),
    ("Postgres + embeddings (anthropic and open stacks)",
     ["DATABASE_URL", "EMBEDDING_PROVIDER", "RERANK_PROVIDER", "VOYAGE_API_KEY"]),
    ("Azure (DEWEY_STACK=azure)", [
        "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "EMBEDDING_DEPLOYMENT_NAME", "EMBEDDING_MODEL_NAME",
        "CHATGPT_DEPLOYMENT_NAME", "CHATGPT_MODEL_NAME", "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_API_KEY",
        "AZURE_SEARCH_INDEX_NAME", "AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_CONTAINER_NAME",
    ]),
]
SECRET_KEYS = {"ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "OPEN_LLM_API_KEY", "AZURE_OPENAI_API_KEY",
               "AZURE_SEARCH_API_KEY", "AZURE_STORAGE_CONNECTION_STRING", "DATABASE_URL", "POSTGRES_PASSWORD"}
# Other names that hold a secret (POSTGRES_PASSWORD, WORDPRESS_APP_PASSWORD, GHOST_CONTENT_API_KEY, HF_TOKEN...).
SECRET_NAME = re.compile(r"PASS|SECRET|TOKEN|KEY|CREDENTIAL|AUTH|CONNECTION_STRING|DATABASE_URL|DSN",
                         re.IGNORECASE)
KEPT_HEADER = "# Kept from your previous .env"
EXTRA_HEADER = "# Other settings"  # values this run produced that no section claims


# ---------------------------------------------------------------- terminal UI

# ------------------------------------------------------------------- styling

# The Gun Violence Data Hub palette, from tokens/colors.css in the Data Team's
# design system, with the nearest xterm-256 index as a fallback. Only the
# mid-tones are used: a terminal brings its own background, which may be light or
# dark, so --white (warm paper) and --black (blue-black ink) are left out and body
# text keeps the reader's own foreground colour.
_PALETTE = {
    "brand": ((0x67, 0x8D, 0xEF), 69),   # --blue, the signature periwinkle: links, accents
    "deep": ((0x49, 0x64, 0xAA), 61),    # --blue-700, for structure
    "gold": ((0xF9, 0xD1, 0x33), 220),   # --gold, whose role is selection highlight
}


def _colour_depth() -> int:
    """24 for truecolour, 8 for the 256-colour palette, 0 for no colour."""
    if os.getenv("NO_COLOR") is not None:  # no-color.org: any value, including empty
        return 0
    truecolour = os.getenv("COLORTERM", "").lower() in ("truecolor", "24bit")
    if os.getenv("FORCE_COLOR"):
        return 24 if truecolour else 8
    if not sys.stdout.isatty() or os.getenv("TERM", "") in ("", "dumb"):
        return 0
    if os.name == "nt" and not (os.getenv("WT_SESSION") or os.getenv("ANSICON")):
        return 0
    return 24 if truecolour else 8


_DEPTH = _colour_depth()
_STYLED = _DEPTH > 0


def _fg(name: str) -> str:
    rgb, index = _PALETTE[name]
    return "\033[38;2;%d;%d;%dm" % rgb if _DEPTH == 24 else f"\033[38;5;{index}m"


RESET, BOLD, DIM, UNDERLINE = "\033[0m", "\033[1m", "\033[2m", "\033[4m"
BRAND, DEEP, GOLD = (_fg("brand"), _fg("deep"), _fg("gold")) if _STYLED else ("", "", "")


def paint(text: str, *codes: str) -> str:
    """Wrap text in SGR codes as a single unit.

    The text itself stays contiguous, which the wizard's own tests rely on: they
    drive install.py through a pty and match on prompt substrings, so a code
    inserted mid-phrase would break them.
    """
    if not _STYLED or not codes:
        return text
    return "".join(codes) + text + RESET


def say(text: str = "") -> None:
    print(text)


def heading(text: str) -> None:
    say()
    say(paint("──", DEEP) + " " + paint(text, BRAND, BOLD) + " "
        + paint("─" * max(3, 60 - len(text)), DEEP, DIM))


def ask(prompt: str, default: str = "", required: bool = False) -> str:
    suffix = paint(f" [{default}]", DIM) if default else ""
    while True:
        try:
            value = input(f"{prompt}{suffix}" + paint(":", BRAND) + " ").strip()
        except EOFError:
            value = ""
        value = value or default
        if value or not required:
            return value
        say("  This one is required.")


def ask_secret(prompt: str, existing: str = "", kept: str = "the saved value") -> str:
    """Hidden input. Enter keeps an existing value; '-' clears it."""
    hint = f" [Enter keeps {kept}, '-' clears it]" if existing else " [Enter to skip for now]"
    try:
        value = getpass.getpass(f"{prompt}" + paint(hint, DIM) + paint(":", BRAND) + " ").strip()
    except EOFError:
        value = ""
    if value == "-":
        return ""
    return value or existing


def ask_yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        value = ask(f"{prompt} " + paint(f"({d})", DIM)).lower()
        if not value:
            return default
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False


def choose(prompt: str, options, default: str, mark: str = "current") -> str:
    """options: list of (value, description)."""
    say(prompt)
    for i, (value, desc) in enumerate(options, 1):
        marker = paint(f" ({mark})", GOLD) if value == default else ""
        say("  " + paint(f"{i}.", BRAND, BOLD) + f" {value}{marker}: {desc}")
    values = [v for v, _ in options]
    while True:
        raw = ask("Choose a number or name", default)
        if raw in values:
            return raw
        if raw.isdigit() and 1 <= int(raw) <= len(values):
            return values[int(raw) - 1]
        say(f"  Please enter 1-{len(values)} or one of: {', '.join(values)}")


def offer_link(label: str, url: str, interactive: bool = True) -> None:
    say(f"  {label}: " + paint(url, BRAND, UNDERLINE))
    if interactive and ask_yes("  Open it in your browser now?", default=False):
        try:
            webbrowser.open(url)
        except Exception:
            say("  Couldn't open a browser; copy the link above instead.")


# ---------------------------------------------------------------- .env I/O

def read_env(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            q = value[0]
            value = re.sub(r"\\([\\" + q + "])", r"\1", value[1:-1])
        values[env_key(key)] = value
    return values


def env_key(text: str) -> str:
    """The variable name in a KEY=value line, without the `export ` some .env files use (python-dotenv does too)."""
    key = text.strip()
    return key[len("export"):].strip() if re.match(r"export\s", key) else key


def quote(value: str) -> str:
    # Single quotes: python-dotenv expands ${VAR} inside double-quoted values,
    # which would silently corrupt a password containing "${". Inside single
    # quotes it only unescapes \\ and \'.
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def render_env(values: dict, extra_existing: dict) -> str:
    """values: keys to write. An empty string is written as KEY="" (meaningful for DEFAULT_REGION)."""
    lines = [f"# Written by install.py on {datetime.now():%Y-%m-%d %H:%M}. Re-run it to change settings;",
             "# see .env.template for every option."]
    written = set()
    for title, keys in SECTIONS:
        present = [k for k in keys if k in values]
        if not present:
            continue
        lines += ["", f"# {title}"]
        for k in present:
            lines.append(f"{k}={quote(values[k])}")
            written.add(k)
    # Values this run produced that no section claims. POSTGRES_PASSWORD is the one
    # that matters: the docs leave it to Compose users, so it is deliberately not in
    # SECTIONS -- putting it there would drop an existing one on a run that doesn't
    # set it -- but the Docker step can generate it, and then it has to reach .env.
    unclaimed = {k: v for k, v in values.items() if k not in written}
    if unclaimed:
        lines += ["", EXTRA_HEADER]
        for k, v in unclaimed.items():
            lines.append(f"{k}={quote(v)}")
            written.add(k)
    # Keep anything already in .env that this installer doesn't manage.
    others = {k: v for k, v in extra_existing.items() if k not in written and k not in values}
    if others:
        lines += ["", KEPT_HEADER]
        lines += [f"{k}={quote(v)}" for k, v in others.items()]
    return "\n".join(lines) + "\n"


def is_secret(key: str) -> bool:
    key = env_key(key)
    return key in SECRET_KEYS or bool(SECRET_NAME.search(key))


def redact(text: str) -> str:
    """
    The .env text for printing. Masks the secrets this installer manages, any
    other key whose name looks like a secret, and every value kept from the
    previous .env: this installer doesn't know what those hold.
    """
    out, kept = [], False
    for line in text.splitlines():
        if line == KEPT_HEADER:
            kept, line = True, f"{KEPT_HEADER} (values not shown here)"
        key, sep, _ = line.partition("=")
        if sep and not line.lstrip().startswith("#") and (kept or is_secret(key)):
            line = f"{key}='********'"
        out.append(line)
    return "\n".join(out)


def write_env(content: str) -> None:
    if ENV_PATH.exists():
        backup = ENV_PATH.with_name(f".env.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(ENV_PATH, backup)
        say(f"  Backed up your previous .env to {backup.name} (gitignored).")
    ENV_PATH.write_text(content, encoding="utf-8")
    try:
        os.chmod(ENV_PATH, 0o600)  # secrets: owner read/write only
    except OSError:
        pass
    say(f"  Wrote {ENV_PATH}")


# ---------------------------------------------------------------- questions

def ask_stack(cur: dict) -> str:
    heading("1. Which stack?")
    return choose(
        "Dewey can run on three stacks:",
        [
            ("anthropic", "Claude writes the answers; your archive lives in Postgres. Needs an "
                          "Anthropic account (paid API). Search models can be hosted (Voyage AI) "
                          "or open source on this machine - you choose next."),
            ("open", "Everything open source on hardware you control: a local chat model "
                     "(via Ollama), local search models, Postgres. No accounts or API keys."),
            ("azure", "The original setup: Azure OpenAI + Azure AI Search + Blob Storage. "
                      "Needs an Azure subscription with those services."),
        ],
        default=cur.get("DEWEY_STACK") or "anthropic",
    )


def ask_newsroom(cur: dict) -> dict:
    heading("Newsroom details (used in Dewey's prompts)")
    say("Leave blank to keep Dewey's original defaults (The Philadelphia Inquirer).")
    out = {
        "NEWSROOM_NAME": ask("Newsroom name, as it should appear in prompts", cur.get("NEWSROOM_NAME", "")),
        "ARCHIVE_START_DATE": ask("Earliest article date in your archive (e.g. January 1, 2015)",
                                  cur.get("ARCHIVE_START_DATE", "")),
    }
    say("Should Dewey assume questions are about a region when none is named?")
    saved = cur.get("DEFAULT_REGION")
    region = ask("Region (e.g. 'the Greater Philadelphia Region'), or 'none' for a national outlet",
                 "none" if saved == "" else (saved or ""))
    if region.lower() == "none":
        out["DEFAULT_REGION"] = ""  # explicit empty value disables the regional assumption
    elif region:
        out["DEFAULT_REGION"] = region
    return out


def ask_anthropic(cur: dict, interactive: bool) -> dict:
    heading("Claude (Anthropic API)")
    say("You need an Anthropic API key. Create an account and a key in the Claude Console")
    say("(API Keys section). API use is billed per token.")
    offer_link("Claude Console", LINKS["anthropic"], interactive)
    key = ask_secret("Paste your Anthropic API key", cur.get("ANTHROPIC_API_KEY", ""))
    if key and not key.startswith("sk-ant-"):
        say("  Note: Anthropic API keys normally start with 'sk-ant-'. Double-check you copied the whole key.")
    out = {"ANTHROPIC_API_KEY": key}
    model = cur.get("ANTHROPIC_MODEL", "")
    if model:
        out["ANTHROPIC_MODEL"] = ask("Claude model", model)
    return out


def ask_retrieval(stack: str, cur: dict) -> dict:
    heading("Search models: embeddings and reranking")
    say("These turn your articles into searchable vectors and re-order the results. They're")
    say("separate from the chat model: you can mix them, e.g. Claude for answers with")
    say("open-source search models, which needs no Voyage account.")
    default_embed = "voyage" if stack == "anthropic" else "local"
    embed = choose(
        "Where should embeddings (the vectors that power semantic search) be computed?",
        [
            ("voyage", "Voyage AI's hosted API (voyage-4). Needs a Voyage key; billed per token."),
            ("local", "voyage-4-nano on this machine. Open-weight (Apache 2.0), no key; "
                      "needs PyTorch and is slower on CPU."),
        ],
        default=cur.get("EMBEDDING_PROVIDER") or default_embed,
    )
    rerank = choose(
        "Where should results be reranked?",
        [
            ("voyage", "Voyage AI's hosted rerank-3. Needs a Voyage key."),
            ("local", "bge-reranker-v2-m3 on this machine. Open-weight (Apache 2.0), no key."),
            ("none", "Skip reranking; use the hybrid search order. Fastest, likely less precise."),
        ],
        default=cur.get("RERANK_PROVIDER") or default_embed,
    )
    say("Note: once you index your archive, the embedding choice is fixed for that database.")
    return {"EMBEDDING_PROVIDER": embed, "RERANK_PROVIDER": rerank}


def ask_voyage(cur: dict, interactive: bool) -> dict:
    heading("Voyage AI (MongoDB)")
    say("Voyage AI keys are created in MongoDB Atlas: sign up or log in, open your project,")
    say("choose 'AI Model APIs', then 'Create model API key'.")
    offer_link("Voyage AI quick start (MongoDB docs)", LINKS["voyage"], interactive)
    return {"VOYAGE_API_KEY": ask_secret("Paste your Voyage AI (Atlas model) API key", cur.get("VOYAGE_API_KEY", ""))}


def ask_open_llm(cur: dict, interactive: bool) -> dict:
    heading("Local chat model (Ollama or another OpenAI-compatible server)")
    base_url = cur.get("OPEN_LLM_BASE_URL") or OLLAMA_DEFAULT_URL
    if shutil.which("ollama"):
        say("Found Ollama on this machine.")
    else:
        say("Ollama isn't installed (or isn't on your PATH). It's a free, MIT-licensed app that")
        say("downloads and serves open-weight chat models. Skip this if you use another server")
        say("(vLLM, llama.cpp, LM Studio) - you'll be asked for its URL.")
        offer_link("Download Ollama", LINKS["ollama"], interactive)
    base_url = ask("Server URL (OpenAI-compatible, ending in /v1)", base_url)

    say("Which model? It must already be downloaded on that server. Suggestions for Ollama:")
    for name, desc in OLLAMA_MODEL_SUGGESTIONS:
        say(f"  - {name}: {desc}")
    model = ask("Model name", cur.get("OPEN_LLM_MODEL") or OLLAMA_MODEL_SUGGESTIONS[0][0], required=True)

    out = {"OPEN_LLM_BASE_URL": base_url if base_url != OLLAMA_DEFAULT_URL else "", "OPEN_LLM_MODEL": model}
    if base_url == OLLAMA_DEFAULT_URL and shutil.which("ollama") and interactive:
        if ask_yes(f"Download '{model}' with Ollama now? (can be several GB)", default=False):
            run(["ollama", "pull", model])
    if base_url != OLLAMA_DEFAULT_URL:
        out["OPEN_LLM_API_KEY"] = ask_secret("API key for that server, if it needs one",
                                             cur.get("OPEN_LLM_API_KEY", ""))
        if out["OPEN_LLM_API_KEY"] and sends_key_in_clear(base_url):
            say("  Warning: that address uses http://, so the API key and every question would be sent")
            say("  unencrypted. Use the server's https:// address unless it runs on this machine.")
    return out


def offer_pgvector_container() -> dict:
    """Print a ready-to-run pgvector container command; return the .env values for it.

    Returns {} if the user hasn't started it, so the caller can fall back to the
    list of options. The password is generated rather than hard-coded and the port
    is published on loopback only, because docker-compose.yml takes the same care
    and a suggestion that contradicts it teaches the wrong habit.
    """
    password = secrets.token_hex(12)  # 24 characters, letters and digits only
    say()
    say("  Run this in another terminal, then come back:")
    say()
    say(paint(f"    docker run -d --name {PGVECTOR_CONTAINER} \\", BRAND))
    say(paint("      -e POSTGRES_USER=dewey -e POSTGRES_DB=dewey \\", BRAND))
    say(paint(f"      -e POSTGRES_PASSWORD={password} \\", BRAND))
    say(paint(f"      -p 127.0.0.1:5432:5432 {PGVECTOR_DOCKER_IMAGE}", BRAND))
    say()
    say("  That publishes the port on 127.0.0.1 only, so nothing else on your network")
    say("  can reach it. The password above is generated for you and is written to .env")
    say("  as POSTGRES_PASSWORD, which docker-compose.yml also needs.")
    if not ask_yes("  Is it running?", default=True):
        say("  No problem: start it whenever you like, then re-run this wizard.")
        return {}
    return {"DATABASE_URL": f"postgresql://dewey:{password}@127.0.0.1:5432/dewey",
            "POSTGRES_PASSWORD": password}


def ask_postgres(cur: dict, interactive: bool) -> dict:
    heading("Postgres with pgvector (where your archive is stored and searched)")
    say("Dewey keeps your archive in a Postgres database with the pgvector extension")
    say("installed. It can run on this machine, or anywhere you can reach from it.")
    say()
    have_docker = bool(shutil.which("docker"))
    options = [("yes", "I have one already, and its connection URL")]
    if have_docker:
        options.append(("docker", f"help me start one here with Docker ({PGVECTOR_DOCKER_IMAGE})"))
    options.append(("later", "not yet, show me the options and carry on without it"))
    default = "yes" if cur.get("DATABASE_URL") else ("docker" if have_docker else "later")
    answer = choose("Do you have a Postgres database for Dewey?", options, default, mark="suggested")

    if answer == "docker":
        started = offer_pgvector_container()
        if started:
            return started
        answer = "later"

    if answer == "later":
        say()
        say("Any of these gives Dewey what it needs:")
        say("  - your own Postgres server, with the pgvector extension installed")
        say("  - a managed Postgres that offers pgvector (Neon, Supabase, Fly, ...)")
        say(f"  - Docker on this machine: {PGVECTOR_DOCKER_IMAGE}")
        say("    or, with Homebrew Postgres on macOS: brew install pgvector")
        if not have_docker:
            offer_link("Docker isn't installed; get Docker", LINKS["docker"], interactive)
        offer_link("pgvector installation guide", LINKS["pgvector"], interactive)
        say("Everything else you answer here still gets written. Add DATABASE_URL to .env")
        say("when the database exists, or re-run this wizard then; until it does, Dewey")
        say("can't index your archive or answer questions.")
        return {"DATABASE_URL": ""}

    url = ask_secret("Database URL (postgresql://user:password@host:port/dbname)",
                     cur.get("DATABASE_URL", ""))
    if url and not url.startswith(("postgresql://", "postgres://")):
        say("  Note: that doesn't look like a postgresql:// URL.")
    return {"DATABASE_URL": url}


def ask_azure(cur: dict, interactive: bool) -> dict:
    heading("Azure")
    say("The Azure stack needs these resources in your Azure subscription:")
    say("  - Azure OpenAI with a chat deployment and an embedding deployment")
    say("  - Azure AI Search")
    say("  - Azure Storage (Blob) for staging documents")
    say("Endpoints and keys are on each resource's 'Keys and Endpoint' / 'Access keys' page.")
    offer_link("Azure portal", LINKS["azure"], interactive)
    fields = [
        ("AZURE_OPENAI_ENDPOINT", "Azure OpenAI endpoint (https://<name>.openai.azure.com)", False),
        ("AZURE_OPENAI_API_KEY", "Azure OpenAI key", True),
        ("CHATGPT_DEPLOYMENT_NAME", "Chat deployment name", False),
        ("CHATGPT_MODEL_NAME", "Chat model name (e.g. gpt-5)", False),
        ("EMBEDDING_DEPLOYMENT_NAME", "Embedding deployment name", False),
        ("EMBEDDING_MODEL_NAME", "Embedding model name (e.g. text-embedding-3-large)", False),
        ("AZURE_SEARCH_ENDPOINT", "AI Search endpoint (https://<name>.search.windows.net)", False),
        ("AZURE_SEARCH_API_KEY", "AI Search admin key", True),
        ("AZURE_SEARCH_INDEX_NAME", "Index name to create (your choice)", False),
        ("AZURE_STORAGE_CONNECTION_STRING", "Storage connection string", True),
        ("AZURE_STORAGE_CONTAINER_NAME", "Blob container name (your choice)", False),
    ]
    out = {}
    for key, label, secret in fields:
        out[key] = ask_secret(label, cur.get(key, "")) if secret else ask(label, cur.get(key, ""))
    return out


# ---------------------------------------------------------------- actions

def run(cmd, **kw) -> bool:
    say("  $ " + " ".join(cmd))
    try:
        return subprocess.run(cmd, cwd=ROOT, **kw).returncode == 0
    except FileNotFoundError:
        say(f"  Couldn't run {cmd[0]!r}.")
        return False


def needs_local_models(values: dict) -> bool:
    return values.get("DEWEY_STACK") in ("anthropic", "open") and "local" in (
        values.get("EMBEDDING_PROVIDER"), values.get("RERANK_PROVIDER"))


def install_dependencies(values: dict) -> None:
    heading("Install Python dependencies")
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if not in_venv:
        say("You're not in a virtual environment. The README recommends one:")
        say("  python -m venv .venv && source .venv/bin/activate   (Windows: .venv\\Scripts\\activate)")
        say("then re-run: python install.py")
        if not ask_yes("Install into the current Python anyway?", default=False):
            return
    pip = [sys.executable, "-m", "pip", "install"]
    if not ask_yes("Install requirements.txt now?", default=True):
        return
    if not run(pip + ["-r", "requirements.txt"]):
        say("  pip failed; see the output above.")
        return
    if needs_local_models(values):
        say("Local models need PyTorch (a large download).")
        if ask_yes("Does this machine lack an NVIDIA GPU? (installs the smaller CPU-only PyTorch)", default=True):
            run(pip + ["torch", "--index-url", LINKS["pytorch_cpu"]])
        run(pip + ["-r", "requirements-local.txt"])


def is_loopback(host: Optional[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def sends_key_in_clear(url: str) -> bool:
    """True when a key sent to this address could be read on the network: plain http:// to another machine."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        return parts.scheme.lower() == "http" and not is_loopback(parts.hostname)
    except ValueError:
        return False


def _origin(url: str):
    parts = urllib.parse.urlsplit(url)
    default = {"http": 80, "https": 443}.get(parts.scheme.lower())
    try:
        port = parts.port or default
    except ValueError:
        port = default
    return parts.scheme.lower(), (parts.hostname or "").lower(), port


class _CredentialSafeRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry the Authorization header to another origin or down to http://
    (urllib copies every header onto the redirected request). Same rule as app/importers/common.py."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(req.full_url) != _origin(new.full_url):
            for store in (new.headers, new.unredirected_hdrs):
                for name in [n for n in store if n.lower() in ("authorization", "proxy-authorization", "cookie")]:
                    del store[name]
        return new


_CHECK_OPENER = urllib.request.build_opener(_CredentialSafeRedirects)


def http_get_json(url: str, headers=None, timeout: float = 5.0):
    req = urllib.request.Request(url, headers=headers or {})
    with _CHECK_OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_services(values: dict) -> None:
    heading("Check credentials and services")
    stack = values["DEWEY_STACK"]
    results = []

    if stack == "anthropic" and values.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            anthropic.Anthropic(api_key=values["ANTHROPIC_API_KEY"]).models.list(limit=1)
            results.append(("Anthropic API key", "ok"))
        except ImportError:
            results.append(("Anthropic API key", "skipped (install requirements first)"))
        except Exception as e:
            results.append(("Anthropic API key", f"FAILED: {type(e).__name__}: {e}"))

    if stack == "open":
        base = (values.get("OPEN_LLM_BASE_URL") or OLLAMA_DEFAULT_URL).rstrip("/")
        headers = {"Authorization": f"Bearer {values['OPEN_LLM_API_KEY']}"} if values.get("OPEN_LLM_API_KEY") else {}
        if headers and sends_key_in_clear(base):
            # Refuse rather than send: anyone on the network path could read the key.
            results.append(("Chat model server", f"NOT checked: {base} uses http://, so the API key would be sent "
                            "unencrypted. Use the server's https:// address (http:// is only safe on this machine)."))
        else:
            try:
                models = [m.get("id") for m in http_get_json(f"{base}/models", headers).get("data", [])]
                want = values.get("OPEN_LLM_MODEL", "")
                if want in models:
                    results.append(("Chat model server", f"ok, '{want}' is available"))
                else:
                    hint = f" Run: ollama pull {want}" if base == OLLAMA_DEFAULT_URL.rstrip("/") else ""
                    results.append(("Chat model server", f"reachable, but '{want}' isn't listed "
                                    f"(found: {', '.join(models) or 'none'}).{hint}"))
            except (urllib.error.URLError, OSError, ValueError) as e:
                results.append(("Chat model server", f"FAILED to reach {base}: {e}. Is Ollama running?"))

    if stack in ("anthropic", "open") and values.get("DATABASE_URL"):
        try:
            import psycopg
            with psycopg.connect(values["DATABASE_URL"], connect_timeout=5) as conn:
                row = conn.execute(
                    "SELECT default_version FROM pg_available_extensions WHERE name = 'vector'").fetchone()
            results.append(("Postgres", f"ok, pgvector {row[0]} available" if row else
                            "connected, but the pgvector extension is NOT installed on this server"))
        except ImportError:
            results.append(("Postgres", "skipped (install requirements first)"))
        except Exception as e:
            results.append(("Postgres", f"FAILED: {type(e).__name__}: {e}"))

    if "voyage" in (values.get("EMBEDDING_PROVIDER"), values.get("RERANK_PROVIDER")) and stack != "azure":
        results.append(("Voyage AI key", "not checked (checking would make a billed API call); "
                        "app/setup_postgres.py will report a bad key"))

    if stack == "azure":
        results.append(("Azure", "not checked here; app/setup.py validates the configuration"))

    for name, status in results:
        say(f"  {name}: {status}")


# ------------------------------------------------- checks on what was imported

@dataclass
class ImportFinding:
    """Something structurally wrong with imported articles. `stop` means don't
    import the rest of the archive before looking at it."""
    stop: bool
    saw: str
    fix: str


def load_imported(paths) -> List[dict]:
    """The articles in the files an importer just wrote."""
    docs = []
    for path in sorted(paths):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, list):
            docs += [d for d in data if isinstance(d, dict)]
    return docs


def review_import(docs: List[dict], plan: Optional["ImportPlan"] = None) -> List[ImportFinding]:
    """What looks wrong about freshly imported articles.

    The importer reports what it skipped. This looks at what it kept, because a
    field mapped to the wrong thing imports perfectly cleanly: a byline that is
    the same on every article, or a date that never varies, only shows up later
    as a filter that returns nothing or credits the wrong reporter.
    """
    found: List[ImportFinding] = []
    total = len(docs)
    if not total:
        return found

    bylined = [d for d in docs if d.get("authors")]
    names = {a for d in docs for a in (d.get("authors") or []) if isinstance(a, str)}

    if not bylined:
        found.append(ImportFinding(
            False,
            f"None of the {total} articles has a byline.",
            "Dewey never guesses a name, so author filters will match nothing. If the site "
            "runs Co-Authors Plus, check that it exposes the coauthors/v1 API; otherwise "
            "pass --byline-field with the field that holds the byline, or import a "
            "WordPress export file, which carries bylines without the REST API.",
        ))
    elif total > 1 and len(names) == 1 and len(bylined) == total:
        found.append(ImportFinding(
            True,
            f"All {total} articles are credited to {sorted(names)[0]!r}.",
            "One name on everything means the byline field isn't computed per article. "
            "Yoast's yoast_head_json behaves this way in a list response, so "
            "--byline-field yoast_head_json.author reads the same name every time. Drop "
            "that option, point it at a field that varies per article, or import a "
            "WordPress export file instead.",
        ))

    composite = sorted(a for a in names if re.search(r"\s+and\s+|,", a))
    if composite:
        found.append(ImportFinding(
            False,
            f"{len(composite)} byline(s) hold more than one name in one string, "
            f"e.g. {composite[0]!r}.",
            "An author filter matches a whole byline, so neither writer is findable under "
            "their own name. Split them into separate names before indexing.",
        ))

    ids = [d.get("id") for d in docs if d.get("id")]
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    if repeated:
        found.append(ImportFinding(
            True,
            f"{len(repeated)} article id(s) appear more than once, e.g. {repeated[0]!r}.",
            "Indexing keeps one article per id, so the others are lost. If you are merging "
            "two sources, import them separately with --prefix.",
        ))

    unlinked = [d for d in docs if not str(d.get("url") or "").startswith(("http://", "https://"))]
    if unlinked:
        found.append(ImportFinding(
            False,
            f"{len(unlinked)} of {total} articles have no usable url.",
            "Answers cite the url, so those articles can be found but not cited. Check that "
            "the importer is reading the public link and not an internal path.",
        ))

    dates = {str(d.get("publish_date") or "")[:10] for d in docs}
    if total > 1 and len(dates) == 1:
        found.append(ImportFinding(
            False,
            f"Every article has the publish date {sorted(dates)[0]!r}.",
            "Date filters can't narrow anything. Check that the field is the publish date "
            "and not the import or last-modified date.",
        ))

    # A byline can be present, varied and still wrong, which no check on the data
    # can see. The WordPress post author is whoever published the story in the CMS,
    # which on some sites is an editor rather than the reporter -- so say where the
    # names came from and ask for a spot-check.
    if plan is not None and plan.source == "wordpress-api" and bylined \
            and not any(a.startswith("--byline-field") for a in plan.args):
        found.append(ImportFinding(
            False,
            "Bylines came from each post's WordPress author field.",
            "On some sites that field holds whoever published the story rather than who "
            "wrote it, and a wrong name imports as cleanly as a right one. Open two or "
            "three of the articles above on the live site and check the bylines match. "
            "If they don't, the real byline is usually in a plugin's own field: pass "
            "--byline-field with it, or import a WordPress export file.",
        ))

    lengths = sorted(len(str(d.get("content") or "")) for d in docs)
    median = lengths[len(lengths) // 2]
    if median < 400:
        found.append(ImportFinding(
            True,
            f"The median article is {median} characters long.",
            "That is the length of an excerpt, not an article, so retrieval has almost "
            "nothing to match on. Check that the content field is the full body text.",
        ))

    return found


def report_import_review(docs: List[dict], plan: Optional["ImportPlan"] = None) -> bool:
    """Print what the review found. False when something needs looking at first."""
    if not docs:
        return True
    findings = review_import(docs, plan)
    if not findings:
        say("  " + paint("Checked", BRAND) + f" {len(docs)} article(s): bylines, ids, links, "
            "dates and article length all look usable.")
        return True
    say()
    say(paint(f"Checked {len(docs)} imported article(s) and found:", BRAND, BOLD))
    for finding in findings:
        mark = paint("needs a look", GOLD) if finding.stop else paint("worth knowing", DIM)
        say(f"  [{mark}] {finding.saw}")
        for line in textwrap.wrap(finding.fix, 74):
            say(f"      {line}")
    return not any(f.stop for f in findings)


def index_archive(values: dict) -> None:
    heading("Index your archive")
    data = sorted((ROOT / "data").glob("*.json"))
    if not data:
        say("No .json files in data/ yet. Add your articles there (format in the README), then run:")
    else:
        say(f"Found {len(data)} JSON file(s) in data/.")
    script = "app/setup.py" if values["DEWEY_STACK"] == "azure" else "app/setup_postgres.py"
    say(f"  python {script}")
    if data and ask_yes("Index them now?", default=False):
        run([sys.executable, script])


# ---------------------------------------------------------------- archive import

IMPORT_SCRIPT = "app/import_archive.py"
TRIAL_LIMIT = 20

# Answers to "Where is your archive?". The first six are the importers' NAMEs
# in app/importers/ (tests/install checks that they match).
IMPORT_SOURCES = [
    ("wordpress-api", "A live WordPress site, read through its REST API. Public posts need no login."),
    ("wordpress-export", "A WordPress export file (wp-admin > Tools > Export), read offline."),
    ("feed", "An RSS or Atom feed from any CMS. Feeds often reach back only a few pages."),
    ("csv", "A CSV file (from a spreadsheet, a database or a CMS), one row per article."),
    ("drupal", "A Drupal 8.7+ site, through its JSON:API module. "
               "Beta: tested against sample exports only."),
    ("ghost", "A Ghost site, through its Content API (needs a Content API key). "
              "Beta: tested against sample exports only."),
    ("json", "I already have article JSON files in data/."),
    ("skip", "Skip this for now."),
]

# Environment variables the importers read logins and keys from (USER_ENV,
# PASSWORD_ENV and KEY_ENV in app/importers/wordpress_api.py, drupal.py and
# ghost.py; tests/install checks that they match). Secrets reach the importer
# only this way: command-line flags show up in the process list and in shell
# history, and the commands below are printed.
IMPORTER_ENV = {
    "wordpress-api": ("WORDPRESS_USER", "WORDPRESS_APP_PASSWORD"),
    "drupal": ("DRUPAL_USER", "DRUPAL_PASSWORD"),
    "ghost": ("GHOST_CONTENT_API_KEY",),
}
# Importer flags not accepted as extra options: the ones that carry a secret
# (the wizard asks for those hidden) and the ones the wizard sets itself.
SECRET_FLAGS = ("--app-password", "--password", "--key", "--content-api-key")
MANAGED_FLAGS = ("--limit", "--out-dir", "--prefix")
# Query parameters that hold a key or token (the same list as importers/common.py).
SECRET_PARAMS = {"key", "api_key", "apikey", "token", "access_token", "password", "secret"}
EXTRA_OPTION_EXAMPLES = {
    "wordpress-api": "--post-type story  or  --after 2020-01-01",
    "wordpress-export": "--post-type story  or  --timezone America/New_York",
    "feed": "--require-full-text  or  --default-timezone America/New_York",
    "csv": "--date-format '%m/%d/%Y'  or  --headline-col Title  or  --encoding cp1252",
    "drupal": "--type story  or  --byline-field field_byline",
    "ghost": "--after 2020-01-01  or  --include-pages",
}


@dataclass
class ImportPlan:
    source: str                                             # importer NAME
    args: List[str]                                         # its options; never a secret
    env: Dict[str, str] = field(default_factory=dict)       # logins/keys, passed in the importer's environment
    env_lines: List[str] = field(default_factory=list)      # shell lines that set them again, without the values
    answers: Dict[str, str] = field(default_factory=dict)   # offered as defaults when trying again


def shell_join(cmd) -> str:
    return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


def importer_command(plan: ImportPlan, limit: Optional[int] = None, python: str = "python") -> List[str]:
    cmd = [python, IMPORT_SCRIPT, plan.source, *plan.args]
    return cmd + ["--limit", str(limit)] if limit else cmd


def importer_env(plan: ImportPlan) -> Dict[str, str]:
    """This process's environment without importer logins, plus the ones given in this wizard."""
    logins = {name for names in IMPORTER_ENV.values() for name in names}
    env = {k: v for k, v in os.environ.items() if k not in logins}
    env.update(plan.env)
    env["PYTHONUNBUFFERED"] = "1"  # keeps progress lines and the report in order when output is piped
    return env


def read_secret_line(name: str) -> str:
    return f"read -rs {name} && export {name}   # paste it, press Enter; it isn't shown or saved"


def path_from_input(raw: str) -> Path:
    """A path as typed or dragged into the terminal: quotes, ~ and backslash-escaped spaces handled."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    path = Path(raw).expanduser()
    if not path.exists() and "\\ " in raw:  # macOS Terminal escapes spaces in dragged-in paths
        path = Path(raw.replace("\\ ", " ")).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def clean_url(raw: str, key_param: bool = False):
    """
    Check a web address typed for an importer. Returns (url, key, problem):
    https:// is added when no scheme is given; with key_param (Ghost), a ?key=
    value is moved out of the URL into `key` so it never appears in a command.
    """
    url = raw.strip()
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", url):
        url = "https://" + url
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
    except ValueError:
        parts, host = None, None
    if parts is None or not host or parts.scheme.lower() not in ("http", "https") or re.search(r"\s", url):
        return "", "", "That doesn't look like a web address; try again, e.g. https://www.example.com"
    if "@" in parts.netloc:
        return "", "", ("Leave the username and password out of the address. If the site needs a login, "
                        "you'll be asked for it separately (hidden).")
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    secret = [(k, v) for k, v in query if k.lower() in SECRET_PARAMS]
    key = ""
    if secret:
        if not key_param:
            return "", "", "That address contains a key or token (?key=, ?token=...); leave that part out."
        key = next((v.strip() for k, v in secret if k.lower() == "key"), "")
        rest = [(k, v) for k, v in query if k.lower() not in SECRET_PARAMS]
        url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(rest)))
    return url, key, None


def ask_or_skip(prompt: str, default: str = "") -> str:
    return ask(prompt if default else f"{prompt} (Enter to skip importing for now)", default)


def ask_url(prompt: str, default: str = "", key_param: bool = False):
    """Returns (url, key from the URL); url is "" when the user skips."""
    while True:
        raw = ask_or_skip(prompt, default)
        if not raw:
            return "", ""
        url, key, problem = clean_url(raw, key_param)
        if not problem:
            return url, key
        say("  " + problem)
        default = ""


def ask_existing_path(prompt: str, default: str = "", allow_dir: bool = False) -> Optional[Path]:
    while True:
        raw = ask_or_skip(prompt, default)
        if not raw:
            return None
        path = path_from_input(raw)
        if path.is_file() or (allow_dir and path.is_dir()):
            return path.resolve()
        say(f"  Couldn't find {'a file or folder' if allow_dir else 'a file'} at {path}.")
        say("  Tip: drag the file into this window to paste its full path.")
        default = ""


def secret_default(prev: Optional[ImportPlan], name: str):
    """(value, description) offered when Enter is pressed at a hidden prompt."""
    if prev and prev.env.get(name):
        return prev.env[name], "the one you typed before"
    if os.environ.get(name):
        return os.environ[name], f"the one already set in {name}"
    return "", ""


def add_login(plan: ImportPlan, prev: Optional[ImportPlan], user_env: str, secret_env: str,
              user_label: str, secret_label: str) -> None:
    user_default = (prev.env.get(user_env) if prev else "") or os.environ.get(user_env, "")
    user = ask(user_label, user_default, required=True)
    existing, kept = secret_default(prev, secret_env)
    secret = ask_secret(f"{secret_label} (hidden)", existing, kept)
    if not secret:
        say(f"  No {secret_label}, so the importer reads only what the public can see.")
        return
    plan.env.update({user_env: user, secret_env: secret})
    plan.answers["user"] = user
    plan.env_lines += [f"export {user_env}={shlex.quote(user)}", read_secret_line(secret_env)]


def plan_wordpress_api(prev: Optional[ImportPlan]) -> Optional[ImportPlan]:
    a = prev.answers if prev else {}
    say("Reads published, public posts from your live site. To check the address, open")
    say('<your site>/wp-json/ in a browser: you should see text starting with {"name":')
    site, _ = ask_url("Your site's public address, e.g. https://www.example.com", a.get("site", ""))
    if not site:
        return None
    plan = ImportPlan("wordpress-api", ["--site", site], answers={"site": site})
    say("A login is only needed if that check shows an error instead (the site closes its API")
    say("to the public).")
    if ask_yes("Does the site need a login for its API?", default=bool(a.get("user"))):
        say("  Use an Application Password, not your normal password: wp-admin > Users > Profile >")
        say("  Application Passwords, type a name such as Dewey, click 'Add Application Password'")
        say("  ('Add New Application Password' before WordPress 6.8)")
        say("  and copy the code (it is shown only once).")
        add_login(plan, prev, *IMPORTER_ENV["wordpress-api"], "WordPress username", "Application Password")
    return plan


def plan_wordpress_export(prev: Optional[ImportPlan]) -> Optional[ImportPlan]:
    a = prev.answers if prev else {}
    say("In wp-admin: Tools > Export, choose 'All content' and click 'Download Export File'.")
    say("A .xml, .xml.gz or .zip file works, or a folder of them (big sites may come in parts).")
    path = ask_existing_path("Path to the export file or folder", a.get("file", ""), allow_dir=True)
    if path is None:
        return None
    return ImportPlan("wordpress-export", ["--file", str(path)], answers={"file": str(path)})


def plan_feed(prev: Optional[ImportPlan]) -> Optional[ImportPlan]:
    a = prev.answers if prev else {}
    say("Feeds usually list only the latest stories. The importer follows links to older pages")
    say("when the feed has them, but for a whole WordPress, Drupal or Ghost archive their own")
    say("importers reach further back. Usual addresses: WordPress <site>/feed/, Drupal")
    say("<site>/rss.xml, Ghost <site>/rss/. Your homepage works if it advertises its feed.")
    default = a.get("feed", "")
    while True:
        raw = ask_or_skip("Feed address, or the path to a saved feed file or folder", default)
        if not raw:
            return None
        default = ""
        path = path_from_input(raw)
        if path.exists():
            return ImportPlan("feed", ["--file", str(path.resolve())], answers={"feed": raw})
        if raw.startswith(("/", "~", ".", "\\")) or re.match(r"^[A-Za-z]:[\\/]", raw):
            say(f"  Couldn't find a file or folder at {path}.")
            continue
        url, _, problem = clean_url(raw)
        if problem:
            say("  " + problem)
            continue
        return ImportPlan("feed", ["--url", url], answers={"feed": url})


def plan_csv(prev: Optional[ImportPlan]) -> Optional[ImportPlan]:
    a = prev.answers if prev else {}
    say("One row per article, with a header row. Save or download spreadsheets as CSV first")
    say("(choose UTF-8 if your program offers it).")
    say("The importer finds the columns by their names and prints the mapping first: check it.")
    path = ask_existing_path("Path to the CSV file", a.get("file", ""))
    if path is None:
        return None
    return ImportPlan("csv", ["--file", str(path)], answers={"file": str(path)})


def plan_drupal(prev: Optional[ImportPlan]) -> Optional[ImportPlan]:
    a = prev.answers if prev else {}
    say("Beta: tested against sample exports only, not yet against a live newsroom site. Check")
    say("the trial run's articles (headlines, bylines, dates, links) before importing everything.")
    say("Needs Drupal 8.7+ with the JSON:API module turned on: <your site>/jsonapi should show")
    say('JSON whose "links" list "node--article".')
    site, _ = ask_url("Your site's public address, e.g. https://www.example.com", a.get("site", ""))
    if not site:
        return None
    plan = ImportPlan("drupal", ["--site", site], answers={"site": site})
    say("A login is only needed if <your site>/jsonapi refuses visitors who aren't logged in.")
    if ask_yes("Does it need a login?", default=bool(a.get("user"))):
        say("  Needs Drupal's core 'HTTP Basic Authentication' module. Use an account with no extra")
        say("  roles (People > Add user, e.g. dewey-reader): what it can see is what gets imported.")
        add_login(plan, prev, *IMPORTER_ENV["drupal"], "Drupal username", "Password for that account")
    return plan


def plan_ghost(prev: Optional[ImportPlan]) -> Optional[ImportPlan]:
    a = prev.answers if prev else {}
    (key_env,) = IMPORTER_ENV["ghost"]
    say("Beta: tested against sample exports only, not yet against a live newsroom site. Check")
    say("the trial run's articles (headlines, bylines, dates, links) before importing everything.")
    say("In Ghost Admin (your site address + /ghost): Settings > Integrations (under Advanced in")
    say("recent versions) > 'Add custom integration', name it Dewey. Copy its API URL and its")
    say("Content API key (26 letters and digits; not the Admin API key, which has a colon).")
    url, key_from_url = ask_url("The integration's API URL (or your site's address)", a.get("url", ""),
                                key_param=True)
    if not url:
        return None
    if key_from_url:
        say("  Took the key= part out of the address; it's used as the Content API key below.")
    existing, kept = (key_from_url, "the key from the address") if key_from_url else secret_default(prev, key_env)
    key = ask_secret("Content API key (hidden)", existing, kept)
    if not key:
        say("  The Ghost importer can't run without a Content API key; skipping the import for now.")
        return None
    return ImportPlan("ghost", ["--url", url], env={key_env: key}, env_lines=[read_secret_line(key_env)],
                      answers={"url": url})


PLAN_QUESTIONS = {
    "wordpress-api": plan_wordpress_api,
    "wordpress-export": plan_wordpress_export,
    "feed": plan_feed,
    "csv": plan_csv,
    "drupal": plan_drupal,
    "ghost": plan_ghost,
}


_LOGIN_PREFIX = re.compile(r"^[^/\s:@]+:[^/\s@]*@")  # user:pass@host, as importers/feed.py checks


def address_has_secret(value: str) -> bool:
    """True for a web address carrying a login (user:pass@) or a key/token query parameter."""
    if "://" not in value and not _LOGIN_PREFIX.match(value):
        return False
    if _LOGIN_PREFIX.match(value):
        return True
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    return "@" in parts.netloc or any(k.lower() in SECRET_PARAMS for k, _ in query)


def extra_options_problem(tokens: List[str], source: str = "<source>") -> Optional[str]:
    for token in tokens:
        # Values too (`--url X` and `--url=X`): an address with a login or ?key= would put a
        # secret on the command line, which is printed. The importers accept some of them.
        if address_has_secret(token.split("=", 1)[1] if token.startswith("--") and "=" in token else token):
            return ("That contains a password, key or token in a web address, which would put it on the "
                    "command line. Choose 'change' and answer the login or key question instead; that is "
                    "typed hidden.")
        if token == "-h":
            token = "--help"
        if not token.startswith("--") or token == "--":
            continue
        name = token.split("=", 1)[0]
        if "--help".startswith(name):
            return f"To see every option, run: python {IMPORT_SCRIPT} {source} --help (it imports nothing)."
        # argparse also accepts any unambiguous abbreviation (--app-pass), so check prefixes.
        for flag in SECRET_FLAGS:
            if flag.startswith(name):
                return (f"{flag} would put a secret on the command line. Choose 'change' and answer the "
                        "login or key question instead; that is typed hidden.")
        for flag in MANAGED_FLAGS:
            if flag.startswith(name):
                return (f"This setup sets {flag} itself. To choose it, run the importer yourself with the "
                        "command shown at the end.")
    return None


def ask_extra_options(plan: ImportPlan, prev: ImportPlan) -> None:
    say(f"You can add importer options, e.g. {EXTRA_OPTION_EXAMPLES[plan.source]}")
    say(f"(every option: python {IMPORT_SCRIPT} {plan.source} --help). The message above often names one.")
    default = prev.answers.get("extra", "")
    while True:
        raw = ask("Extra options (Enter for none" + (", '-' to drop these" if default else "") + ")", default)
        if not raw or raw == "-":
            return
        try:
            tokens = shlex.split(raw, posix=os.name != "nt")
        except ValueError as e:
            say(f"  Couldn't read that ({e}); check the quotes.")
            default = ""
            continue
        problem = extra_options_problem(tokens, plan.source)
        if problem:
            say("  " + problem)
            default = ""
            continue
        plan.args += tokens
        plan.answers["extra"] = raw
        return


def run_importer(plan: ImportPlan, limit: Optional[int] = None) -> bool:
    cmd = importer_command(plan, limit, python=sys.executable)
    say("  $ " + shell_join(cmd))  # no secret in it: those are in the environment
    try:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=importer_env(plan))
    except OSError as e:
        say(f"  Couldn't start the importer: {e}")
        return False
    try:
        return proc.wait() == 0
    except KeyboardInterrupt:
        # The importer got Ctrl+C too; let it remove its temporary files before stopping.
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise


def json_snapshot(folder: Path) -> Dict[str, int]:
    try:
        return {p.name: p.stat().st_mtime_ns for p in folder.glob("*.json")}
    except OSError:
        return {}


def show_import_commands(plan: ImportPlan, trial: bool = False, again: bool = False) -> None:
    say()
    say(f"To run this import{' again' if again else ''} later, from the Dewey folder ({ROOT}):")
    for line in plan.env_lines:
        say("  " + line)
    if trial:
        say("  " + shell_join(importer_command(plan, TRIAL_LIMIT)) + "   # trial run")
    say("  " + shell_join(importer_command(plan)))
    if plan.env_lines and os.name == "nt":
        say("  (Windows: set those environment variables in your terminal instead of using export/read;")
        say("  never put the password or key on the command line.)")
    say("Re-running reads everything again and replaces the files from the last run of the same")
    say("import. Stories deleted or unpublished in your CMS stay in Dewey's index until you rebuild")
    say("it (see 'Importing your archive' in the README).")


def offer_trial_cleanup(files) -> None:
    files = sorted(p for p in files if p.exists())
    if not files:
        return
    say(f"The trial run wrote {len(files)} sample file(s): {', '.join(p.name for p in files)}")
    say("They're only samples. Indexing them next to the full import repeats work (each article is")
    say("stored once, but embedded again).")
    if ask_yes("Remove the sample file(s)?", default=True):
        for p in files:
            try:
                p.unlink()
            except OSError as e:
                say(f"  Couldn't remove {p.name}: {e}")
        say("  Removed.")


def get_archive(dry_run: bool = False) -> None:
    heading("Get your archive into Dewey")
    data_dir = ROOT / "data"
    existing = sorted(data_dir.glob("*.json"))
    say("Dewey searches article files (JSON) in the data/ folder. An importer can make them from")
    say("your CMS, a feed or a spreadsheet. Importers only read: they never change your site, and")
    say("they keep published, public stories only. A small trial run comes first.")
    if existing:
        say(f"data/ already has {len(existing)} JSON file(s). An import adds its own files; importing")
        say("the same source again replaces that source's earlier files.")
    source = choose("Where is your archive?", IMPORT_SOURCES, default="json" if existing else "skip",
                    mark="default")
    if source == "skip":
        say("Skipped. To import later, run this from the Dewey folder to see the options:")
        say(f"  python {IMPORT_SCRIPT} --help")
        return
    if source == "json":
        if existing:
            say(f"Using the {len(existing)} JSON file(s) already in data/.")
        else:
            say("data/ has no .json files yet. Put your article files there (format in the README).")
        return

    prev: Optional[ImportPlan] = None
    trial_files = set()
    while True:
        plan = PLAN_QUESTIONS[source](prev)
        if plan is None:
            say(f"Skipped. To import later: python {IMPORT_SCRIPT} {source} --help")
            return
        if prev is not None:
            ask_extra_options(plan, prev)
        if dry_run:
            say("(--dry-run: nothing is imported. These are the commands this would run.)")
            show_import_commands(plan, trial=True)
            return

        if ask_yes(f"Try a small import first ({TRIAL_LIMIT} articles, saved as separate -sample files)?",
                   default=True):
            before = json_snapshot(data_dir)
            trial_ok = run_importer(plan, limit=TRIAL_LIMIT)
            after = json_snapshot(data_dir)
            trial_files |= {data_dir / n for n, m in after.items() if before.get(n) != m and "-sample-" in n}
            say()
            review_ok = True
            if trial_ok:
                say("Check the report above (how many articles, their dates, missing bylines, what was")
                say("skipped and why), and open the -sample file in data/ to check a few stories.")
                review_ok = report_import_review(load_imported(trial_files), plan)
            else:
                say("The trial stopped; the message above says what to change.")
            next_step = choose("What next?", [
                ("full", "Import everything with these settings"),
                ("change", "Change the answers (or add importer options) and try again"),
                ("stop", "Stop here; the command to import later is shown next"),
            ], default="full" if trial_ok and review_ok else "change", mark="default")
        else:
            next_step = "full"

        if next_step == "full":
            say("A big archive can take a while. Ctrl+C stops it (and this setup) without changing data/.")
            if run_importer(plan):
                say()
                offer_trial_cleanup(trial_files)
                show_import_commands(plan, again=True)
                return
            next_step = choose("The import stopped; the message above says why. What next?", [
                ("change", "Change the answers (or add importer options) and try again"),
                ("stop", "Stop here; the command to import later is shown next"),
            ], default="change", mark="default")
        if next_step == "change":
            prev = plan
            continue
        if trial_files:
            say("The trial articles stay in data/: indexing them is a quick way to try Dewey first.")
        show_import_commands(plan, trial=True)
        return


def missing_required(values: dict) -> list:
    stack = values["DEWEY_STACK"]
    need = []
    if stack == "anthropic":
        need += ["ANTHROPIC_API_KEY", "DATABASE_URL"]
    if stack == "open":
        need += ["OPEN_LLM_MODEL", "DATABASE_URL"]
    if stack in ("anthropic", "open") and "voyage" in (values.get("EMBEDDING_PROVIDER"), values.get("RERANK_PROVIDER")):
        need.append("VOYAGE_API_KEY")
    if stack == "azure":
        need += [k for k in SECTIONS[-1][1]]
    return [k for k in need if not values.get(k)]


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive setup for Dewey.")
    parser.add_argument("--dry-run", action="store_true",
                        help="ask the questions, then print the .env (secrets masked) instead of writing it")
    parser.add_argument("--no-browser", action="store_true", help="never offer to open links in a browser")
    args = parser.parse_args()
    interactive = not args.no_browser

    say()
    say("  " + paint("DEWEY", BRAND, BOLD) + paint("  ·  newsroom archive setup", DIM))
    say("  " + paint("─" * 44, DEEP, DIM))
    say("This walks you through choosing a stack, getting any accounts and keys it needs,")
    say("and writing your .env file. Secrets are typed hidden and stored only in .env.")
    if sys.version_info < MIN_PYTHON:
        say(f"\nDewey needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+; this is {sys.version.split()[0]}. "
            "Install a newer Python from https://www.python.org/downloads/ and re-run.")
        return 1

    current = read_env(ENV_PATH)
    if current:
        say("\nFound an existing .env; its values are offered as defaults.")

    values = {"DEWEY_STACK": ask_stack(current)}
    stack = values["DEWEY_STACK"]

    if stack == "anthropic":
        values.update(ask_anthropic(current, interactive))
    if stack == "open":
        values.update(ask_open_llm(current, interactive))
    if stack in ("anthropic", "open"):
        values.update(ask_retrieval(stack, current))
        if "voyage" in (values["EMBEDDING_PROVIDER"], values["RERANK_PROVIDER"]):
            values.update(ask_voyage(current, interactive))
        values.update(ask_postgres(current, interactive))
    if stack == "azure":
        values.update(ask_azure(current, interactive))
    values.update(ask_newsroom(current))

    # Write only non-empty answers (plus an explicit empty DEFAULT_REGION). Settings for
    # other stacks are dropped; unrelated keys already in .env are kept as they were.
    managed = {k for _, keys in SECTIONS for k in keys}
    to_write = {k: v for k, v in values.items() if v or (k == "DEFAULT_REGION" and v == "")}
    content = render_env(to_write, {k: v for k, v in current.items() if k not in managed})

    heading("Summary")
    missing = missing_required(values)
    say(redact(content))
    if missing:
        say(f"Still missing: {', '.join(missing)}. Dewey won't start until these are set; "
            "re-run this installer or edit .env when you have them.")

    postgres_stack = stack in ("anthropic", "open")
    if args.dry_run:
        say("\n(--dry-run: nothing was written.)")
        if postgres_stack:
            get_archive(dry_run=True)
        return 0
    if not ask_yes("Write this to .env?", default=True):
        say("Nothing written.")
        return 0
    write_env(content)

    install_dependencies(values)
    if not missing and ask_yes("Check your credentials and services now?", default=True):
        check_services(values)
    if postgres_stack:
        get_archive()  # importers need no keys or database, so this runs even with settings missing
    if not missing:
        index_archive(values)

    heading("Next steps")
    if missing:
        say(f"1. Fill in: {', '.join(missing)} (re-run python install.py)")
        if postgres_stack:
            say("2. Index your archive:  python app/setup_postgres.py")
    say("Start Dewey:  python app/main.py   then open http://localhost:7860")
    say("Before relying on answers, ask questions you already know the answers to and check")
    say("each citation against the article it links to.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\nCancelled; nothing further was changed.")
        sys.exit(130)

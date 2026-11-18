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
  4. optionally installs Python dependencies, checks the credentials and
     services it can reach, and indexes the articles in data/.

Re-running it is safe: values already in .env are offered as defaults, so you
can change one option without re-entering everything.
"""
import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

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
               "AZURE_SEARCH_API_KEY", "AZURE_STORAGE_CONNECTION_STRING", "DATABASE_URL"}


# ---------------------------------------------------------------- terminal UI

def say(text: str = "") -> None:
    print(text)


def heading(text: str) -> None:
    say()
    say(f"── {text} " + "─" * max(0, 60 - len(text)))


def ask(prompt: str, default: str = "", required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            value = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            value = ""
        value = value or default
        if value or not required:
            return value
        say("  This one is required.")


def ask_secret(prompt: str, existing: str = "") -> str:
    """Hidden input. Enter keeps an existing value; '-' clears it."""
    hint = " [Enter keeps the saved value, '-' clears it]" if existing else " [Enter to skip for now]"
    try:
        value = getpass.getpass(f"{prompt}{hint}: ").strip()
    except EOFError:
        value = ""
    if value == "-":
        return ""
    return value or existing


def ask_yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        value = ask(f"{prompt} ({d})").lower()
        if not value:
            return default
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False


def choose(prompt: str, options, default: str) -> str:
    """options: list of (value, description)."""
    say(prompt)
    for i, (value, desc) in enumerate(options, 1):
        marker = " (current)" if value == default else ""
        say(f"  {i}. {value}{marker}: {desc}")
    values = [v for v, _ in options]
    while True:
        raw = ask("Choose a number or name", default)
        if raw in values:
            return raw
        if raw.isdigit() and 1 <= int(raw) <= len(values):
            return values[int(raw) - 1]
        say(f"  Please enter 1-{len(values)} or one of: {', '.join(values)}")


def offer_link(label: str, url: str, interactive: bool = True) -> None:
    say(f"  {label}: {url}")
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
        values[key.strip()] = value
    return values


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
    # Keep anything already in .env that this installer doesn't manage.
    others = {k: v for k, v in extra_existing.items() if k not in written and k not in values}
    if others:
        lines += ["", "# Kept from your previous .env"]
        lines += [f"{k}={quote(v)}" for k, v in others.items()]
    return "\n".join(lines) + "\n"


def redact(text: str) -> str:
    out = []
    for line in text.splitlines():
        key = line.split("=", 1)[0]
        out.append(f"{key}='********'" if key in SECRET_KEYS and "=" in line else line)
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
    return out


def ask_postgres(cur: dict, interactive: bool) -> dict:
    heading("Postgres with pgvector (where your archive is stored and searched)")
    say("Dewey needs a Postgres database with the pgvector extension available. Options:")
    say("  a) Docker, quickest for a laptop:")
    say(f"       docker run -d --name dewey-db -e POSTGRES_PASSWORD=dewey -p 5432:5432 {PGVECTOR_DOCKER_IMAGE}")
    say("     then use: postgresql://postgres:dewey@localhost:5432/postgres")
    say("     (Change the password for anything beyond local testing.)")
    say("  b) Your own Postgres server, with pgvector installed")
    say("  c) A managed Postgres service that supports the pgvector extension")
    say("     Or on macOS with Homebrew Postgres: brew install pgvector")
    if not shutil.which("docker"):
        offer_link("Docker isn't installed; get Docker", LINKS["docker"], interactive)
    offer_link("pgvector installation guide", LINKS["pgvector"], interactive)
    url = ask_secret("Database URL (postgresql://user:password@host:port/dbname)", cur.get("DATABASE_URL", ""))
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


def http_get_json(url: str, headers=None, timeout: float = 5.0):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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

    say("Dewey setup")
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

    if args.dry_run:
        say("\n(--dry-run: nothing was written.)")
        return 0
    if not ask_yes("Write this to .env?", default=True):
        say("Nothing written.")
        return 0
    write_env(content)

    install_dependencies(values)
    if not missing and ask_yes("Check your credentials and services now?", default=True):
        check_services(values)
    if not missing:
        index_archive(values)

    heading("Next steps")
    if missing:
        say(f"1. Fill in: {', '.join(missing)} (re-run python install.py)")
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

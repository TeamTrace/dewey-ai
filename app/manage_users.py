"""
Manage the accounts that can log in to Dewey. Standard library only.

    python app/manage_users.py add NAME       # asks for the password twice
    python app/manage_users.py remove NAME
    python app/manage_users.py list           # user names only

Edits the users file: DEWEY_USERS_FILE if set (in the environment or in .env,
which Dewey also reads), otherwise users.txt in the repository root (pass
--file to use another). Only a salted scrypt hash of each password is stored;
this tool never prints passwords or hashes. Dewey reads the file when it
starts, so restart Dewey after a change.

See docs/DEPLOYMENT.md for using these accounts on Docker, Fly.io and Render.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path
from typing import Callable, List, Optional, TextIO, Tuple

import auth

REPO_ROOT = auth.REPO_ROOT
HEADER = (
    "# Dewey login accounts: one per line, name:hash. Passwords are not stored.\n"
    "# Edit with: python app/manage_users.py add|remove|list\n"
)
MAX_ATTEMPTS = 3

_DOTENV_LINE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class UsageError(Exception):
    pass


def dotenv_value(name: str, path: Optional[Path] = None) -> Optional[str]:
    """`name` as set in the repo's .env, or None. main.py loads .env with
    python-dotenv, which this standard-library tool can't use; this reads the
    simple NAME=value / NAME='value' lines install.py and .env.template write.
    The last assignment wins, as in python-dotenv."""
    path = REPO_ROOT / ".env" if path is None else path
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    value = None
    for line in text.splitlines():
        match = _DOTENV_LINE.match(line)
        if not match or match.group(1) != name:
            continue
        raw = match.group(2).strip()
        if raw[:1] in ("'", '"'):
            end = raw.find(raw[0], 1)
            value = raw[1:end] if end != -1 else raw[1:]
        else:
            value = raw.split(" #", 1)[0].strip()
    return value


def setting(name: str) -> Tuple[Optional[str], str]:
    """(value, where it comes from) the way Dewey sees it: the environment first
    (python-dotenv doesn't override it), then .env."""
    if name in os.environ:
        return os.environ[name], "this shell's environment"
    return dotenv_value(name), str(REPO_ROOT / ".env")


def default_users_file() -> Path:
    value, _ = setting(auth.USERS_FILE_ENV)
    path, _ = auth.users_file_path({auth.USERS_FILE_ENV: value or ""}, repo_root=REPO_ROOT)
    return path


def _read_lines(path: Path) -> List[str]:
    try:
        path.stat()
    except FileNotFoundError:
        return []
    except OSError:
        pass  # read_users_text explains the problem
    try:
        return auth.read_users_text(path).splitlines()
    except auth.UsersConfigError as e:
        raise UsageError(str(e)) from None


def _entry_name(line: str) -> Optional[str]:
    """The user name a line defines, or None for blank and comment lines."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    return stripped.partition(":")[0]


def _write(path: Path, lines: List[str]) -> None:
    text = "\n".join(lines).rstrip("\n") + "\n"
    try:
        auth.write_text_atomic(path, text)
    except OSError as e:
        raise UsageError(f"Can't write {path}: {e.strerror or e}") from None


def ask_new_password(
    name: str,
    prompt: Callable[[str], str] = getpass.getpass,
    out: Optional[TextIO] = None,
) -> str:
    """Ask twice, hidden. Retries a few times on a short or mismatched password."""
    out = sys.stderr if out is None else out
    for _ in range(MAX_ATTEMPTS):
        password = prompt(f"New password for {name} (at least {auth.MIN_PASSWORD_LENGTH} characters): ")
        if len(password) < auth.MIN_PASSWORD_LENGTH:
            print(f"Too short: use at least {auth.MIN_PASSWORD_LENGTH} characters. "
                  "A few unrelated words make a good one.", file=out)
            continue
        if len(password) > auth.MAX_PASSWORD_LENGTH:
            print(f"Too long: use at most {auth.MAX_PASSWORD_LENGTH} characters.", file=out)
            continue
        if password.strip().lower() == name.lower():
            print("The password can't be the user name.", file=out)
            continue
        if prompt("Repeat the password: ") != password:
            print("The two passwords don't match.", file=out)
            continue
        return password
    raise UsageError("No password set.")


def add_user(
    path: Path,
    name: str,
    prompt: Callable[[str], str] = getpass.getpass,
    confirm: Callable[[str], str] = input,
    out: Optional[TextIO] = None,
) -> None:
    out = sys.stdout if out is None else out
    if not auth.valid_username(name):
        raise UsageError(f"User name must be {auth.USERNAME_RULE}.")
    lines = _read_lines(path)
    existing = [i for i, line in enumerate(lines) if _entry_name(line) == name]
    if existing:
        answer = confirm(f"{name} already exists. Replace their password? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Nothing changed.", file=out)
            return
    password = ask_new_password(name, prompt)
    entry = f"{name}:{auth.hash_password(password)}"
    if existing:
        lines[existing[0]] = entry
        for i in reversed(existing[1:]):
            del lines[i]
        verb = "Changed the password for"
    else:
        if not lines:
            lines = HEADER.splitlines()
        lines.append(entry)
        verb = "Added"
    _write(path, lines)
    print(f"{verb} {name} in {path}.", file=out)
    _remind(out)


def remove_user(path: Path, name: str, out: Optional[TextIO] = None) -> None:
    out = sys.stdout if out is None else out
    lines = _read_lines(path)
    kept = [line for line in lines if _entry_name(line) != name]
    if len(kept) == len(lines):
        _warn_if_users_env_overrides(name)
        raise UsageError(f"No user named {name!r} in {path}.")
    _write(path, kept)
    print(f"Removed {name} from {path}.", file=out)
    _remind(out, name)


def list_users(path: Path, out: Optional[TextIO] = None) -> None:
    out = sys.stdout if out is None else out
    _warn_if_users_env_overrides()
    lines = _read_lines(path)
    if not lines and not path.exists():
        print(f"No users file at {path}. Add a user with: python app/manage_users.py add NAME", file=out)
        return
    parsed = auth.parse_users("\n".join(lines))
    for problem in parsed.problems:
        print(f"warning: {problem}", file=sys.stderr)
    names = sorted(parsed.records)
    print(f"{len(names)} user(s) in {path}" + (":" if names else "."), file=out)
    for name in names:
        print(f"  {name}", file=out)


def _remind(out: TextIO, name: Optional[str] = None) -> None:
    print("Restart Dewey to apply the change (accounts are read at startup).", file=out)
    _warn_if_users_env_overrides(name)


def _warn_if_users_env_overrides(name: Optional[str] = None) -> None:
    """Dewey ignores the users file while DEWEY_USERS is set (in the environment or
    .env), so an edit here would silently not apply. Names only, never hashes."""
    inline, where = setting(auth.USERS_ENV)
    if not (inline and inline.strip()):
        return
    message = (f"Warning: {auth.USERS_ENV} is set in {where}, so Dewey uses it instead of the users file "
               f"and this change does not apply there. Update {auth.USERS_ENV} as well")
    if name is not None and name in auth.parse_users(inline, commas=True).records:
        message += f": {name} is still listed in it"
    print(message + ".", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the accounts that can log in to Dewey.")
    parser.add_argument("--file", type=Path, default=None,
                        help="users file (default: DEWEY_USERS_FILE from the environment or .env, else users.txt in the repo root)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    p = sub.add_parser("add", help="add a user, or change an existing user's password")
    p.add_argument("name")
    p = sub.add_parser("remove", help="remove a user")
    p.add_argument("name")
    sub.add_parser("list", help="list user names")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    path = args.file if args.file is not None else default_users_file()
    try:
        if args.command == "add":
            add_user(path, args.name)
        elif args.command == "remove":
            remove_user(path, args.name)
        else:
            list_users(path)
    except UsageError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled; nothing changed.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

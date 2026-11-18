"""
Password login for Dewey's web UI. Standard library only, so
`app/manage_users.py` runs on a server before (or without) installing
Dewey's dependencies.

Accounts are read once, when Dewey starts, from one of:

  DEWEY_USERS       an environment variable holding the accounts, separated by
                    commas or newlines. Use it on platforms where secrets reach
                    the app as environment variables (Fly.io, Render).
  DEWEY_USERS_FILE  a users file, one account per line. Default: users.txt in
                    the repository root. Used when DEWEY_USERS is unset.

Create and edit accounts with `python app/manage_users.py`. An account is

    alice:scrypt:32768:8:1:<salt>:<key>

where <salt> and <key> are URL-safe base64 without padding, and 32768:8:1 are
the scrypt cost parameters used to make <key>. Only this hash is stored, never
the password. The format has no "$", quotes, commas or spaces, so a line can be
pasted into a shell or a platform's secret store unchanged. Blank lines and
lines starting with "#" are ignored. (`pbkdf2_sha256:<iterations>:<salt>:<key>`
is also accepted, for Pythons built without scrypt.)

Messages from this module never include a password, a hash, or the content of
a malformed line: only line numbers and user names.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_USERS_FILE = REPO_ROOT / "users.txt"
USERS_ENV = "DEWEY_USERS"
USERS_FILE_ENV = "DEWEY_USERS_FILE"

USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")
USERNAME_RULE = "1-64 characters: letters, digits, '.', '_', '@' or '-', starting with a letter or digit"
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024  # longer input is rejected without hashing it

# scrypt cost for new hashes: 32 MiB of memory and roughly 0.1 s of CPU per
# login check. Stored with each hash, so these can be raised later without
# invalidating existing accounts.
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16
KEY_BYTES = 32

# Upper bounds when reading a hash, so a hand-edited file can't make each
# login attempt allocate gigabytes or spin for minutes.
MAX_SCRYPT_MEMORY = 256 * 1024 * 1024
MAX_PBKDF2_ITERATIONS = 10_000_000

# Gradio runs each POST /login in a thread pool (40 threads by default) with no
# rate limit, and each scrypt check above allocates 32 MiB. Without a cap, 40
# parallel attempts from someone without an account take about 1.2 GB. So at
# most this many checks run at once; an attempt that can't start within
# HASH_WAIT_SECONDS is rejected as a wrong password.
MAX_PARALLEL_HASHES = 2
HASH_WAIT_SECONDS = 5.0
_hash_slots = threading.BoundedSemaphore(MAX_PARALLEL_HASHES)
_busy_warned_at: Optional[float] = None

_INT_RE = re.compile(r"[0-9]{1,9}")
_B64_RE = re.compile(r"[A-Za-z0-9_-]+")


class UsersConfigError(Exception):
    """The accounts are configured but can't be used. Dewey must not start."""


def _warn(message: str) -> None:
    print(f"Dewey: warning: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    if not _B64_RE.fullmatch(text):
        raise ValueError("salt or key is not URL-safe base64")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("salt or key is not URL-safe base64") from None


def _parse_int(text: str) -> int:
    if not _INT_RE.fullmatch(text):
        raise ValueError("cost parameters must be whole numbers")
    return int(text)


@dataclass(frozen=True)
class PasswordHash:
    algorithm: str  # "scrypt" or "pbkdf2_sha256"
    params: Tuple[int, ...]  # (n, r, p) for scrypt, (iterations,) for pbkdf2_sha256
    salt: bytes
    key: bytes

    def encode(self) -> str:
        return ":".join([self.algorithm, *map(str, self.params), _b64encode(self.salt), _b64encode(self.key)])

    @classmethod
    def decode(cls, text: str) -> "PasswordHash":
        """Parse an encoded hash. Error messages never repeat the input."""
        parts = text.split(":")
        algorithm = parts[0]
        if algorithm == "scrypt":
            if len(parts) != 6:
                raise ValueError("an scrypt hash has 6 fields: scrypt:N:r:p:salt:key")
            if not hasattr(hashlib, "scrypt"):
                raise ValueError("this Python's hashlib has no scrypt (it needs OpenSSL 1.1 or later)")
            n, r, p = (_parse_int(x) for x in parts[1:4])
            if n < 2 or n & (n - 1) or not 1 <= r <= 64 or not 1 <= p <= 16 or 128 * n * r * p > MAX_SCRYPT_MEMORY:
                raise ValueError("scrypt cost parameters are out of range")
            params: Tuple[int, ...] = (n, r, p)
            salt_text, key_text = parts[4], parts[5]
        elif algorithm == "pbkdf2_sha256":
            if len(parts) != 4:
                raise ValueError("a pbkdf2_sha256 hash has 4 fields: pbkdf2_sha256:iterations:salt:key")
            iterations = _parse_int(parts[1])
            if not 10_000 <= iterations <= MAX_PBKDF2_ITERATIONS:
                raise ValueError("pbkdf2_sha256 iteration count is out of range")
            params = (iterations,)
            salt_text, key_text = parts[2], parts[3]
        else:
            raise ValueError("unknown hash algorithm (expected scrypt or pbkdf2_sha256)")
        salt, key = _b64decode(salt_text), _b64decode(key_text)
        if len(salt) < 8 or not 16 <= len(key) <= 64:
            raise ValueError("salt or key has the wrong length")
        return cls(algorithm, params, salt, key)


def _derive(password: str, algorithm: str, params: Tuple[int, ...], salt: bytes, dklen: int) -> bytes:
    data = password.encode("utf-8")
    if algorithm == "scrypt":
        n, r, p = params
        # OpenSSL's default memory cap (32 MiB) is too small for n=2**15, r=8.
        maxmem = 128 * r * (n + p + 2) + 1024 * 1024
        return hashlib.scrypt(data, salt=salt, n=n, r=r, p=p, maxmem=maxmem, dklen=dklen)
    if algorithm == "pbkdf2_sha256":
        return hashlib.pbkdf2_hmac("sha256", data, salt, params[0], dklen=dklen)
    raise ValueError("unknown hash algorithm")


def _new_hash_settings() -> Tuple[str, Tuple[int, ...]]:
    if hasattr(hashlib, "scrypt"):
        return "scrypt", (SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return "pbkdf2_sha256", (PBKDF2_ITERATIONS,)  # pragma: no cover - Pythons with very old OpenSSL


def hash_password(password: str) -> str:
    """Hash a new password with a fresh random salt. Returns the encoded hash."""
    algorithm, params = _new_hash_settings()
    salt = secrets.token_bytes(SALT_BYTES)
    key = _derive(password, algorithm, params, salt, KEY_BYTES)
    return PasswordHash(algorithm, params, salt, key).encode()


def _matches(password: str, stored: PasswordHash) -> bool:
    derived = _derive(password, stored.algorithm, stored.params, stored.salt, len(stored.key))
    return hmac.compare_digest(derived, stored.key)  # constant-time comparison


_dummy: Optional[PasswordHash] = None


def _dummy_hash() -> PasswordHash:
    """A hash no password matches, checked for unknown users so a failed login
    takes as long whether or not the user name exists."""
    global _dummy
    if _dummy is None:
        algorithm, params = _new_hash_settings()
        _dummy = PasswordHash(algorithm, params, secrets.token_bytes(SALT_BYTES), secrets.token_bytes(KEY_BYTES))
    return _dummy


def verify_password(password: str, encoded: str) -> bool:
    try:
        stored = PasswordHash.decode(encoded)
    except ValueError:
        return False
    return _matches(password, stored)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def valid_username(name: str) -> bool:
    return bool(USERNAME_RE.fullmatch(name))


@dataclass
class ParsedUsers:
    records: Dict[str, PasswordHash] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)  # never contain line content
    entries: int = 0  # non-blank, non-comment entries seen, valid or not


def _entries(text: str, commas: bool) -> List[Tuple[str, str]]:
    """(location, entry) pairs, skipping blank and "#" comment entries.

    In a file each line is one entry. With commas=True (the DEWEY_USERS
    variable) commas separate entries too, so all accounts fit on one line.
    """
    found = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        pieces = [p.strip() for p in (line.split(",") if commas else [line])]
        pieces = [p for p in pieces if p and not p.startswith("#")]
        for i, piece in enumerate(pieces, start=1):
            found.append((f"line {lineno}" if len(pieces) == 1 else f"line {lineno}, entry {i}", piece))
    return found


def parse_users(text: str, commas: bool = False) -> ParsedUsers:
    result = ParsedUsers()
    for location, entry in _entries(text, commas):
        result.entries += 1
        name, sep, encoded = entry.partition(":")
        if not sep:
            result.problems.append(f"{location}: not in name:hash form; skipped")
            continue
        if not valid_username(name):
            result.problems.append(f"{location}: user name must be {USERNAME_RULE}; skipped")
            continue
        try:
            stored = PasswordHash.decode(encoded)
        except ValueError as e:
            result.problems.append(f"{location}: password hash for {name!r} is not valid ({e}); skipped")
            continue
        if name in result.records:
            result.problems.append(f"{location}: {name!r} is listed more than once; using the first entry")
            continue
        result.records[name] = stored
    return result


class UserStore:
    """Accounts loaded at startup. Pass `verify` to Gradio as launch(auth=...)."""

    def __init__(self, records: Mapping[str, PasswordHash], source: str):
        self._records = dict(records)
        self.source = source

    def __len__(self) -> int:
        return len(self._records)

    def usernames(self) -> List[str]:
        return sorted(self._records)

    def verify(self, username: str, password: str) -> bool:
        """Gradio login check. Always derives one hash and makes one constant-time
        comparison, so unknown users and wrong passwords take the same time."""
        record = self._records.get(username) if isinstance(username, str) else None
        usable = isinstance(password, str) and len(password) <= MAX_PASSWORD_LENGTH
        target = record if (record is not None and usable) else _dummy_hash()
        if not _hash_slots.acquire(timeout=HASH_WAIT_SECONDS):
            _warn_busy()
            return False
        try:
            matches = _matches(password if usable else "", target)
        except (ValueError, MemoryError):
            print("Dewey: a login check failed while hashing; treating it as a wrong password.", file=sys.stderr)
            return False
        finally:
            _hash_slots.release()
        return record is not None and usable and matches


def _warn_busy() -> None:
    """Say (at most once a minute) that login attempts are being turned away."""
    global _busy_warned_at
    now = time.monotonic()
    if _busy_warned_at is None or now - _busy_warned_at >= 60:
        _busy_warned_at = now
        print("Dewey: warning: too many login attempts at once; rejecting some. If nobody is logging in, "
              "someone may be guessing passwords: rate-limit POST /login at your reverse proxy.", file=sys.stderr)


def read_users_text(path: Path) -> str:
    """Read a users file, turning every failure into a UsersConfigError that says what to do."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise UsersConfigError(
            f"Users file {path} does not exist ({USERS_FILE_ENV}). "
            "Create it with: python app/manage_users.py add NAME"
        ) from None
    except IsADirectoryError:
        raise UsersConfigError(
            f"{path} is a directory, not a users file. Docker creates a directory when a bind-mounted "
            "file doesn't exist on the host: remove it, create users.txt with app/manage_users.py, and restart."
        ) from None
    except PermissionError:
        raise UsersConfigError(
            f"Can't read users file {path}: permission denied. The Docker image runs as user id 1000; "
            "make the file readable by that user (for example: chmod 644 users.txt)."
        ) from None
    except OSError as e:
        raise UsersConfigError(f"Can't read users file {path}: {e.strerror or e}") from None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UsersConfigError(f"Users file {path} is not UTF-8 text.") from None


def users_file_path(env: Mapping[str, str], repo_root: Path = REPO_ROOT) -> Tuple[Path, bool]:
    """(path, explicitly configured?)"""
    setting = env.get(USERS_FILE_ENV, "").strip()
    if setting:
        return Path(setting).expanduser(), True
    return repo_root / "users.txt", False


def load_user_store(
    env: Mapping[str, str],
    repo_root: Path = REPO_ROOT,
    warn: Callable[[str], None] = _warn,
) -> Optional[UserStore]:
    """Load accounts from DEWEY_USERS or the users file.

    Returns None when no accounts are configured (no DEWEY_USERS, no
    DEWEY_USERS_FILE, and no users.txt in the repo root). Returns a possibly
    empty UserStore when a source exists. Raises UsersConfigError when accounts
    are configured but unusable (missing explicit file, unreadable file, or a
    source whose every entry is malformed), so Dewey never silently runs
    without the login someone set up.
    """
    inline = env.get(USERS_ENV, "")
    path, explicit = users_file_path(env, repo_root)

    if inline.strip():
        if explicit:
            raise UsersConfigError(f"Set {USERS_ENV} or {USERS_FILE_ENV}, not both.")
        source = f"the {USERS_ENV} environment variable"
        parsed = parse_users(inline, commas=True)
        if path.is_file():
            warn(f"{path} is ignored because {USERS_ENV} is set.")
    else:
        if not explicit:
            try:
                path.stat()
            except FileNotFoundError:
                return None
            except OSError:
                pass  # read_users_text reports it
        source = f"users file {path}"
        parsed = parse_users(read_users_text(path))

    for problem in parsed.problems:
        warn(f"{source}, {problem}")
    if parsed.entries and not parsed.records:
        raise UsersConfigError(
            f"{source} lists {parsed.entries} account(s) but none of them is valid (see the warnings above). "
            "Re-create them with: python app/manage_users.py add NAME"
        )
    return UserStore(parsed.records, source)


# ---------------------------------------------------------------------------
# Writing the users file (used by manage_users.py)
# ---------------------------------------------------------------------------

def write_text_atomic(path: Path, text: str) -> None:
    """Replace `path` with `text` without leaving a half-written file.

    A new file is created readable by its owner only (0600). An existing
    file keeps its permission bits and, where allowed, its owner, so a file
    made readable for a container (chmod 644) stays readable.
    """
    path = Path(os.path.realpath(path))
    try:
        st: Optional[os.stat_result] = path.stat()
    except FileNotFoundError:
        st = None
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".users-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, (st.st_mode & 0o777) if st else 0o600)
        if st is not None and hasattr(os, "chown"):
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except PermissionError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

"""
Which Claude credential Dewey's client will use, worked out without the Anthropic SDK.

Dewey builds its Claude client with no arguments (anthropic_llm.py), so the SDK picks the
credential itself. This module follows the same steps as the pinned SDK (anthropic 2.10.0:
lib/credentials/_chain.py, _constants.py and _providers.py) and sorts the result into three states:

    usable       the client will have a credential to send
    missing      the client stops while it is being built, or is left with no credential at all.
                 Dewey refuses to start.
    unconfirmed  the client is built, but its first request to Claude will fail: the sign-in
                 looks signed out, or is a kind this SDK does not know. Dewey warns and starts,
                 because searches never call Claude. The verdict comes from looking at files, so
                 it must never stop Dewey or block indexing.

Two programs use this one file, so they can't disagree. app/providers/__init__.py imports it.
install.py, which runs before Dewey's packages are installed, loads it by its path. So this file
must stay standard library only, with no relative imports and nothing that needs the `providers`
package to be imported, and it must never print (in the MCP server's stdio mode, stdout is the
JSON-RPC channel). Keep the syntax plain too: no newer than Python 3.9.

What it looks at: the variables it is given, the active_config file and one settings file
(configs/NAME.json). It checks that the credentials file (credentials/NAME.json) exists and never
opens it. It does not check file permissions, follow the SDK's symlink rules or ask Anthropic
whether a sign-in still works: build_stack() turns the SDK's own refusals into a plain message.

tests/install/test_credential_agreement.py runs every scenario in tests/claude_profiles.py through
this file and through the real SDK. Run it when the anthropic pin in requirements.txt changes.
"""
import json
import os
import re
import shlex
import sys
from typing import List, Mapping, NamedTuple, Optional, Tuple

USABLE, MISSING, UNCONFIRMED = "usable", "missing", "unconfirmed"

FEDERATION = "workload identity federation"
DEFAULT_PROFILE = "default"
NEEDS = "DEWEY_STACK=anthropic needs a Claude credential."


class ClaudeCredential(NamedTuple):
    """What anthropic_credential_status() found.

    state        usable, missing or unconfirmed (see the top of this file)
    source       ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, "workload identity federation" or
                 "profile" (a sign-in saved by `ant auth login`); "" when missing
    reason       why the state is not usable:
                   no_credential             nothing is set and no sign-in is saved
                   profile_not_found         the sign-in that was selected has no settings file
                   profile_name_invalid      its name is one the SDK refuses, or active_config is not text
                   profile_unreadable        its settings file can't be read as a sign-in
                   signed_out                its credentials file is missing (as after `ant auth logout`)
                   credentials_path_missing  the same, for a sign-in that keeps that file somewhere of its own
                   profile_type_unknown      its settings name a kind of sign-in this SDK does not know
    profile      the sign-in's name, when one was looked at
    selected_by  how it was chosen: ANTHROPIC_PROFILE, active_config or default
    path         the file that was looked for (for no_credential, the config folder)
    active       the active sign-in's name (from active_config, else "default"), when a sign-in
                 was looked at; "" if active_config could not be read as text
    """
    state: str
    source: str = ""
    reason: str = ""
    profile: str = ""
    selected_by: str = ""
    path: str = ""
    active: str = ""


# ---------------------------------------------------------------- the verdict

def _home(env: Mapping[str, str]) -> str:
    """The home folder, from these variables where Python's own lookup (pathlib.Path.home) reads them."""
    if os.name == "nt":
        return env.get("USERPROFILE") or os.path.expanduser("~")
    if "HOME" in env:
        return (env["HOME"] or "").rstrip("/") or "/"
    return os.path.expanduser("~")


def _expand_user(path: str, env: Mapping[str, str]) -> str:
    """os.path.expanduser, with a leading ~ taken from the same variables as everything else here."""
    if path[:1] == "~" and path[1:2] in ("", "/", os.sep):
        return _home(env) + path[1:]
    return os.path.expanduser(path)


def anthropic_config_dir(env: Optional[Mapping[str, str]] = None) -> str:
    """The folder that holds the saved sign-ins: $ANTHROPIC_CONFIG_DIR (used exactly as given: the SDK
    neither trims it nor expands ~), else ~/.config/anthropic, or on Windows %APPDATA%\\Anthropic."""
    if env is None:
        env = os.environ
    explicit = env.get("ANTHROPIC_CONFIG_DIR") or ""
    if explicit:
        return explicit
    if sys.platform == "win32":
        base = env.get("APPDATA") or os.path.join(_home(env), "AppData", "Roaming")
        return os.path.join(base, "Anthropic")
    return os.path.join(_home(env), ".config", "anthropic")


def _name_is_valid(name: str) -> bool:
    """The SDK's rule for a sign-in's name, which becomes a file name (_validate_profile_name)."""
    if not name or name != name.strip() or name.startswith("."):
        return False
    return not any(bad and bad in name for bad in ("/", "\\", os.sep, "\x00"))


def _active_name(config_dir: str) -> Tuple[str, bool]:
    """(name, readable): what the active_config file says. No file, or an empty one, is ("", True),
    which means "default". A file that is not UTF-8 text is ("", False): the SDK's constructor
    lets UnicodeDecodeError out for it."""
    try:
        with open(os.path.join(config_dir, "active_config"), encoding="utf-8") as handle:
            return handle.read().strip(), True
    except OSError:
        return "", True
    except ValueError:
        return "", False


def _judge_profile(env: Mapping[str, str], config_dir: str, name: str, selected_by: str,
                   active: str) -> ClaudeCredential:
    """One sign-in, as CredentialsFile in the SDK would load it."""

    def found(state: str, reason: str = "", path: str = "") -> ClaudeCredential:
        return ClaudeCredential(state, "" if state == MISSING else "profile", reason, name, selected_by, path, active)

    if not _name_is_valid(name):
        return found(MISSING, "profile_name_invalid",
                     os.path.join(config_dir, "active_config") if selected_by == "active_config" else "")
    config_path = os.path.join(config_dir, "configs", name + ".json")
    try:
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
    except FileNotFoundError:
        return found(MISSING, "profile_not_found", config_path)
    except (OSError, ValueError, RecursionError):  # ValueError: not UTF-8, or not JSON
        return found(MISSING, "profile_unreadable", config_path)
    authentication = config.get("authentication") if isinstance(config, dict) else None
    if not isinstance(authentication, dict):
        return found(MISSING, "profile_unreadable", config_path)

    kind = authentication.get("type")
    if kind == "oidc_federation":
        return found(USABLE, path=config_path)  # it exchanges a token when asked: no credentials file needed
    if kind != "user_oauth":
        return found(UNCONFIRMED, "profile_type_unknown", config_path)
    elsewhere = authentication.get("credentials_path")
    if elsewhere:
        credentials_path = _expand_user(str(elsewhere), env)
        if not os.path.isfile(credentials_path):
            return found(UNCONFIRMED, "credentials_path_missing", credentials_path)
    else:
        credentials_path = os.path.join(config_dir, "credentials", name + ".json")
        if not os.path.isfile(credentials_path):
            return found(UNCONFIRMED, "signed_out", credentials_path)
    return found(USABLE, path=config_path)


def anthropic_credential_status(env: Optional[Mapping[str, str]] = None) -> ClaudeCredential:
    """The Claude credential a client built from these variables would use. Never raises.

    `env` is the whole environment to judge (default: this process's). The wizard passes .env's
    lines together with the terminal's variables, which is what Dewey will see once it has loaded .env.

    The order is the SDK's, and the first match wins:
      1. ANTHROPIC_API_KEY, then ANTHROPIC_AUTH_TOKEN.
      2. ANTHROPIC_PROFILE: that sign-in is used, or the client stops. It never tries the next step.
      3. The Workload Identity Federation variables.
      4. The active sign-in: the one active_config names, else "default". A named one that is
         missing stops the client too.
    A blank key or token (empty, or only spaces) is passed over here. The SDK would skip an empty one
    and send one made only of spaces; Dewey refuses both before it gets this far (build_stack).
    Everywhere else an empty value means "not set" and anything else counts, spaces included, as in the SDK.
    """
    if env is None:
        env = os.environ

    def value(name: str) -> str:
        return env.get(name) or ""

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if value(name).strip():
            return ClaudeCredential(USABLE, source=name)

    config_dir = anthropic_config_dir(env)
    named = value("ANTHROPIC_PROFILE")
    if named:
        active, readable = _active_name(config_dir)
        return _judge_profile(env, config_dir, named, "ANTHROPIC_PROFILE",
                              (active or DEFAULT_PROFILE) if readable else "")

    if (value("ANTHROPIC_FEDERATION_RULE_ID") and value("ANTHROPIC_ORGANIZATION_ID")
            and (value("ANTHROPIC_IDENTITY_TOKEN") or value("ANTHROPIC_IDENTITY_TOKEN_FILE"))):
        return ClaudeCredential(USABLE, source=FEDERATION)

    active, readable = _active_name(config_dir)
    if not readable:
        return ClaudeCredential(MISSING, reason="profile_name_invalid", selected_by="active_config",
                                path=os.path.join(config_dir, "active_config"))
    if active:
        return _judge_profile(env, config_dir, active, "active_config", active)
    default = _judge_profile(env, config_dir, DEFAULT_PROFILE, "default", DEFAULT_PROFILE)
    if default.reason == "profile_not_found":
        return ClaudeCredential(MISSING, reason="no_credential", path=config_dir)
    return default


def blank_credential_variables(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN, when set but blank (empty, or only spaces)."""
    if env is None:
        env = os.environ
    return [name for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
            if name in env and not (env[name] or "").strip()]


# ---------------------------------------------------------------- what to tell people
#
# Plain lines for newsroom staff. A command stands on a line of its own, four spaces in, after
# the line that explains it, and a sign-in's name is quoted for the shell (names can hold spaces).
#
# Facts about Anthropic's `ant` tool that the wording rests on, checked in November 2026.
# Run with ant 2.12.1, on throwaway folders (only commands that change nothing):
# - `ant auth status` reports what ant finds in the terminal it runs in, and takes --profile NAME.
#   It never sees Dewey's .env, and its choice can differ from the Python SDK's: with
#   ANTHROPIC_PROFILE naming a missing sign-in and federation variables set, it shows federation
#   as active, while the SDK stops. For a signed-out sign-in it prints `(profile "NAME" configured
#   but not logged in ...)`; for a damaged settings file, `(profile config unreadable: ...)`.
# - `ant profile list` and `ant profile activate NAME` exist.
# Read, not run (signing in and out would change this computer's sign-ins):
# - https://platform.claude.com/docs/en/cli-sdks-libraries/cli/authentication : a login that names a
#   profile creates it if needed and makes it the active profile; `ant auth logout` removes the
#   stored credentials and the profile keeps its settings.
# - https://platform.claude.com/docs/en/manage-claude/wif-reference#active-profile :
#   `ant profile activate NAME` writes the active_config file.
# - ant's source at tag v2.12.1, pkg/cmd/cmd_auth.go: login writes configs/NAME.json only when it
#   doesn't exist yet ("Re-login never rewrites the profile config"), so it can't repair a damaged
#   one, and it always writes credentials/NAME.json, whatever "credentials_path" says.

_NAME_RULE = "A name can't begin or end with a space, begin with a dot, or contain a slash or backslash."


def _printable(text: str) -> str:
    """Text that can always be printed. A variable can hold bytes that aren't UTF-8 (Python keeps
    them as lone surrogates), and printing one must not turn a message into a crash."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def quoted_name(name: str) -> str:
    """A sign-in's name between double quotes for a message, with control characters written out."""
    return _printable(json.dumps(name, ensure_ascii=False))


def ant_status_lines(credential: Optional[ClaudeCredential] = None,
                     to: str = "see what the ant tool finds in this terminal") -> List[str]:
    """The `ant auth status` command and the line before it. When ANTHROPIC_PROFILE chose the
    sign-in, the command names it: the variable may be set in .env only, which ant doesn't read."""
    command = "ant auth status"
    if credential is not None and credential.selected_by == "ANTHROPIC_PROFILE" and _name_is_valid(credential.profile):
        command += " --profile " + shlex.quote(credential.profile)
    return [f"To {to} (it does not read Dewey's .env), run:", "    " + command]


def _switch_back_lines(credential: ClaudeCredential) -> List[str]:
    """What to add under `ant auth login --profile NAME` when NAME is not the active sign-in."""
    if credential.selected_by != "ANTHROPIC_PROFILE" or credential.profile == credential.active:
        return []
    instead = f", in place of {quoted_name(credential.active)}" if credential.active else ""
    return [f"That also makes it the active sign-in{instead}. To switch back, run this with the old name in place "
            "of NAME:",
            "    ant profile activate NAME"]


def _problem_lines(credential: ClaudeCredential) -> List[str]:
    reason, path = credential.reason, credential.path
    name = quoted_name(credential.profile)
    login = f"    ant auth login --profile {shlex.quote(credential.profile)}"
    by_variable = credential.selected_by == "ANTHROPIC_PROFILE"

    if reason == "profile_not_found" and by_variable:
        return [
            f"ANTHROPIC_PROFILE is set to {name}, but no Claude sign-in with that name is saved on this computer.",
            f"Dewey looked for {path}",
            "Dewey's Claude client stops when ANTHROPIC_PROFILE names a missing sign-in: it does not try another "
            "credential.",
            "To list the saved sign-ins, run:",
            "    ant profile list",
            "To create this one, run:",
            login,
            *_switch_back_lines(credential),
            "Or remove ANTHROPIC_PROFILE from .env and from your terminal.",
        ]
    if reason == "profile_not_found":
        return [
            f"The active Claude sign-in is {name}, but no sign-in with that name is saved on this computer.",
            f"Dewey looked for {path}",
            "Dewey's Claude client stops when the active sign-in is missing: it does not fall back to another "
            "saved sign-in.",
            "To list the saved sign-ins, run:",
            "    ant profile list",
            "To make a saved one active, run this with its name in place of NAME:",
            "    ant profile activate NAME",
            "Or sign in under this name:",
            login,
        ]
    if reason == "profile_name_invalid" and by_variable:
        return [
            f"The Claude sign-in name {name} is not one Dewey's Claude client accepts.",
            "It comes from ANTHROPIC_PROFILE.",
            _NAME_RULE,
            "Correct ANTHROPIC_PROFILE in .env or your terminal, or remove it.",
        ]
    if reason == "profile_name_invalid" and not credential.profile:
        return [
            "The file that names the active Claude sign-in can't be read as text.",
            f"The file is {path}",
            "To write it again, run this with a saved sign-in's name in place of NAME:",
            "    ant profile activate NAME",
            "To list the saved sign-ins, run:",
            "    ant profile list",
        ]
    if reason == "profile_name_invalid":
        return [
            f"The Claude sign-in name {name} is not one Dewey's Claude client accepts.",
            f"It comes from {path}",
            _NAME_RULE,
            "To make a saved sign-in active, run this with its name in place of NAME:",
            "    ant profile activate NAME",
        ]
    if reason == "profile_unreadable":
        return [
            f"The Claude sign-in {name} can't be used: its settings file is damaged, or is not a sign-in file.",
            f"The file is {path}",
            *ant_status_lines(credential, to="see what the ant tool finds wrong with it"),
            "Signing in again does not rewrite a settings file that already exists. Rename or delete that file, "
            "then sign in again:",
            login,
            *_switch_back_lines(credential),
        ]
    if reason == "signed_out":
        return [
            f"The Claude sign-in {name} looks signed out: its saved credentials are missing, as after ant auth logout.",
            f"Dewey looked for {path}",
            *ant_status_lines(credential),
            'A signed-out sign-in shows there as "configured but not logged in" (ant 2.12).',
            "To sign in again, run:",
            login,
            *_switch_back_lines(credential),
        ]
    if reason == "credentials_path_missing":
        return [
            f"The Claude sign-in {name} keeps its credentials in a file of its own, and that file is missing.",
            f"Dewey looked for {path}",
            'The sign-in\'s settings name that location ("credentials_path"). Put the file back there, or have the '
            "program that writes it write it again.",
            *ant_status_lines(credential),
        ]
    if reason == "profile_type_unknown":
        return [
            f"The Claude sign-in {name} is a kind that Dewey's Claude client does not know, so its questions to "
            "Claude would fail.",
            f"Its settings file is {path}",
            "Dewey's Claude client knows two kinds: a browser sign-in saved by the ant tool, and Workload Identity "
            "Federation.",
            *ant_status_lines(credential),
        ]
    return []


def credential_problem_lines(credential: ClaudeCredential) -> List[str]:
    """What is wrong with the sign-in that was selected, and what to do about it.

    The wizard and Dewey's startup both print these lines. Empty for a usable credential, and when
    there is simply none (reason no_credential), which each program words itself.
    """
    return [_printable(line) for line in _problem_lines(credential)]


def blank_credential_message(names: List[str], where: str = "") -> str:
    """Why Dewey won't start with a blank ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN. `where` says
    where it is set (" in .env", " in this terminal") when the caller knows."""
    verb = "is" if len(names) == 1 else "are"
    return (f"{' and '.join(names)} {verb} set but empty{where}. Dewey won't start with a blank credential: its "
            "Claude client skips an empty value and would send one made only of spaces as the key. "
            "Remove the blank setting, or give it a real value.")


# ---------------------------------------------------------------- Dewey's startup messages

def mask_url_credentials(text: str) -> str:
    """Hide the user:password part of every URL in a message (the SDK's https error quotes ANTHROPIC_BASE_URL)."""
    return re.sub(r"(?<=://)[^/\s]*@", "***@", text)


def startup_error(credential: ClaudeCredential) -> str:
    """Why Dewey can't start with a `missing` credential: the text of the StackConfigError."""
    lines = credential_problem_lines(credential)
    if lines:
        return "\n".join([f"{NEEDS} The saved sign-in it would use can't be used.", *lines,
                          "Or set ANTHROPIC_API_KEY in .env."])
    return _printable(
        f"{NEEDS} Set ANTHROPIC_API_KEY, or run `ant auth login` to save a sign-in, or set "
        "ANTHROPIC_FEDERATION_RULE_ID, ANTHROPIC_ORGANIZATION_ID and ANTHROPIC_IDENTITY_TOKEN (or "
        "ANTHROPIC_IDENTITY_TOKEN_FILE) for Workload Identity Federation. Dewey found none of these; it looked "
        f"for a saved sign-in in {credential.path}. Leave ANTHROPIC_API_KEY out rather than blank.")


def startup_warning(credential: ClaudeCredential) -> str:
    """What Dewey prints on stderr when it starts with an `unconfirmed` credential."""
    # The SDK reads the credentials file again on every request, but the settings file only once.
    after = "then restart Dewey" if credential.reason == "profile_type_unknown" else "no restart is needed afterwards"
    return "\n".join([
        "Dewey: warning: could not confirm a Claude credential.",
        *("  " + line for line in credential_problem_lines(credential)),
        "  Dewey is starting anyway. Searches (search_archive) work. Written answers (ask_archive, the demo page) "
        f"will fail until this is fixed; {after}.",
    ])


def sdk_refusal_message(error: BaseException, env: Optional[Mapping[str, str]] = None) -> str:
    """The SDK refused to build a client for a reason the check above doesn't look for (file
    permissions, a symlink, a base URL that isn't https...). Its own words say which, so they are
    repeated, with any password in a URL masked."""
    lines = [f"{NEEDS} Dewey's Claude client (Anthropic's library) could not load the one it found. "
             f"It said: {mask_url_credentials(str(error))}"]
    if isinstance(error, UnicodeDecodeError):
        # The one file the SDK reads without catching this: active_config, in the default location.
        pointer = os.path.join(anthropic_config_dir(env), "active_config")
        lines += [f"That usually means the file naming the active sign-in is damaged: {pointer}",
                  "To write it again, run this with a saved sign-in's name in place of NAME:",
                  "    ant profile activate NAME"]
    return _printable("\n".join(lines + ant_status_lines()))


def no_credential_in_client_message() -> str:
    """The SDK built a client that holds no credential at all: in the default location, with no
    active_config, it skips a sign-in it can't load and says nothing."""
    return "\n".join([
        f"{NEEDS} Dewey's Claude client found none it could use: it skips a saved sign-in whose files it can't "
        "read or doesn't trust, without saying why. Set ANTHROPIC_API_KEY in .env, or look at the saved sign-in.",
        *ant_status_lines(),
    ])

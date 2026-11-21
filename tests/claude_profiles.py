"""
Throwaway `ant` sign-in folders, and the table of Claude credential scenarios that the tests in
tests/install, tests/providers, tests/deploy and tests/mcp share.

A sign-in ("profile") is two files under Anthropic's config folder (~/.config/anthropic, or
$ANTHROPIC_CONFIG_DIR): configs/NAME.json, which is not secret, and credentials/NAME.json, which
is. `ant auth logout` removes the second and keeps the first. The files written here have the
shape the pinned SDK reads (anthropic 2.10.0, lib/credentials/_providers.py) and the file modes it
insists on, whatever the umask: a settings file that group or others can write, or a credentials
file they can read, is refused.

Standard library only, because the installer's tests run before requirements.txt is installed.
The name doesn't start with "test", so unittest doesn't collect it.

Safety: every folder is made under a new temp folder that is removed with the test, and every
environment built here sets HOME to it (and ANTHROPIC_CONFIG_DIR, except in the "default
location" rows), so no test looks at the developer's own sign-in.
"""
import json
import os
import pathlib
import shutil
import socket
import tempfile
import time
from contextlib import contextmanager
from typing import NamedTuple
from unittest import mock

ORGANIZATION_ID = "00000000-0000-0000-0000-000000000000"
API_KEY = "sk-ant-test-key"
AUTH_TOKEN = "sk-ant-oat01-test-token"
FEDERATION_ENV = {
    "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_01",
    "ANTHROPIC_ORGANIZATION_ID": ORGANIZATION_ID,
    "ANTHROPIC_IDENTITY_TOKEN_FILE": "/var/run/secrets/anthropic.com/token",
}

# What write_profile() puts on disk for a name.
SIGNED_IN = "signed in"      # settings and credentials, as after `ant auth login`
SIGNED_OUT = "signed out"    # settings only, as after `ant auth logout`
FEDERATION = "federation"    # a Workload Identity Federation sign-in: it needs no credentials file


def login_config(**authentication):
    """configs/NAME.json for a sign-in made with `ant auth login`."""
    return {"version": "1.0",
            "authentication": {"type": "user_oauth", "client_id": "test-client", **authentication},
            "organization_id": ORGANIZATION_ID}


def federation_config():
    return {"version": "1.0",
            "authentication": {"type": "oidc_federation", "federation_rule_id": "fdrl_01",
                               "identity_token": {"source": "file",
                                                  "path": FEDERATION_ENV["ANTHROPIC_IDENTITY_TOKEN_FILE"]}},
            "organization_id": ORGANIZATION_ID}


def saved_credentials():
    """credentials/NAME.json: a token that has an hour left, so the SDK returns it without a network call."""
    return {"version": "1.0", "type": "oauth_token", "access_token": "test-access-token",
            "refresh_token": "test-refresh-token", "expires_at": int(time.time()) + 3600}


def temp_dir(testcase, prefix="dewey-claude-"):
    path = pathlib.Path(tempfile.mkdtemp(prefix=prefix))
    testcase.addCleanup(shutil.rmtree, path, True)
    return path


def _write(path, content, mode):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(content, (bytes, str)):
        content = json.dumps(content)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    os.chmod(path, mode)  # explicit: a umask of 002 would otherwise make the SDK refuse the file


def write_credentials(config_dir, name="default", path=None):
    """Sign a profile in: write its credentials file (0600)."""
    _write(path or pathlib.Path(config_dir, "credentials", f"{name}.json"), saved_credentials(), 0o600)


def write_profile(config_dir, name, kind=SIGNED_IN):
    """Write one sign-in. kind: SIGNED_IN, SIGNED_OUT, FEDERATION, a dict (the settings file's
    content, with no credentials file) or text/bytes (the settings file's raw content)."""
    if kind in (SIGNED_IN, SIGNED_OUT):
        content = login_config()
    elif kind == FEDERATION:
        content = federation_config()
    else:
        content = kind
    _write(pathlib.Path(config_dir, "configs", f"{name}.json"), content, 0o644)
    if kind == SIGNED_IN:
        write_credentials(config_dir, name)


def make_config_dir(testcase, profiles=None, active=None, config_dir=None):
    """A config folder laid out the way the pinned SDK expects, removed with the test.

    profiles: {name: kind} (see write_profile). active: what the active_config file holds
    (text or bytes), or None for no such file. Returns the folder's path as a string.
    """
    config_dir = pathlib.Path(config_dir) if config_dir else temp_dir(testcase) / "config"
    (config_dir / "configs").mkdir(parents=True, exist_ok=True)
    for name, kind in (profiles or {}).items():
        write_profile(config_dir, name, kind)
    if active is not None:
        _write(config_dir / "active_config", active, 0o644)
    return str(config_dir)


@contextmanager
def no_network():
    """Fail the test if anything opens a connection. The SDK builds a client, and reads a saved
    token that has not expired, without the network; a test that got this wrong must not reach Anthropic."""
    def refuse(*args, **kwargs):
        raise AssertionError("this test must not use the network")

    with mock.patch.object(socket.socket, "connect", refuse), \
            mock.patch.object(socket.socket, "connect_ex", refuse):
        yield


# ---------------------------------------------------------------- the scenarios

FEDERATION_SOURCE = "workload identity federation"


def _usable(source, profile="", selected_by="", path="", active=""):
    return ("usable", source, "", profile, selected_by, path, active)


def _missing(reason, profile="", selected_by="", path="", active=""):
    return ("missing", "", reason, profile, selected_by, path, active)


def _unconfirmed(reason, profile, selected_by, path, active):
    return ("unconfirmed", "profile", reason, profile, selected_by, path, active)


def _usable_profile(name="default", selected_by="default", active=None):
    return _usable("profile", name, selected_by, "{dir}/configs/%s.json" % name, name if active is None else active)


class Scenario(NamedTuple):
    """One environment and sign-in folder, and what Dewey's check should say about it.

    expect is (state, source, reason, profile, selected_by, path, active), the fields of
    ClaudeCredential in app/providers/claude_credentials.py. In expect's path, in env values and
    in files' keys, {dir} is the config folder, {home} the home folder and {elsewhere} a third folder.
    """
    name: str
    expect: tuple
    profiles: dict = {}
    active: object = None       # what active_config holds: text, bytes, or None for no file
    env: dict = {}              # variables besides HOME and ANTHROPIC_CONFIG_DIR
    files: dict = {}            # more credentials files to write: {path: True}
    home: bool = False          # True: the folder is ~/.config/anthropic and ANTHROPIC_CONFIG_DIR is unset
    posix_only: bool = False
    sdk_stops_with: str = ""    # the SDK's constructor raises this instead of one of its own errors


def _without(name):
    return {k: v for k, v in FEDERATION_ENV.items() if k != name}


_NO_BLOCK = {"version": "1.0", "organization_id": ORGANIZATION_ID}   # a settings file with no "authentication"
_OTHER_KIND = {"version": "1.0", "authentication": {"type": "api_key"}, "organization_id": ORGANIZATION_ID}
_NO_KIND = {"version": "1.0", "authentication": {}, "organization_id": ORGANIZATION_ID}
_NOT_TEXT = b"\xff\xfe\n"

CASE_1 = "CASE 1 (issue #19): ANTHROPIC_PROFILE names a missing sign-in while federation variables are set"
CASE_2 = "CASE 2 (issue #19): active_config names a missing sign-in while a default one exists"
CASE_3 = "CASE 3 (issue #19): the sign-in was signed out with ant auth logout"

SCENARIOS = [
    # --- no files involved
    Scenario("nothing set and nothing saved", _missing("no_credential", path="{dir}")),
    Scenario("an API key", _usable("ANTHROPIC_API_KEY"), env={"ANTHROPIC_API_KEY": API_KEY}),
    Scenario("an auth token", _usable("ANTHROPIC_AUTH_TOKEN"), env={"ANTHROPIC_AUTH_TOKEN": AUTH_TOKEN}),
    Scenario("a key and a token: the key is used", _usable("ANTHROPIC_API_KEY"),
             env={"ANTHROPIC_API_KEY": API_KEY, "ANTHROPIC_AUTH_TOKEN": AUTH_TOKEN}),
    Scenario("federation variables", _usable(FEDERATION_SOURCE), env=FEDERATION_ENV),
    Scenario("federation with the identity token in a variable", _usable(FEDERATION_SOURCE),
             env={**_without("ANTHROPIC_IDENTITY_TOKEN_FILE"), "ANTHROPIC_IDENTITY_TOKEN": "eyJhbGciOi"}),
    Scenario("federation without the rule id", _missing("no_credential", path="{dir}"),
             env=_without("ANTHROPIC_FEDERATION_RULE_ID")),
    Scenario("federation without the organization id", _missing("no_credential", path="{dir}"),
             env=_without("ANTHROPIC_ORGANIZATION_ID")),
    Scenario("federation without an identity token", _missing("no_credential", path="{dir}"),
             env=_without("ANTHROPIC_IDENTITY_TOKEN_FILE")),
    Scenario("an empty federation variable is unset", _usable_profile(),
             profiles={"default": SIGNED_IN}, env={**FEDERATION_ENV, "ANTHROPIC_FEDERATION_RULE_ID": ""}),
    Scenario("a federation variable holding only a space still selects federation, as in the SDK",
             _usable(FEDERATION_SOURCE),
             profiles={"default": SIGNED_IN}, env={**FEDERATION_ENV, "ANTHROPIC_FEDERATION_RULE_ID": " "}),

    # --- healthy sign-ins
    Scenario("the default sign-in", _usable_profile(), profiles={"default": SIGNED_IN}),
    Scenario("the active sign-in", _usable_profile("work", "active_config"),
             profiles={"work": SIGNED_IN}, active="work\n"),
    Scenario("a sign-in named by ANTHROPIC_PROFILE", _usable_profile("staging", "ANTHROPIC_PROFILE", "default"),
             profiles={"staging": SIGNED_IN}, env={"ANTHROPIC_PROFILE": "staging"}),
    Scenario("a federation sign-in needs no credentials file", _usable_profile(),
             profiles={"default": FEDERATION}),
    Scenario("credentials kept in another file, which is there", _usable_profile(),
             profiles={"default": login_config(credentials_path="{elsewhere}/creds.json")},
             files={"{elsewhere}/creds.json": True}),
    Scenario("credentials kept in the home folder (~)", _usable_profile(),
             profiles={"default": login_config(credentials_path="~/creds.json")},
             files={"{home}/creds.json": True}, posix_only=True),
    Scenario("an empty active_config means the default sign-in", _usable_profile(),
             profiles={"default": SIGNED_IN}, active="  \n"),
    Scenario("an empty ANTHROPIC_PROFILE is unset", _usable_profile(),
             profiles={"default": SIGNED_IN}, env={"ANTHROPIC_PROFILE": ""}),
    Scenario("an empty ANTHROPIC_API_KEY is skipped by the SDK (Dewey itself refuses it)", _usable_profile(),
             profiles={"default": SIGNED_IN}, env={"ANTHROPIC_API_KEY": ""}),

    # --- which source wins
    Scenario("ANTHROPIC_PROFILE comes before federation variables",
             _usable_profile("staging", "ANTHROPIC_PROFILE", "default"),
             profiles={"staging": SIGNED_IN}, env={"ANTHROPIC_PROFILE": "staging", **FEDERATION_ENV}),
    Scenario("federation variables come before a missing active sign-in", _usable(FEDERATION_SOURCE),
             profiles={"default": SIGNED_IN}, active="gone\n", env=FEDERATION_ENV),
    Scenario("an API key comes before a missing named sign-in", _usable("ANTHROPIC_API_KEY"),
             env={"ANTHROPIC_API_KEY": API_KEY, "ANTHROPIC_PROFILE": "gone"}),

    # --- the three cases of issue #19 (i), and their neighbours
    Scenario(CASE_1,
             _missing("profile_not_found", "gone", "ANTHROPIC_PROFILE", "{dir}/configs/gone.json", "default"),
             profiles={"default": SIGNED_IN}, env={"ANTHROPIC_PROFILE": "gone", **FEDERATION_ENV}),
    Scenario("ANTHROPIC_PROFILE names a missing sign-in",
             _missing("profile_not_found", "gone", "ANTHROPIC_PROFILE", "{dir}/configs/gone.json", "default"),
             profiles={"default": SIGNED_IN}, env={"ANTHROPIC_PROFILE": "gone"}),
    Scenario(CASE_2,
             _missing("profile_not_found", "gone", "active_config", "{dir}/configs/gone.json", "gone"),
             profiles={"default": SIGNED_IN}, active="gone\n"),
    Scenario(CASE_3,
             _unconfirmed("signed_out", "default", "default", "{dir}/credentials/default.json", "default"),
             profiles={"default": SIGNED_OUT}),
    Scenario("the active sign-in is signed out",
             _unconfirmed("signed_out", "work", "active_config", "{dir}/credentials/work.json", "work"),
             profiles={"work": SIGNED_OUT, "default": SIGNED_IN}, active="work\n"),
    Scenario("the sign-in named by ANTHROPIC_PROFILE is signed out",
             _unconfirmed("signed_out", "staging", "ANTHROPIC_PROFILE", "{dir}/credentials/staging.json", "default"),
             profiles={"staging": SIGNED_OUT, "default": SIGNED_IN}, env={"ANTHROPIC_PROFILE": "staging"}),
    Scenario("credentials kept in another file, which is missing",
             _unconfirmed("credentials_path_missing", "default", "default", "{elsewhere}/creds.json", "default"),
             profiles={"default": login_config(credentials_path="{elsewhere}/creds.json")}),
    Scenario("a credentials_path that is not text",
             _unconfirmed("credentials_path_missing", "default", "default", "12345", "default"),
             profiles={"default": login_config(credentials_path=12345)}),

    # --- settings files the SDK can't use
    Scenario("a settings file without an authentication block",
             _missing("profile_unreadable", "default", "default", "{dir}/configs/default.json", "default"),
             profiles={"default": _NO_BLOCK}),
    Scenario("a settings file that is an empty object",
             _missing("profile_unreadable", "default", "default", "{dir}/configs/default.json", "default"),
             profiles={"default": {}}),
    Scenario("a settings file that is not JSON",
             _missing("profile_unreadable", "default", "default", "{dir}/configs/default.json", "default"),
             profiles={"default": "{not json"}),
    Scenario("a settings file that is a JSON list",
             _missing("profile_unreadable", "default", "default", "{dir}/configs/default.json", "default"),
             profiles={"default": "[1, 2]"}),
    Scenario("a settings file that is not UTF-8",
             _missing("profile_unreadable", "default", "default", "{dir}/configs/default.json", "default"),
             profiles={"default": b"\xff\xfe{"}),
    Scenario("a damaged settings file for the sign-in ANTHROPIC_PROFILE names",
             _missing("profile_unreadable", "staging", "ANTHROPIC_PROFILE", "{dir}/configs/staging.json", "default"),
             profiles={"staging": "{not json", "default": SIGNED_IN}, env={"ANTHROPIC_PROFILE": "staging"}),
    Scenario("a kind of sign-in the SDK does not know",
             _unconfirmed("profile_type_unknown", "default", "default", "{dir}/configs/default.json", "default"),
             profiles={"default": _OTHER_KIND}, files={"{dir}/credentials/default.json": True}),
    Scenario("a sign-in with no kind at all",
             _unconfirmed("profile_type_unknown", "default", "default", "{dir}/configs/default.json", "default"),
             profiles={"default": _NO_KIND}, files={"{dir}/credentials/default.json": True}),

    # --- names the SDK refuses
    Scenario("ANTHROPIC_PROFILE padded with spaces",
             _missing("profile_name_invalid", " work ", "ANTHROPIC_PROFILE", "", "default"),
             profiles={"work": SIGNED_IN}, env={"ANTHROPIC_PROFILE": " work "}),
    Scenario("ANTHROPIC_PROFILE holding only a space",
             _missing("profile_name_invalid", " ", "ANTHROPIC_PROFILE", "", "default"),
             profiles={"default": SIGNED_IN}, env={"ANTHROPIC_PROFILE": " "}),
    Scenario("ANTHROPIC_PROFILE starting with a dot",
             _missing("profile_name_invalid", ".hidden", "ANTHROPIC_PROFILE", "", "default"),
             profiles={"default": SIGNED_IN}, env={"ANTHROPIC_PROFILE": ".hidden"}),
    Scenario("active_config holding a path",
             _missing("profile_name_invalid", "../x", "active_config", "{dir}/active_config", "../x"),
             profiles={"default": SIGNED_IN}, active="../x\n"),
    Scenario("active_config that is not text",
             _missing("profile_name_invalid", "", "active_config", "{dir}/active_config", ""),
             profiles={"default": SIGNED_IN}, active=_NOT_TEXT, sdk_stops_with="UnicodeDecodeError"),
    Scenario("ANTHROPIC_PROFILE set while active_config is not text",
             _usable_profile("default", "ANTHROPIC_PROFILE", ""),
             profiles={"default": SIGNED_IN}, active=_NOT_TEXT, env={"ANTHROPIC_PROFILE": "default"}),

    # --- the default location: ~/.config/anthropic, found through HOME. There the SDK swallows
    # a broken default sign-in (and a missing one) and builds a client with no credential at all.
    Scenario("default location: the default sign-in", _usable_profile(),
             profiles={"default": SIGNED_IN}, home=True, posix_only=True),
    Scenario("default location: the active sign-in", _usable_profile("work", "active_config"),
             profiles={"work": SIGNED_IN}, active="work\n", home=True, posix_only=True),
    Scenario("default location: federation variables", _usable(FEDERATION_SOURCE),
             env=FEDERATION_ENV, home=True, posix_only=True),
    Scenario("default location: nothing saved", _missing("no_credential", path="{dir}"),
             home=True, posix_only=True),
    Scenario("default location: a damaged default sign-in and no active_config",
             _missing("profile_unreadable", "default", "default", "{dir}/configs/default.json", "default"),
             profiles={"default": _NO_BLOCK}, home=True, posix_only=True),
    Scenario("default location: active_config names a missing sign-in",
             _missing("profile_not_found", "gone", "active_config", "{dir}/configs/gone.json", "gone"),
             profiles={"default": SIGNED_IN}, active="gone\n", home=True, posix_only=True),
    Scenario("default location: signed out",
             _unconfirmed("signed_out", "default", "default", "{dir}/credentials/default.json", "default"),
             profiles={"default": SIGNED_OUT}, home=True, posix_only=True),
]


def scenario(name):
    for row in SCENARIOS:
        if row.name == name:
            return row
    raise KeyError(name)


def _fill(value, names):
    """Replace {dir}, {home} and {elsewhere} in every string of a nested value."""
    if isinstance(value, str):
        for key, path in names.items():
            value = value.replace("{%s}" % key, path)
        return value
    if isinstance(value, dict):
        return {_fill(k, names): _fill(v, names) for k, v in value.items()}
    return value


class Built(NamedTuple):
    env: dict            # the whole environment to judge (and to give the SDK): nothing else is set
    expect: tuple        # the row's expect, with the real paths filled in
    config_dir: str
    home: str


def build(testcase, row):
    """Write a scenario's files into a new temp folder. Returns the environment and the expected verdict."""
    base = temp_dir(testcase)
    home = base / "home"
    config_dir = home / ".config" / "anthropic" if row.home else base / "config"
    names = {"dir": str(config_dir), "home": str(home), "elsewhere": str(base / "elsewhere")}
    home.mkdir()
    make_config_dir(testcase, _fill(row.profiles, names), row.active, config_dir)
    for path in _fill(row.files, names):
        write_credentials(config_dir, path=path)
    env = {"HOME": str(home), **_fill(row.env, names)}
    if not row.home:
        env["ANTHROPIC_CONFIG_DIR"] = str(config_dir)
    path = _fill(row.expect[5], names)
    expect = row.expect[:5] + (os.path.normpath(path) if path else "",) + row.expect[6:]
    return Built(env, expect, str(config_dir), str(home))


def runnable(row):
    """False for a row that needs POSIX (HOME, ~) on another system."""
    return os.name == "posix" or not row.posix_only

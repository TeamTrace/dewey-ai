"""Which Claude credential build_stack() accepts for DEWEY_STACK=anthropic.

anthropic_llm.py constructs Anthropic() with no arguments, so any source the SDK
resolves works: an API key, an auth token, Workload Identity Federation, or a
sign-in saved by `ant auth login`. These tests pin that, and what Dewey does when
the SDK would stop or fail instead (issue #19, box i):

- a sign-in that is named but not saved, or whose settings file can't be used:
  build_stack() raises StackConfigError with a plain message, before the client is built;
- a sign-in that looks signed out: Dewey starts and warns on stderr, because searches
  don't use Claude and signing in again needs no restart;
- anything else the SDK refuses, or a client it builds with no credential at all:
  StackConfigError again, in the SDK's own words (RealClientTest). The exception is a web
  address it refuses or can't read: that is never repeated, since it can carry a password.

The sign-in folders come from tests/claude_profiles.py. Every test replaces the
environment and points ANTHROPIC_CONFIG_DIR or HOME at a temp folder, so none of
them can see the developer's own sign-in, and none uses the network.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import contextlib
import io
import os
import unittest
from unittest import mock

import anthropic

import providers
from models import NewsroomConfig
from providers import claude_credentials
from tests import claude_profiles as cp

NEWSROOM = NewsroomConfig(publisher="The Example Times")
BASE_ENV = {"DEWEY_STACK": "anthropic", "DATABASE_URL": "postgresql://u:p@db.invalid/dewey"}
FEDERATION = cp.FEDERATION_ENV
NEEDS = "DEWEY_STACK=anthropic needs a Claude credential."

# Passwords for a web address (http://reader:PASSWORD@claude-proxy.example.org). The second line
# holds the characters that end the "user:password@" part early for a careless reader of the address.
PASSWORDS = ("hunter2", "p@ssw0rd", "it's-a-secret", 'say"cheese', "a:b:c",
             "pa/ss", "pa ss", "a/b@c", "é/ü", "x://y", "x?y#z", "tab\there", "new\nline", "back\\slash")


class ConfigDirMixin:
    def config_dir(self, profiles=(), active=None):
        """A config directory laid out the way the SDK expects, cleaned up with the test.
        profiles: names of signed-in profiles, or {name: kind} (see tests/claude_profiles.py)."""
        if not isinstance(profiles, dict):
            profiles = {name: cp.SIGNED_IN for name in profiles}
        return cp.make_config_dir(self, profiles, None if active is None else active + "\n")


class CredentialSourceTest(ConfigDirMixin, unittest.TestCase):

    def environment(self, config_dir=None, **env):
        env.setdefault("ANTHROPIC_CONFIG_DIR", config_dir or self.config_dir())
        env.setdefault("HOME", str(cp.temp_dir(self)))
        return mock.patch.dict(os.environ, env, clear=True)

    def source(self, config_dir=None, **env):
        with self.environment(config_dir, **env):
            return providers.anthropic_credential_source()

    def status(self, config_dir=None, **env):
        with self.environment(config_dir, **env):
            return providers.anthropic_credential_status()

    def test_api_key_wins(self):
        self.assertEqual(self.source(ANTHROPIC_API_KEY="sk-ant-x", ANTHROPIC_AUTH_TOKEN="t"),
                         "ANTHROPIC_API_KEY")

    def test_auth_token_alone(self):
        self.assertEqual(self.source(ANTHROPIC_AUTH_TOKEN="sk-ant-oat01-x"), "ANTHROPIC_AUTH_TOKEN")

    def test_federation_needs_all_three(self):
        self.assertEqual(self.source(**FEDERATION), "workload identity federation")
        for dropped in FEDERATION:
            partial = {k: v for k, v in FEDERATION.items() if k != dropped}
            self.assertEqual(self.source(**partial), "", f"accepted federation without {dropped}")

    def test_inline_identity_token_also_counts(self):
        env = {k: v for k, v in FEDERATION.items() if k != "ANTHROPIC_IDENTITY_TOKEN_FILE"}
        self.assertEqual(self.source(**env, ANTHROPIC_IDENTITY_TOKEN="eyJhbGciOi"),
                         "workload identity federation")

    def test_nothing_set(self):
        self.assertEqual(self.source(), "")

    def test_profile_from_active_config(self):
        d = self.config_dir(profiles=["work"], active="work")
        self.assertEqual(self.source(config_dir=d), "profile")

    def test_profile_named_default_without_active_config(self):
        self.assertEqual(self.source(config_dir=self.config_dir(profiles=["default"])), "profile")

    def test_active_config_naming_a_missing_profile_is_not_a_fallthrough(self):
        # Case 2 of issue #19 (i). The SDK stops on this; it does not fall back to "default".
        d = self.config_dir(profiles=["default"], active="gone")
        self.assertEqual(self.source(config_dir=d), "")
        status = self.status(config_dir=d)
        self.assertEqual((status.state, status.reason, status.profile), ("missing", "profile_not_found", "gone"))

    def test_anthropic_profile_env_var_selects_it(self):
        d = self.config_dir(profiles=["staging"])
        self.assertEqual(self.source(config_dir=d, ANTHROPIC_PROFILE="staging"), "profile")

    def test_named_profile_that_does_not_exist_is_not_a_fallthrough(self):
        d = self.config_dir(profiles=["default"])
        self.assertEqual(self.source(config_dir=d, ANTHROPIC_PROFILE="missing"), "")

    def test_named_profile_is_checked_before_federation(self):
        # Case 1 of issue #19 (i): the SDK tries ANTHROPIC_PROFILE first and stops if that sign-in is missing.
        d = self.config_dir(profiles=["default", "staging"])
        self.assertEqual(self.source(config_dir=d, ANTHROPIC_PROFILE="gone", **FEDERATION), "")
        self.assertEqual(self.source(config_dir=d, ANTHROPIC_PROFILE="staging", **FEDERATION), "profile")

    def test_signed_out_profile_is_unconfirmed(self):
        # Case 3 of issue #19 (i): `ant auth logout` leaves the settings file and removes the credentials.
        d = self.config_dir(profiles={"default": cp.SIGNED_OUT})
        status = self.status(config_dir=d)
        self.assertEqual((status.state, status.source, status.reason), ("unconfirmed", "profile", "signed_out"))
        self.assertEqual(self.source(config_dir=d), "", "a signed-out sign-in is not a credential Dewey can count on")

    def test_profile_file_without_authentication_is_not_a_credential(self):
        d = self.config_dir(profiles={"default": {"version": "1.0", "organization_id": "org"}})
        self.assertEqual(self.source(config_dir=d), "")
        self.assertEqual(self.status(config_dir=d).reason, "profile_unreadable")

    def test_blank_values_do_not_count_as_credentials(self):
        self.assertEqual(self.source(ANTHROPIC_API_KEY="   "), "")

    def test_passwords_in_urls_are_masked(self):
        # A password can hold a slash, a space or an @ of its own, so everything from "://" to the
        # last @ on the line is hidden. The text has the shape of the SDK's own message, which quotes
        # the address with repr().
        mask = claude_credentials.mask_url_credentials
        for password in PASSWORDS:
            url = f"http://reader:{password}@claude-proxy.example.org/v1"
            text = f"base_url must use https (got {url!r}); the token-exchange endpoint carries secret material."
            with self.subTest(password):
                self.assertEqual(mask(text), text.replace(repr(url)[1:-1], "http://***@claude-proxy.example.org/v1"))
                self.assertNotIn("reader", mask(text))
        self.assertEqual(mask("Config file not found at /home/you@work/.config/anthropic/configs/gone.json"),
                         "Config file not found at /home/you@work/.config/anthropic/configs/gone.json")
        # One address a line: a line without one is left alone, whatever the lines around it hold.
        self.assertEqual(mask("see https://docs.example.org/help\n"
                              "at /home/you@work\n"
                              "via http://a:b/c@proxy.example.org:1"),
                         "see https://docs.example.org/help\nat /home/you@work\nvia http://***@proxy.example.org:1")

    def test_whatever_else_the_sdk_says_is_repeated_with_passwords_masked_and_control_characters_written_out(self):
        # Not a message the pinned SDK has: the net under any refusal that quotes an address or a path.
        said = ("could not reach http://reader:pa/ss@proxy.example.org/v1\n"
                "from /tmp/odd\x1b[31mfolder/configs/default.json")
        message = claude_credentials.sdk_refusal_message(anthropic.AnthropicError(said), {"HOME": "/nowhere"})
        self.assertIn("It said: could not reach http://***@proxy.example.org/v1\n"
                      "from /tmp/odd\\u001b[31mfolder/configs/default.json\n", message)
        for part in ("reader", "pa/ss", "\x1b"):
            self.assertNotIn(part, message)


class BuildStackTest(ConfigDirMixin, unittest.TestCase):
    """The gate in build_stack(), with the store and the Claude client stubbed out."""

    def build(self, config_dir=None, **env):
        env.setdefault("ANTHROPIC_CONFIG_DIR", config_dir or self.config_dir())
        env.setdefault("HOME", str(cp.temp_dir(self)))
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {**BASE_ENV, **env}, clear=True), \
                mock.patch.object(providers, "build_pgvector_store", return_value=object()), \
                mock.patch("providers.anthropic_llm.AnthropicLLM") as self.llm, \
                contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            providers.build_stack(NEWSROOM)
        return self.llm

    def refused(self, config_dir=None, **env):
        with self.assertRaises(providers.StackConfigError) as ctx:
            self.build(config_dir, **env)
        self.assertFalse(self.llm.called, "the Claude client was built anyway")
        self.assertEqual((self.stdout.getvalue(), self.stderr.getvalue()), ("", ""))
        return str(ctx.exception)

    def test_a_profile_is_enough(self):
        llm = self.build(config_dir=self.config_dir(profiles=["default"]))
        self.assertTrue(llm.called, "build_stack rejected a profile-only configuration")

    def test_api_key_still_works(self):
        self.assertTrue(self.build(ANTHROPIC_API_KEY="sk-ant-x").called)

    def test_usable_credentials_print_nothing(self):
        self.build(ANTHROPIC_API_KEY="sk-ant-x")
        self.assertEqual((self.stdout.getvalue(), self.stderr.getvalue()), ("", ""))
        self.build(config_dir=self.config_dir(profiles=["default"]))
        self.assertEqual((self.stdout.getvalue(), self.stderr.getvalue()), ("", ""))
        self.build(**FEDERATION)
        self.assertEqual((self.stdout.getvalue(), self.stderr.getvalue()), ("", ""))

    def test_no_credential_at_all_is_an_error(self):
        message = self.refused()
        self.assertIn("ant auth login", message)

    def test_no_credential_message_says_where_it_looked(self):
        d = self.config_dir()
        message = self.refused(config_dir=d)
        self.assertTrue(message.startswith(NEEDS), message)
        self.assertIn("ANTHROPIC_API_KEY", message)
        self.assertIn("ant auth login", message)
        self.assertIn(d, message)
        self.assertNotIn("precedence", message)

    def test_missing_named_profile_stops_with_a_plain_message_even_with_federation_set(self):
        # Case 1. Without this gate the SDK's CredentialsError left build_stack() as a traceback.
        d = self.config_dir(profiles=["default"])
        message = self.refused(config_dir=d, ANTHROPIC_PROFILE="gone", **FEDERATION)
        self.assertTrue(message.startswith(NEEDS), message)
        self.assertIn('ANTHROPIC_PROFILE is set to "gone"', message)
        self.assertIn(os.path.join(d, "configs", "gone.json"), message)
        self.assertIn("\n    ant auth login --profile gone\n", message)
        self.assertIn("ANTHROPIC_API_KEY", message.splitlines()[-1])

    def test_missing_active_profile_stops_with_a_plain_message(self):
        # Case 2.
        message = self.refused(config_dir=self.config_dir(profiles=["default"], active="gone"))
        self.assertTrue(message.startswith(NEEDS), message)
        self.assertIn('The active Claude sign-in is "gone"', message)
        self.assertIn("\n    ant profile activate NAME\n", message)

    def test_a_damaged_settings_file_stops_with_a_plain_message(self):
        d = self.config_dir(profiles={"default": "{not json"})
        message = self.refused(config_dir=d)
        self.assertIn(os.path.join(d, "configs", "default.json"), message)
        self.assertIn("\n    ant auth status\n", message)

    def test_signed_out_profile_warns_on_stderr_and_still_builds(self):
        # Case 3: searches don't use Claude, and signing in again needs no restart, so Dewey starts.
        d = self.config_dir(profiles={"default": cp.SIGNED_OUT})
        llm = self.build(config_dir=d)
        self.assertTrue(llm.called)
        warning = self.stderr.getvalue()
        self.assertTrue(warning.startswith("Dewey: warning: could not confirm a Claude credential.\n"), warning)
        self.assertIn(os.path.join(d, "credentials", "default.json"), warning)
        self.assertIn("\n      ant auth status\n", warning)
        self.assertIn("\n      ant auth login --profile default\n", warning)
        self.assertIn("Dewey is starting anyway", warning)
        self.assertIn("no restart is needed", warning)
        self.assertEqual(self.stdout.getvalue(), "", "stdout is the MCP server's JSON-RPC channel")

    def test_unknown_kind_of_sign_in_warns_and_says_to_restart(self):
        d = self.config_dir(profiles={"default": {"version": "1.0", "authentication": {"type": "api_key"}}})
        self.assertTrue(self.build(config_dir=d).called)
        warning = self.stderr.getvalue()
        self.assertIn("could not confirm a Claude credential", warning)
        self.assertIn("restart Dewey", warning)
        self.assertNotIn("ant auth login", warning)
        self.assertEqual(self.stdout.getvalue(), "")

    def test_blank_api_key_is_reported_rather_than_shadowing_a_profile(self):
        d = self.config_dir(profiles=["default"])
        message = self.refused(config_dir=d, ANTHROPIC_API_KEY="")
        self.assertIn("set but empty", message)

    def test_blank_values_are_refused_for_the_reason_that_is_true(self):
        # The pinned SDK skips an empty key and would send one made only of spaces (see
        # SdkAgreementTest in tests/install). It never lets an empty value "take precedence".
        d = self.config_dir(profiles=["default"])
        for env in ({"ANTHROPIC_API_KEY": ""}, {"ANTHROPIC_API_KEY": "  "}, {"ANTHROPIC_AUTH_TOKEN": ""},
                    {"ANTHROPIC_AUTH_TOKEN": " ", "ANTHROPIC_API_KEY": "sk-ant-x"}):
            message = self.refused(config_dir=d, **env)
            with self.subTest(env):
                self.assertIn("set but empty", message)
                self.assertNotIn("precedence", message)
                self.assertIn(next(name for name, value in env.items() if not value.strip()), message)

    def test_database_url_is_still_required(self):
        with self.assertRaises(providers.StackConfigError) as ctx:
            with mock.patch.dict(os.environ, {"DEWEY_STACK": "anthropic",
                                              "ANTHROPIC_API_KEY": "sk-ant-x",
                                              "ANTHROPIC_CONFIG_DIR": self.config_dir()},
                                 clear=True):
                providers.build_stack(NEWSROOM)
        self.assertIn("DATABASE_URL", str(ctx.exception))


class RealClientTest(ConfigDirMixin, unittest.TestCase):
    """build_stack() with the real SDK client (only the store is stubbed): the backstop.

    The gate tested above decides from files. These tests show that whatever it decides, a
    refusal the SDK makes while the client is built, or a web address its HTTP library can't
    read, ends as a StackConfigError in plain words and not as a traceback, and that a healthy
    setup is not tripped up. Not covered, and still that library's own error: a proxy or
    certificate variable it refuses for another reason (an unknown proxy scheme, a certificate
    file that isn't there). Building the client sends nothing; connections are refused to be sure.
    """

    def build(self, **env):
        env.setdefault("HOME", str(cp.temp_dir(self)))
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {**BASE_ENV, **env}, clear=True), cp.no_network(), \
                mock.patch.object(providers, "build_pgvector_store", return_value=object()), \
                contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            llm, _ = providers.build_stack(NEWSROOM)
        self.addCleanup(llm.client.close)
        return llm

    def refused(self, **env):
        with self.assertRaises(providers.StackConfigError) as ctx:
            self.build(**env)
        self.assertEqual(self.stdout.getvalue(), "")
        return str(ctx.exception)

    def test_cases_1_and_2_are_config_errors_with_the_real_client(self):
        d = self.config_dir(profiles=["default"])
        self.assertIn('"gone"', self.refused(ANTHROPIC_CONFIG_DIR=d, ANTHROPIC_PROFILE="gone", **FEDERATION))
        d = self.config_dir(profiles=["default"], active="gone")
        self.assertIn('"gone"', self.refused(ANTHROPIC_CONFIG_DIR=d))

    def test_the_backstop_alone_handles_cases_1_and_2(self):
        # If the check and a future SDK drift apart, the SDK's own error must still end as a
        # configuration error. Force the check to say "usable" and let the real client refuse.
        usable = claude_credentials.ClaudeCredential("usable", "profile", profile="gone")
        for env in ({"ANTHROPIC_CONFIG_DIR": self.config_dir(profiles=["default"]),
                     "ANTHROPIC_PROFILE": "gone", **FEDERATION},
                    {"ANTHROPIC_CONFIG_DIR": self.config_dir(profiles=["default"], active="gone")}):
            with mock.patch.object(providers, "anthropic_credential_status", return_value=usable):
                message = self.refused(**env)
            with self.subTest(sorted(env)):
                self.assertTrue(message.startswith(NEEDS), message)
                self.assertIn("Dewey's Claude client (Anthropic's library) could not load", message)
                self.assertIn("Config file not found", message)
                self.assertIn("\n    ant auth status", message)

    def test_case_3_builds_and_warns_and_fails_only_on_use(self):
        llm = self.build(ANTHROPIC_CONFIG_DIR=self.config_dir(profiles={"default": cp.SIGNED_OUT}))
        self.assertIn("Dewey: warning: could not confirm a Claude credential.", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")
        with cp.no_network(), self.assertRaises(anthropic.CredentialsError):
            llm.client.credentials()

    @unittest.skipUnless(os.name == "posix", "the SDK checks file modes on POSIX only")
    def test_an_sdk_refusal_nobody_listed_becomes_a_config_error(self):
        # The check doesn't look at file modes; the SDK refuses a settings file that others can write.
        d = self.config_dir(profiles=["default"])
        os.chmod(os.path.join(d, "configs", "default.json"), 0o664)
        message = self.refused(ANTHROPIC_CONFIG_DIR=d)
        self.assertTrue(message.startswith(NEEDS), message)
        self.assertIn("writable by group or others", message)

    @unittest.skipUnless(os.name == "posix", "the default location is found through HOME")
    def test_a_client_left_without_a_credential_becomes_a_config_error(self):
        # In the default location, with no active_config, the SDK swallows the same refusal and
        # builds a client with no credential, which would fail on the first question with a TypeError.
        home = cp.temp_dir(self)
        d = cp.make_config_dir(self, {"default": cp.SIGNED_IN}, config_dir=home / ".config" / "anthropic")
        os.chmod(os.path.join(d, "configs", "default.json"), 0o664)
        message = self.refused(HOME=str(home))
        self.assertTrue(message.startswith(NEEDS), message)
        self.assertIn("found none it could use", message)

    def no_part_of_the_address(self, message, password):
        for part in (password, "reader", "claude-proxy", "(got"):
            self.assertNotIn(part, message)
        self.assertNotIn(password, self.stderr.getvalue())
        self.assertIn("not shown", message)

    def test_a_password_in_a_url_is_not_repeated(self):
        # The SDK's https error quotes ANTHROPIC_BASE_URL, which can carry a proxy's password: one with
        # a slash, a space or an @ in it too. Dewey says which setting to change and repeats none of it.
        d = self.config_dir(profiles=["default"])
        for password in PASSWORDS:
            message = self.refused(ANTHROPIC_CONFIG_DIR=d,
                                   ANTHROPIC_BASE_URL=f"http://reader:{password}@claude-proxy.example.org")
            with self.subTest(password):
                self.assertTrue(message.startswith(NEEDS), message)
                self.assertIn("does not begin with https://", message)
                self.assertIn("ANTHROPIC_BASE_URL (in .env or your terminal)", message)
                self.assertIn(os.path.join(d, "configs", "default.json"), message)   # the other place it can be set
                self.no_part_of_the_address(message, password)

    def test_a_password_in_a_sign_ins_own_address_is_not_repeated(self):
        # The same refusal, for "base_url" in the sign-in's settings file. No variable is set, so
        # the message names the file alone.
        for password in PASSWORDS:
            d = self.config_dir(profiles={"default": {
                **cp.login_config(), "base_url": f"http://reader:{password}@claude-proxy.example.org"}})
            cp.write_credentials(d, "default")
            message = self.refused(ANTHROPIC_CONFIG_DIR=d)
            with self.subTest(password):
                self.assertIn("does not begin with https://", message)
                self.assertIn('"base_url" in the sign-in\'s settings file', message)
                self.assertIn(os.path.join(d, "configs", "default.json"), message)
                self.assertNotIn("ANTHROPIC_BASE_URL", message)
                self.no_part_of_the_address(message, password)

    def test_a_password_in_a_url_is_not_repeated_with_federation_variables_either(self):
        for password in PASSWORDS:
            message = self.refused(ANTHROPIC_CONFIG_DIR=self.config_dir(), **FEDERATION,
                                   ANTHROPIC_BASE_URL=f"http://reader:{password}@claude-proxy.example.org")
            with self.subTest(password):
                self.assertIn("does not begin with https://", message)
                self.assertIn("ANTHROPIC_BASE_URL (in .env or your terminal)", message)
                self.assertNotIn("settings file", message)
                self.no_part_of_the_address(message, password)

    def test_a_web_address_that_cannot_be_read_is_a_config_error_in_deweys_own_words(self):
        # The HTTP library under the SDK raises its own error for a badly formed address. That used
        # to leave build_stack() as a traceback, and its text quotes the part it stopped at: with a
        # slash in the password, the part of the password before the slash ("Invalid port: 'Zq7'").
        signed_in, nothing = self.config_dir(profiles=["default"]), self.config_dir()
        bad = {"ANTHROPIC_BASE_URL": "https://reader:Zq7/Kx9@claude-proxy.example.org"}
        for env in ({"ANTHROPIC_CONFIG_DIR": signed_in, **bad},
                    {"ANTHROPIC_CONFIG_DIR": nothing, **bad, "ANTHROPIC_API_KEY": "sk-ant-x"},
                    {"ANTHROPIC_CONFIG_DIR": nothing, **bad, "ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-x"},
                    {"ANTHROPIC_CONFIG_DIR": nothing, **bad, **FEDERATION},
                    {"ANTHROPIC_CONFIG_DIR": nothing, "ANTHROPIC_BASE_URL": "http://localhost:80a80",
                     "ANTHROPIC_API_KEY": "sk-ant-x"}):
            message = self.refused(**env)
            with self.subTest(sorted(env)):
                self.assertTrue(message.startswith(NEEDS), message)
                self.assertIn("ANTHROPIC_BASE_URL (in .env or your terminal) is not a web address", message)
                self.assertIn("not shown", message)
                for part in ("Zq7", "Kx9", "reader", "claude-proxy", "80a80", "Invalid port", "sk-ant"):
                    self.assertNotIn(part, message + self.stderr.getvalue())

    def test_an_unreadable_address_somewhere_else_names_the_places_to_look(self):
        # ANTHROPIC_BASE_URL is not set, so it is not blamed: the address is in the sign-in's settings
        # file (as here) or in a proxy variable, which the same library reads.
        d = self.config_dir(profiles={"default": {
            **cp.login_config(), "base_url": "https://reader:Zq7/Kx9@claude-proxy.example.org"}})
        cp.write_credentials(d, "default")
        message = self.refused(ANTHROPIC_CONFIG_DIR=d)
        self.assertTrue(message.startswith(NEEDS), message)
        self.assertIn(os.path.join(d, "configs", "default.json"), message)
        self.assertIn("HTTPS_PROXY", message)
        self.assertIn("not shown", message)
        for part in ("ANTHROPIC_BASE_URL", "Zq7", "Kx9", "reader", "claude-proxy", "Invalid port"):
            self.assertNotIn(part, message)

    def test_an_unreadable_address_leaves_the_proxy_variables_when_nothing_else_can_be_it(self):
        # The message alone is tested for these: after a refusal over a proxy variable the SDK's
        # half-built HTTP client prints "Exception ignored in ..." of its own, which would land in
        # the test log (seen in a scratch run).
        proxies = "\nLook at the proxy variables, if any are set: HTTP_PROXY, HTTPS_PROXY, ALL_PROXY and NO_PROXY.\n"
        key = claude_credentials.ClaudeCredential("usable", "ANTHROPIC_API_KEY")
        sign_in = claude_credentials.ClaudeCredential("usable", "profile", profile="default")
        variable_set = {"HOME": "/nowhere", "ANTHROPIC_BASE_URL": "https://claude-proxy.example.org"}
        # With the variable set (and readable), the sign-in's own address isn't read when the client
        # is built: see the test below.
        for credential, env in ((key, {"HOME": "/nowhere"}), (sign_in, variable_set)):
            message = claude_credentials.unreadable_address_message(env, credential, False)
            with self.subTest(credential.source):
                self.assertTrue(message.startswith(NEEDS), message)
                self.assertIn(proxies, message)
                self.assertIn("not shown", message)
                for part in ("settings file", "ANTHROPIC_BASE_URL", "claude-proxy"):
                    self.assertNotIn(part, message)

    def test_the_sign_ins_own_address_is_not_read_at_startup_while_the_variable_is_set(self):
        # Why the message above leaves the settings file out then. If a new SDK starts reading it
        # here, this test fails and the message should name the file again.
        d = self.config_dir(profiles={"default": {
            **cp.login_config(), "base_url": "https://reader:Zq7/Kx9@claude-proxy.example.org"}})
        cp.write_credentials(d, "default")
        llm = self.build(ANTHROPIC_CONFIG_DIR=d, ANTHROPIC_BASE_URL="https://other-proxy.example.org")
        self.assertEqual(llm.client.base_url.host, "other-proxy.example.org")
        self.assertEqual((self.stdout.getvalue(), self.stderr.getvalue()), ("", ""))

    @unittest.skipUnless(os.name == "posix", "the SDK checks file modes on POSIX only")
    def test_an_sdk_refusal_names_the_sign_in_anthropic_profile_chose(self):
        # ANTHROPIC_PROFILE may be set in .env only, which ant doesn't read: without --profile, the
        # command would report on the active sign-in instead of the one that was refused.
        d = self.config_dir(profiles=["default", "staging"])
        os.chmod(os.path.join(d, "configs", "staging.json"), 0o664)
        message = self.refused(ANTHROPIC_CONFIG_DIR=d, ANTHROPIC_PROFILE="staging")
        self.assertIn("writable by group or others", message)
        self.assertTrue(message.endswith("\n    ant auth status --profile staging"), message)

    @unittest.skipUnless(os.name == "posix", "the SDK checks file modes on POSIX only")
    def test_an_escape_character_in_a_path_the_sdk_quotes_is_written_out(self):
        # The SDK's refusal quotes the settings file's path as it is. A folder name can hold an
        # escape character, which a terminal would obey; Dewey writes it out.
        d = cp.make_config_dir(self, {"default": cp.SIGNED_IN}, config_dir=cp.temp_dir(self) / "odd\x1b[31mfolder")
        os.chmod(os.path.join(d, "configs", "default.json"), 0o664)
        message = self.refused(ANTHROPIC_CONFIG_DIR=d)
        self.assertIn("writable by group or others", message)
        self.assertIn("odd\\u001b[31mfolder", message)
        self.assertNotIn("\x1b", message)

    def test_an_active_config_that_is_not_text_is_a_config_error_too(self):
        # The SDK lets UnicodeDecodeError out for this file, which is not one of its own errors.
        d = cp.make_config_dir(self, {"default": cp.SIGNED_IN}, active=b"\xff\xfe\n")
        message = self.refused(ANTHROPIC_CONFIG_DIR=d)
        self.assertIn(os.path.join(d, "active_config"), message)
        self.assertIn("\n    ant profile activate NAME", message)

    @unittest.skipUnless(os.name == "posix", "the default location is found through HOME")
    def test_an_active_config_that_is_not_text_stops_the_sdk_even_with_a_key(self):
        # With a key the check is satisfied, but in the default location the SDK still reads
        # active_config (to warn that the key hides a sign-in) and fails on it.
        home = cp.temp_dir(self)
        cp.make_config_dir(self, {"default": cp.SIGNED_IN}, active=b"\xff\xfe\n",
                           config_dir=home / ".config" / "anthropic")
        message = self.refused(HOME=str(home), ANTHROPIC_API_KEY="sk-ant-x")
        self.assertTrue(message.startswith(NEEDS), message)
        self.assertIn("Dewey's Claude client (Anthropic's library) could not load", message)

    def test_a_key_and_a_signed_in_profile_both_build(self):
        llm = self.build(ANTHROPIC_CONFIG_DIR=self.config_dir(), ANTHROPIC_API_KEY="sk-ant-x")
        self.assertEqual(llm.client.api_key, "sk-ant-x")
        self.assertEqual(self.stdout.getvalue(), "")
        llm = self.build(ANTHROPIC_CONFIG_DIR=self.config_dir(profiles=["default"]))
        self.assertEqual(type(llm.client.credentials).__name__, "CredentialsFile")
        self.assertEqual((self.stdout.getvalue(), self.stderr.getvalue()), ("", ""))
        llm = self.build(ANTHROPIC_CONFIG_DIR=self.config_dir(), **FEDERATION)
        self.assertEqual(type(llm.client.credentials).__name__, "WorkloadIdentityCredentials")


if __name__ == "__main__":
    unittest.main()

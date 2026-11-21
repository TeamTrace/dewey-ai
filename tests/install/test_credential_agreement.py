"""The Claude credential check: one rule for the wizard and for Dewey's startup, held against the SDK.

app/providers/claude_credentials.py works out which Claude credential Dewey's client will use.
providers imports it; install.py, which runs before the packages are installed, loads the same
file by path. These tests run every scenario in tests/claude_profiles.py three ways:

- the verdict itself, through the installer (standard library only, so this part runs before
  requirements.txt is installed);
- the wizard (install.anthropic_credential_present) against Dewey's startup check
  (providers.anthropic_credential_source): skipped when providers can't be imported;
- both against what the real SDK does with the same folder and variables: skipped when the
  anthropic package isn't installed. This is the guard for a version bump. If a new SDK changes
  the order or the files, it fails here first.

Then the wizard's own use of the verdict: what it calls missing, what the Summary and the check
step print, and runs of the whole wizard.

No test reads the developer's own sign-in: each gets a throwaway folder, and the environment is
replaced, not added to. No network: connections are refused wherever the real SDK client is built.
"""
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))          # tests.claude_profiles, when this file is run on its own
sys.path.insert(0, str(REPO / "app"))

import contextlib  # noqa: E402
import importlib.util  # noqa: E402
import io  # noqa: E402
import itertools  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import shlex  # noqa: E402
import shutil  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from tests import claude_profiles as cp  # noqa: E402
from tests.docs.test_docs import shell_problems  # noqa: E402

try:
    import providers  # noqa: E402
except ImportError:  # the installer's own tests run before requirements.txt is installed
    providers = None

try:
    import anthropic  # noqa: E402
except ImportError:
    anthropic = None

SHARED_CHECK = pathlib.Path("app", "providers", "claude_credentials.py")
_copies = itertools.count()


def load_install(path=REPO / "install.py", name="dewey_install_cred"):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


install = load_install()

ANTHROPIC = {"DEWEY_STACK": "anthropic", "DATABASE_URL": "postgresql://dewey:pw@db.invalid:5432/dewey"}


def copy_of_the_wizard(testcase, with_shared_check=True):
    """install.py loaded from a copy in a temp folder, so its .env and data/ are temporary by construction."""
    root = cp.temp_dir(testcase, "dewey-wizard-")
    shutil.copy2(REPO / "install.py", root / "install.py")
    if with_shared_check:
        (root / SHARED_CHECK).parent.mkdir(parents=True)
        shutil.copy2(REPO / SHARED_CHECK, root / SHARED_CHECK)
    name = f"dewey_install_copy_{next(_copies)}"
    testcase.addCleanup(sys.modules.pop, name, None)
    return load_install(root / "install.py", name), root


class StatusTest(unittest.TestCase):
    """The verdict for each scenario, through the check as install.py loads it."""

    def setUp(self):
        self.check = install.load_claude_credentials()
        self.assertIsNotNone(self.check, f"install.py could not load {SHARED_CHECK}")

    def verdict(self, name):
        built = cp.build(self, cp.scenario(name))
        return self.check.anthropic_credential_status(built.env), built

    def test_case_1_named_profile_missing_while_federation_is_set(self):
        status, built = self.verdict(cp.CASE_1)
        self.assertEqual((status.state, status.reason, status.profile, status.selected_by),
                         ("missing", "profile_not_found", "gone", "ANTHROPIC_PROFILE"))
        self.assertEqual(status.path, os.path.join(built.config_dir, "configs", "gone.json"))

    def test_case_2_active_profile_missing_while_default_exists(self):
        status, _ = self.verdict(cp.CASE_2)  # no falling back to the default sign-in
        self.assertEqual((status.state, status.reason, status.profile, status.selected_by),
                         ("missing", "profile_not_found", "gone", "active_config"))

    def test_case_3_signed_out_profile_is_unconfirmed(self):
        status, built = self.verdict(cp.CASE_3)
        self.assertEqual((status.state, status.source, status.reason, status.profile),
                         ("unconfirmed", "profile", "signed_out", "default"))
        self.assertEqual(status.path, os.path.join(built.config_dir, "credentials", "default.json"))

    def test_every_scenario_gets_the_expected_verdict(self):
        for row in cp.SCENARIOS:
            if not cp.runnable(row):
                continue
            built = cp.build(self, row)
            with self.subTest(row.name):
                self.assertEqual(tuple(self.check.anthropic_credential_status(built.env)), built.expect)

    def test_only_the_variables_passed_in_are_judged(self):
        """The wizard judges .env's lines together with the terminal's, so the check takes its
        variables as an argument. Nothing may leak in from this process's own environment."""
        built = cp.build(self, cp.scenario(cp.CASE_3))
        elsewhere = cp.build(self, cp.scenario("the default sign-in"))
        with mock.patch.dict(os.environ, {**elsewhere.env, "ANTHROPIC_API_KEY": cp.API_KEY}, clear=True):
            self.assertEqual(tuple(self.check.anthropic_credential_status(built.env)), built.expect)

    def test_the_credentials_file_is_never_opened(self):
        built = cp.build(self, cp.scenario("the active sign-in"))
        opened = []
        real_open = open

        def recording_open(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", recording_open):
            status = self.check.anthropic_credential_status(built.env)
        self.assertEqual(status.state, "usable")
        self.assertEqual(sorted(opened), [os.path.join(built.config_dir, "active_config"),
                                          os.path.join(built.config_dir, "configs", "work.json")])

    def test_a_blank_key_or_token_is_not_a_credential(self):
        # Dewey refuses to start with one (build_stack), so the verdict describes what is left.
        signed_in = cp.build(self, cp.scenario("the default sign-in"))
        nothing = cp.build(self, cp.scenario("nothing set and nothing saved"))
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            for blank in ("", "   "):
                with self.subTest(name=name, blank=blank):
                    status = self.check.anthropic_credential_status({**signed_in.env, name: blank})
                    self.assertEqual((status.state, status.source), ("usable", "profile"))
                    status = self.check.anthropic_credential_status({**nothing.env, name: blank})
                    self.assertEqual((status.state, status.reason), ("missing", "no_credential"))
                    self.assertEqual(self.check.blank_credential_variables({**nothing.env, name: blank}), [name])
        self.assertEqual(self.check.blank_credential_variables(nothing.env), [])
        self.assertIn("set but empty", self.check.blank_credential_message(["ANTHROPIC_API_KEY"]))
        self.assertNotIn("precedence", self.check.blank_credential_message(["ANTHROPIC_API_KEY"]))


class ProblemLinesTest(unittest.TestCase):
    """The lines both the wizard and Dewey print about a sign-in that can't be used."""

    def setUp(self):
        self.check = install.load_claude_credentials()

    def lines(self, name, **env):
        built = cp.build(self, cp.scenario(name))
        status = self.check.anthropic_credential_status({**built.env, **env})
        return self.check.credential_problem_lines(status), built

    def test_usable_and_no_credential_have_no_lines(self):
        for name in ("an API key", "the default sign-in", "nothing set and nothing saved"):
            self.assertEqual(self.lines(name)[0], [], name)

    def test_case_1_names_the_variable_the_file_and_the_fix(self):
        lines, built = self.lines(cp.CASE_1)
        self.assertIn('ANTHROPIC_PROFILE is set to "gone"', lines[0])
        self.assertIn(os.path.join(built.config_dir, "configs", "gone.json"), lines[1])
        self.assertIn("    ant profile list", lines)
        self.assertIn("    ant auth login --profile gone", lines)
        self.assertFalse([line for line in lines if "ant auth status" in line],
                         "ant auth status reports the federation variables as active in this case")

    def test_case_2_names_the_active_sign_in_and_how_to_pick_another(self):
        lines, _ = self.lines(cp.CASE_2)
        self.assertIn('The active Claude sign-in is "gone"', lines[0])
        self.assertIn("    ant profile activate NAME", lines)
        self.assertIn("    ant auth login --profile gone", lines)

    def test_case_3_says_signed_out_and_how_to_check_and_sign_in(self):
        lines, built = self.lines(cp.CASE_3)
        self.assertIn("looks signed out", lines[0])
        self.assertIn(os.path.join(built.config_dir, "credentials", "default.json"), lines[1])
        self.assertIn("    ant auth status", lines)
        self.assertIn("    ant auth login --profile default", lines)
        text = "\n".join(lines)
        self.assertIn("what the ant tool finds in this terminal", text)
        self.assertIn("does not read Dewey's .env", text)
        self.assertIn("configured but not logged in", text)

    def test_signing_in_is_not_said_to_repair_a_damaged_settings_file(self):
        # `ant auth login` never rewrites a settings file that already exists.
        lines, built = self.lines("a settings file that is not JSON")
        text = "\n".join(lines)
        self.assertIn(os.path.join(built.config_dir, "configs", "default.json"), text)
        self.assertIn("    ant auth status", lines)
        self.assertRegex(text, r"[Rr]ename or delete that file")
        self.assertLess(lines.index("    ant auth status"), lines.index("    ant auth login --profile default"))
        self.assertNotIn("repair", text)

    def test_no_login_command_where_signing_in_would_not_help(self):
        for name in ("credentials kept in another file, which is missing",
                     "a kind of sign-in the SDK does not know", "a sign-in with no kind at all"):
            lines, _ = self.lines(name)
            with self.subTest(name):
                self.assertTrue(lines)
                self.assertFalse([line for line in lines if "ant auth login" in line])
                self.assertIn("    ant auth status", lines)
        lines, built = self.lines("credentials kept in another file, which is missing")
        self.assertIn(os.path.join(os.path.dirname(built.config_dir), "elsewhere", "creds.json"), "\n".join(lines))

    def test_credentials_kept_elsewhere_warn_that_ant_will_suggest_signing_in(self):
        # For this layout `ant auth status` (2.12.1) says the sign-in is "configured but not logged in"
        # and to run `ant auth login`. A login writes credentials/NAME.json, which is not the file
        # Dewey's client reads, so the lines say so before they send anyone to that command.
        lines, _ = self.lines("credentials kept in another file, which is missing")
        said = [i for i, line in enumerate(lines) if "does not write that file" in line]
        self.assertEqual(len(said), 1, lines)
        self.assertLess(said[0], lines.index("    ant auth status"))
        # Only this kind of sign-in gets the line: for a plain signed-out one, signing in is the fix.
        lines, _ = self.lines(cp.CASE_3)
        self.assertNotIn("does not write that file", "\n".join(lines))

    def test_a_damaged_settings_file_says_when_anthropic_profile_chose_the_sign_in(self):
        # A message about a sign-in that can't be used says where its name came from.
        lines, built = self.lines("a damaged settings file for the sign-in ANTHROPIC_PROFILE names")
        self.assertIn('"staging"', lines[0])
        self.assertIn("ANTHROPIC_PROFILE names this sign-in.", lines)
        self.assertIn(os.path.join(built.config_dir, "configs", "staging.json"), "\n".join(lines))
        self.assertIn("    ant auth status --profile staging", lines)
        # Not said of a sign-in that no variable chose.
        lines, _ = self.lines("a settings file that is not JSON")
        self.assertNotIn("ANTHROPIC_PROFILE", "\n".join(lines))

    def test_a_name_from_anthropic_profile_is_passed_to_ant_auth_status(self):
        # In another terminal, or with the variable only in .env, plain `ant auth status` looks at the active sign-in.
        lines, _ = self.lines("the sign-in named by ANTHROPIC_PROFILE is signed out")
        self.assertIn("    ant auth status --profile staging", lines)
        self.assertNotIn("    ant auth status", lines)

    def test_signing_in_under_a_name_makes_it_active_and_the_lines_say_so(self):
        # `ant auth login --profile NAME` also activates NAME; other Anthropic tools then use it too.
        for name in (cp.CASE_1, "the sign-in named by ANTHROPIC_PROFILE is signed out"):
            lines, _ = self.lines(name)
            with self.subTest(name):
                login = next(i for i, line in enumerate(lines) if "ant auth login --profile" in line)
                self.assertIn("active sign-in", lines[login + 1])
                self.assertIn("NAME", lines[login + 1])
                self.assertEqual(lines[login + 2], "    ant profile activate NAME")
        # When the sign-in judged is already the active one there is nothing to switch back to.
        for name in (cp.CASE_3, "the active sign-in is signed out"):
            lines, _ = self.lines(name)
            self.assertNotIn("    ant profile activate NAME", lines, name)
        built = cp.build(self, cp.scenario("the active sign-in is signed out"))
        status = self.check.anthropic_credential_status({**built.env, "ANTHROPIC_PROFILE": "work"})
        self.assertEqual((status.selected_by, status.active), ("ANTHROPIC_PROFILE", "work"))
        self.assertNotIn("    ant profile activate NAME", self.check.credential_problem_lines(status))

    def test_bad_names_are_explained_without_a_command_that_uses_them(self):
        lines, _ = self.lines("ANTHROPIC_PROFILE padded with spaces")
        self.assertIn('" work "', lines[0])
        self.assertIn("ANTHROPIC_PROFILE", "\n".join(lines))
        self.assertFalse([line for line in lines if line.startswith("    ")])
        lines, built = self.lines("active_config holding a path")
        self.assertIn('"../x"', lines[0])
        self.assertIn(os.path.join(built.config_dir, "active_config"), "\n".join(lines))
        self.assertIn("    ant profile activate NAME", lines)
        lines, built = self.lines("active_config that is not text")
        self.assertIn(os.path.join(built.config_dir, "active_config"), "\n".join(lines))
        self.assertIn("    ant profile activate NAME", lines)

    def test_a_name_that_is_not_utf8_never_becomes_a_crash(self):
        # A variable holding other bytes reaches Python with lone surrogates in it, which print() refuses.
        built = cp.build(self, cp.scenario("the default sign-in"))
        status = self.check.anthropic_credential_status({**built.env, "ANTHROPIC_PROFILE": "r\udce9daction"})
        self.assertEqual(status.state, "missing")
        for text in ("\n".join(self.check.credential_problem_lines(status)), self.check.startup_error(status),
                     self.check.startup_warning(status._replace(state="unconfirmed", reason="signed_out"))):
            self.assertIn("\\udce9", text)
            text.encode("utf-8")
            # Written out like that it is another name, so no command is built from it.
            self.assertNotIn("--profile", text)
            self.assertIn("can't be shown as it is", text)
        # The wizard's check step prints these lines for a healthy sign-in, as they are.
        found = status._replace(state="usable", source="profile", reason="")
        self.assertEqual(self.check.ant_status_lines(found)[1], "    ant auth status")
        "\n".join(self.check.ant_status_lines(found)).encode("utf-8")

    # Line breaks and the other characters a terminal acts on instead of showing.
    CONTROLS = {chr(n) for n in (*range(0x20), *range(0x7f, 0xa0), 0x2028, 0x2029)}
    REASONS = (("missing", "profile_not_found"), ("missing", "profile_unreadable"), ("unconfirmed", "signed_out"),
               ("unconfirmed", "credentials_path_missing"), ("unconfirmed", "profile_type_unknown"))

    def messages(self, status):
        """Every text Dewey or the wizard builds from a verdict: the lines, and the two startup messages."""
        lines = self.check.credential_problem_lines(status)
        return lines, (self.check.startup_error(status) if status.state == "missing"
                       else self.check.startup_warning(status))

    def test_a_name_with_a_line_break_or_escape_character_is_written_out_and_never_put_in_a_command(self):
        """An active_config file holding two lines, or a variable with an escape character in it, gives
        a name the SDK takes as it is. Printed raw, the line break would cut the printed command in two
        and the escape character would go to the terminal. So the name is written out (\\n) wherever it
        is shown, and no command is built from it."""
        for name in ("work\nextra", "work\x1b[2Jextra", "tab\there", "cr\rhere", "del\x7fhere", "csi\x9b2Jhere",
                     "sep\u2028here"):
            for state, reason in self.REASONS:
                for selected_by in ("ANTHROPIC_PROFILE", "active_config"):
                    status = self.check.ClaudeCredential(state, "" if state == "missing" else "profile", reason, name,
                                                         selected_by, f"/tmp/dewey/configs/{name}.json", "default")
                    lines, message = self.messages(status)
                    with self.subTest(name=name, reason=reason, selected_by=selected_by):
                        self.assertTrue(lines)
                        for line in lines:
                            self.assertFalse(self.CONTROLS & set(line), line)
                        self.assertFalse((self.CONTROLS - {"\n"}) & set(message), message)
                        self.assertNotIn(name, message)   # so no line break of the name's is in it either
                        self.assertFalse([line for line in lines if line.startswith("    ") and "--profile" in line],
                                         "a command was built from a name that can't be typed")
                        # Said once, straight after the line that shows the name.
                        self.assertEqual([i for i, line in enumerate(lines) if "can't be shown as it is" in line], [1])
        status = self.check.ClaudeCredential("missing", "", "profile_not_found", "work\nextra", "active_config",
                                             "/tmp/dewey/configs/work\nextra.json", "work\nextra")
        lines, _ = self.messages(status)
        self.assertIn('"work\\nextra"', lines[0])
        self.assertIn("Dewey looked for /tmp/dewey/configs/work\\nextra.json", lines)
        self.assertIn("    ant profile activate NAME", lines)   # the command that writes active_config again

    @unittest.skipUnless(os.name == "posix", "Windows refuses a file name with a line break in it another way")
    def test_an_active_config_file_holding_two_lines_is_reported_in_one_piece(self):
        # The same, from real files: the SDK reads the whole file as the name.
        config_dir = cp.make_config_dir(self, {"default": cp.SIGNED_IN}, active="work\nextra\n")
        status = self.check.anthropic_credential_status({"HOME": str(cp.temp_dir(self)),
                                                         "ANTHROPIC_CONFIG_DIR": config_dir})
        self.assertEqual((status.state, status.reason, status.profile),
                         ("missing", "profile_not_found", "work\nextra"))
        lines, message = self.messages(status)
        self.assertIn('The active Claude sign-in is "work\\nextra"', lines[0])
        self.assertNotIn("work\nextra", message)
        self.assertFalse([line for line in lines if "ant auth login" in line])

    def test_control_characters_in_a_path_are_written_out(self):
        # A folder name can hold them too (ANTHROPIC_CONFIG_DIR, or "credentials_path" in a settings file).
        odd = "/tmp/dewey\x1b[31m/red\nfolder"
        shown = "/tmp/dewey\\u001b[31m/red\\nfolder"
        for state, reason in self.REASONS:
            status = self.check.ClaudeCredential(state, "" if state == "missing" else "profile", reason, "work",
                                                 "active_config", f"{odd}/work.json", "work")
            lines, message = self.messages(status)
            with self.subTest(reason):
                self.assertIn(f"{shown}/work.json", "\n".join(lines))
                self.assertFalse((self.CONTROLS - {"\n"}) & set(message), message)
                self.assertNotIn(odd, message)
        # Nothing saved at all: the message names the config folder, which ANTHROPIC_CONFIG_DIR can set.
        nothing = self.check.ClaudeCredential("missing", reason="no_credential", path=odd)
        message = self.check.startup_error(nothing)
        self.assertIn(f"it looked for a saved sign-in in {shown}.", message)
        self.assertFalse(self.CONTROLS & set(message), message)

    def test_printed_commands_paste_safely_for_odd_names(self):
        """A sign-in's name can hold spaces and shell characters. Each printed command must be safe to
        paste into zsh, and the shell must hand ant the name as one word, unchanged."""
        fixed = (["ant", "profile", "list"], ["ant", "profile", "activate", "NAME"], ["ant", "auth", "status"])
        for name in ("my work", "we[i]rd?", "it's", "a!b", "x#y", 'say"cheese', "$HOME", "a;b", "é", "plain"):
            for reason, selected_by in (("profile_not_found", "ANTHROPIC_PROFILE"), ("profile_not_found", "active_config"),
                                        ("profile_unreadable", "ANTHROPIC_PROFILE"), ("profile_unreadable", "default"),
                                        ("signed_out", "ANTHROPIC_PROFILE"), ("signed_out", "default")):
                status = self.check.ClaudeCredential(
                    "unconfirmed" if reason == "signed_out" else "missing", "", reason, name, selected_by,
                    "/tmp/x.json", "default")
                commands = [line for line in self.check.credential_problem_lines(status) if line.startswith("    ")]
                self.assertTrue([c for c in commands if "ant auth login" in c], (name, reason))
                for command in commands:
                    with self.subTest(name=name, reason=reason, command=command):
                        self.assertEqual(shell_problems(command), [])
                        words = shlex.split(command)
                        if words not in fixed:
                            self.assertIn(words, (["ant", "auth", "login", "--profile", name],
                                                  ["ant", "auth", "status", "--profile", name]))


@unittest.skipIf(providers is None, "needs requirements.txt (providers imports pydantic)")
class AgreementTest(unittest.TestCase):
    """The wizard and Dewey's startup check: one file, one answer."""

    def test_the_wizard_loads_the_file_that_providers_imports(self):
        loaded = install.load_claude_credentials()
        self.assertTrue(os.path.samefile(loaded.__file__, providers.claude_credentials.__file__))
        self.assertEqual(pathlib.Path(loaded.__file__).resolve(), (REPO / SHARED_CHECK).resolve())

    def test_installer_and_providers_agree_on_every_scenario(self):
        for row in cp.SCENARIOS:
            if not cp.runnable(row):
                continue
            built = cp.build(self, row)
            with self.subTest(row.name), mock.patch.dict(os.environ, built.env, clear=True):
                usable = built.expect[0] == "usable"
                source = providers.anthropic_credential_source()
                self.assertEqual(source, built.expect[1] if usable else "", "providers disagreed")
                self.assertEqual(install.anthropic_credential_present({"DEWEY_STACK": "anthropic"}), usable,
                                 "the installer disagreed")
                self.assertEqual(tuple(providers.anthropic_credential_status()), built.expect)


@unittest.skipIf(anthropic is None, "needs the anthropic package (requirements.txt)")
class SdkAgreementTest(unittest.TestCase):
    """What the pinned SDK really does in each scenario. Run this when the anthropic pin changes."""

    def setUp(self):
        self.check = install.load_claude_credentials()

    @contextlib.contextmanager
    def environment(self, env):
        quiet = logging.root.manager.disable
        logging.disable(logging.WARNING)  # the SDK logs a notice when a key hides a sign-in: not what is tested
        try:
            with mock.patch.dict(os.environ, env, clear=True), cp.no_network():
                yield
        finally:
            logging.disable(quiet)

    def client(self):
        client = anthropic.Anthropic()
        self.addCleanup(client.close)
        return client

    def test_the_sdk_does_what_the_check_predicts(self):
        for row in cp.SCENARIOS:
            if not cp.runnable(row):
                continue
            built = cp.build(self, row)
            state, source, _, profile = built.expect[:4]
            with self.subTest(row.name), self.environment(built.env):
                client, stopped = None, ""
                try:
                    client = self.client()
                except anthropic.AnthropicError:
                    stopped = "AnthropicError"
                except UnicodeDecodeError:
                    stopped = "UnicodeDecodeError"   # an active_config that isn't text: the SDK doesn't catch this
                self.assertEqual(self.check.anthropic_credential_status(built.env).state, state)
                held = [n for n in ("api_key", "auth_token", "credentials")
                        if client is not None and getattr(client, n) is not None]
                if state == "missing":
                    # The SDK stops, or builds a client with no credential at all. Dewey refuses either way.
                    if stopped or row.sdk_stops_with:
                        self.assertEqual(stopped, row.sdk_stops_with or "AnthropicError")
                    else:
                        self.assertEqual(held, [], "the SDK found a credential where the check found none")
                    continue
                self.assertEqual(stopped, "", "the SDK refused a credential the check accepts")
                if source == "profile":
                    self.assertEqual(held, ["credentials"])
                    self.assertEqual(type(client.credentials).__name__, "CredentialsFile")
                    self.assertEqual(client.credentials.profile, profile)
                elif source == cp.FEDERATION_SOURCE:
                    self.assertEqual(held, ["credentials"])
                    self.assertEqual(type(client.credentials).__name__, "WorkloadIdentityCredentials")
                else:
                    self.assertEqual(held, [{"ANTHROPIC_API_KEY": "api_key", "ANTHROPIC_AUTH_TOKEN": "auth_token"}[source]])
                    self.assertEqual(getattr(client, held[0]), built.env[source])
                if state == "unconfirmed":
                    # Built, but it can't produce a token: this is what a first question would hit.
                    with self.assertRaises(anthropic.CredentialsError):
                        client.credentials()

    def test_signing_in_fixes_a_client_that_was_built_signed_out(self):
        """Dewey's warning says no restart is needed after signing in: the SDK reads the file on every call."""
        built = cp.build(self, cp.scenario(cp.CASE_3))
        token = cp.saved_credentials()["access_token"]
        with self.environment(built.env):
            client = self.client()
            with self.assertRaises(anthropic.CredentialsError):
                client.credentials()
            with self.assertRaises(anthropic.CredentialsError):
                client._token_cache.get_token()  # where a request gets its token: a failure isn't remembered
            cp.write_credentials(built.config_dir, "default")
            self.assertEqual(client.credentials().token, token)
            self.assertEqual(client._token_cache.get_token(), token)
            self.assertEqual(self.check.anthropic_credential_status(built.env).state, "usable")

    def test_an_unknown_kind_of_sign_in_does_need_a_restart(self):
        """...and for this one the warning says to restart: the SDK reads the settings file once."""
        built = cp.build(self, cp.scenario("a kind of sign-in the SDK does not know"))
        with self.environment(built.env):
            client = self.client()
            cp.write_profile(built.config_dir, "default", cp.SIGNED_IN)
            self.assertEqual(self.check.anthropic_credential_status(built.env).state, "usable")
            with self.assertRaises(anthropic.CredentialsError):
                client.credentials()
            self.assertIsNotNone(self.client().credentials().token)

    @unittest.skipUnless(os.name == "posix", "the default location is found through HOME")
    def test_default_location_broken_profile_leaves_the_client_without_a_credential(self):
        """Why build_stack() also looks at the client it built: here the SDK raises nothing."""
        built = cp.build(self, cp.scenario("default location: a damaged default sign-in and no active_config"))
        with self.environment(built.env):
            client = self.client()
            self.assertEqual((client.api_key, client.auth_token, client.credentials), (None, None, None))
        self.assertEqual(self.check.anthropic_credential_status(built.env).state, "missing")

    def test_a_blank_key_is_where_dewey_is_stricter_than_the_sdk(self):
        """The reason build_stack() gives for refusing a blank key: the SDK skips an empty one and
        would send one made only of spaces."""
        built = cp.build(self, cp.scenario("the default sign-in"))
        with self.environment({**built.env, "ANTHROPIC_API_KEY": ""}):
            client = self.client()
            self.assertIsNone(client.api_key)
            self.assertEqual(type(client.credentials).__name__, "CredentialsFile")
        with self.environment({**built.env, "ANTHROPIC_API_KEY": "  "}):
            self.assertEqual(self.client().api_key, "  ")


class MissingRequiredTest(unittest.TestCase):
    """What the wizard lists under "Still missing"."""

    def missing(self, name, values=None, env_file=None, **env):
        built = cp.build(self, cp.scenario(name))
        with mock.patch.dict(os.environ, {**built.env, **env}, clear=True):
            if env_file is None:
                return install.missing_required({**ANTHROPIC, **(values or {})})
            return install.missing_required({**ANTHROPIC, **(values or {})}, env_file)

    def test_a_profile_means_the_key_is_not_reported_missing(self):
        self.assertEqual(self.missing("the default sign-in"), [])

    def test_without_any_credential_the_key_is_still_reported(self):
        self.assertEqual(self.missing("nothing set and nothing saved"), ["ANTHROPIC_API_KEY"])

    def test_database_url_is_still_required(self):
        self.assertEqual(self.missing("nothing set and nothing saved",
                                      {"ANTHROPIC_API_KEY": "sk-ant-x", "DATABASE_URL": ""}), ["DATABASE_URL"])

    def test_case_1_is_reported_missing(self):
        self.assertEqual(self.missing(cp.CASE_1), ["ANTHROPIC_API_KEY"])

    def test_case_2_is_reported_missing(self):
        self.assertEqual(self.missing(cp.CASE_2), ["ANTHROPIC_API_KEY"])

    def test_case_3_is_not_counted_as_missing(self):
        # Dewey starts in this state and indexing needs no Claude credential: the Summary warns instead.
        self.assertEqual(self.missing(cp.CASE_3), [])
        self.assertEqual(self.missing("a kind of sign-in the SDK does not know"), [])

    def test_an_api_key_outranks_a_broken_profile(self):
        self.assertEqual(self.missing(cp.CASE_1, {"ANTHROPIC_API_KEY": "sk-ant-x"}), [])

    def test_a_key_that_is_only_in_the_terminal_counts(self):
        # Dewey reads the terminal's variables too, and python-dotenv never replaces one that is set.
        self.assertEqual(self.missing(cp.CASE_3, ANTHROPIC_API_KEY=cp.API_KEY), [])
        self.assertEqual(self.missing("nothing set and nothing saved", ANTHROPIC_API_KEY=cp.API_KEY), [])

    def test_anthropic_profile_kept_in_the_env_file_is_used(self):
        # Dewey reads ANTHROPIC_PROFILE from .env; a wizard that looked only at the terminal would miss it.
        self.assertEqual(self.missing("the default sign-in", env_file={"ANTHROPIC_PROFILE": "gone"}),
                         ["ANTHROPIC_API_KEY"])
        self.assertEqual(self.missing(cp.CASE_2), ["ANTHROPIC_API_KEY"])
        self.assertEqual(self.missing(cp.CASE_2, env_file={"ANTHROPIC_PROFILE": "default"}), [])

    def test_terminal_variables_outrank_env_file_lines(self):
        self.assertEqual(self.missing("the default sign-in", env_file={"ANTHROPIC_PROFILE": "gone"},
                                      ANTHROPIC_PROFILE="default"), [])
        self.assertEqual(self.missing("the default sign-in", env_file={"ANTHROPIC_PROFILE": "default"},
                                      ANTHROPIC_PROFILE="gone"), ["ANTHROPIC_API_KEY"])

    def test_other_stacks_never_ask_for_a_claude_credential(self):
        with mock.patch.dict(os.environ, cp.build(self, cp.scenario(cp.CASE_1)).env, clear=True):
            self.assertEqual(install.missing_required({"DEWEY_STACK": "open", "OPEN_LLM_MODEL": "qwen3:8b",
                                                       "DATABASE_URL": "postgresql://x/y"}), [])


class SummaryTest(unittest.TestCase):
    """What the Summary says after the .env text."""

    STILL_MISSING = ("Still missing: ANTHROPIC_API_KEY. Dewey won't start until these are set; "
                     "re-run this installer or edit .env when you have them.")

    def report(self, name, values=None, env_file=None, **env):
        built = cp.build(self, cp.scenario(name))
        with mock.patch.dict(os.environ, {**built.env, **env}, clear=True):
            values = {**ANTHROPIC, **(values or {})}
            missing = install.missing_required(values, env_file)
            return install.summary_report(values, missing, env_file), built

    def test_nothing_to_say_about_a_healthy_setup(self):
        for name in ("the default sign-in", "federation variables", "an auth token"):
            self.assertEqual(self.report(name)[0], [], name)
        self.assertEqual(self.report("nothing set and nothing saved", {"ANTHROPIC_API_KEY": "sk-ant-x"})[0], [])

    def test_no_credential_keeps_todays_line_only(self):
        self.assertEqual(self.report("nothing set and nothing saved")[0], [self.STILL_MISSING])

    def test_case_1_names_the_sign_in_and_the_fix(self):
        lines, _ = self.report(cp.CASE_1)
        self.assertEqual(lines[0], self.STILL_MISSING)
        self.assertTrue(lines[1].startswith("About ANTHROPIC_API_KEY:"), lines[1])
        self.assertIn('  ANTHROPIC_PROFILE is set to "gone", but no Claude sign-in with that name is saved on this '
                      'computer.', lines)
        self.assertIn("      ant profile list", lines)
        self.assertIn("      ant auth login --profile gone", lines)
        self.assertIn("API key", lines[-1])

    def test_case_2_names_the_sign_in_and_the_fix(self):
        lines, _ = self.report(cp.CASE_2)
        self.assertEqual(lines[0], self.STILL_MISSING)
        self.assertIn('The active Claude sign-in is "gone"', "\n".join(lines))
        self.assertIn("      ant profile activate NAME", lines)

    def test_case_3_says_could_not_confirm_and_how_to_sign_in(self):
        lines, built = self.report(cp.CASE_3)
        self.assertTrue(lines[0].startswith("Dewey could not confirm a Claude credential."), lines[0])
        text = "\n".join(lines)
        self.assertNotIn("Still missing", text)
        self.assertNotIn("won't start", text)
        self.assertIn("      ant auth status", lines)
        self.assertIn("      ant auth login --profile default", lines)
        self.assertIn(os.path.join(built.config_dir, "credentials", "default.json"), text)

    def test_case_3_with_another_gap_keeps_the_still_missing_line_for_that_gap(self):
        lines, _ = self.report(cp.CASE_3, {"DATABASE_URL": ""})
        self.assertTrue(lines[0].startswith("Still missing: DATABASE_URL. "), lines[0])
        self.assertTrue(lines[1].startswith("Dewey could not confirm a Claude credential."), lines[1])

    def test_an_unknown_kind_of_sign_in_is_reported_without_a_login_command(self):
        lines, _ = self.report("a kind of sign-in the SDK does not know")
        self.assertTrue(lines[0].startswith("Dewey could not confirm a Claude credential."), lines[0])
        self.assertNotIn("ant auth login", "\n".join(lines))

    def test_a_key_that_is_only_in_the_terminal_is_pointed_out(self):
        lines, _ = self.report(cp.CASE_3, ANTHROPIC_API_KEY=cp.API_KEY)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("ANTHROPIC_API_KEY", lines[0])
        self.assertIn("this terminal", lines[0])
        self.assertIn("not saved in .env", lines[0])
        self.assertNotIn(cp.API_KEY, lines[0])
        # A key typed into the wizard is saved, so there is nothing to point out.
        self.assertEqual(self.report(cp.CASE_3, {"ANTHROPIC_API_KEY": "sk-ant-typed"}, ANTHROPIC_API_KEY=cp.API_KEY)[0], [])

    def test_a_key_typed_into_the_wizard_is_the_credential_that_is_judged(self):
        # The key is about to be written to .env, so Dewey will have it whatever the saved sign-in is
        # like. No key is in the terminal here: only the typed one can make these two cases usable.
        typed = {**ANTHROPIC, "ANTHROPIC_API_KEY": "sk-ant-typed"}
        for name in (cp.CASE_3, cp.CASE_1):
            with self.subTest(name):
                self.assertEqual(self.report(name, typed)[0], [])
                with mock.patch.dict(os.environ, cp.build(self, cp.scenario(name)).env, clear=True):
                    self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
                    self.assertTrue(install.anthropic_credential_present(typed))
                    self.assertEqual(install.claude_credential(typed).source, "ANTHROPIC_API_KEY")
                    self.assertFalse(install.anthropic_credential_present(dict(ANTHROPIC)))

    def test_a_blank_token_or_exported_blank_key_is_reported(self):
        # Dewey refuses to start with either, whatever else is set.
        lines, _ = self.report("the default sign-in", ANTHROPIC_AUTH_TOKEN="")
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("ANTHROPIC_AUTH_TOKEN is set but empty in this terminal", lines[0])
        lines, _ = self.report("the default sign-in", env_file={"ANTHROPIC_AUTH_TOKEN": "  "})
        self.assertIn("ANTHROPIC_AUTH_TOKEN is set but empty in .env", lines[0])
        lines, _ = self.report("the default sign-in", {"ANTHROPIC_API_KEY": "sk-ant-typed"}, ANTHROPIC_API_KEY="")
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("ANTHROPIC_API_KEY is set but empty in this terminal", lines[0])
        # A real token in the terminal hides a blank line in .env, as it does for Dewey.
        self.assertEqual(self.report("the default sign-in", env_file={"ANTHROPIC_AUTH_TOKEN": ""},
                                     ANTHROPIC_AUTH_TOKEN=cp.AUTH_TOKEN)[0], [])

    def test_other_stacks_get_the_still_missing_line_and_nothing_else(self):
        with mock.patch.dict(os.environ, cp.build(self, cp.scenario(cp.CASE_1)).env, clear=True):
            values = {"DEWEY_STACK": "open", "OPEN_LLM_MODEL": "", "DATABASE_URL": "postgresql://x/y"}
            self.assertEqual(install.summary_report(values, install.missing_required(values)),
                             ["Still missing: OPEN_LLM_MODEL. Dewey won't start until these are set; "
                              "re-run this installer or edit .env when you have them."])


class CheckServicesTest(unittest.TestCase):
    """The Claude line of "Check credentials and services". Only an API key is tested against Anthropic."""

    VALUES = {"DEWEY_STACK": "anthropic", "EMBEDDING_PROVIDER": "local", "RERANK_PROVIDER": "none"}  # no database

    def check(self, name, values=None, env_file_text="", **env):
        built = cp.build(self, cp.scenario(name))
        env_path = cp.temp_dir(self) / ".env"
        if env_file_text:
            env_path.write_text(env_file_text, encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {**built.env, **env}, clear=True), cp.no_network(), \
                mock.patch.object(install, "ENV_PATH", env_path), contextlib.redirect_stdout(out):
            install.check_services({**self.VALUES, **(values or {})})
        return out.getvalue()

    def test_a_sign_in_gets_a_not_checked_line(self):
        shown = self.check("the default sign-in")
        self.assertIn('Claude sign-in: not checked (this step tests an API key only); found the saved sign-in "default".',
                      shown)
        self.assertIn("          ant auth status\n", shown)
        self.assertNotIn(": ok", shown)

    def test_a_token_and_federation_get_a_not_checked_line(self):
        self.assertIn("Claude credential: not checked (this step tests an API key only); ANTHROPIC_AUTH_TOKEN is set",
                      self.check("an auth token"))
        self.assertIn("not checked (this step tests an API key only); Workload Identity Federation variables are set",
                      self.check("federation variables"))

    def test_the_three_cases_never_pass_silently(self):
        for name in (cp.CASE_1, cp.CASE_2, cp.CASE_3, "nothing set and nothing saved",
                     "a kind of sign-in the SDK does not know"):
            shown = self.check(name)
            with self.subTest(name):
                claude = [line for line in shown.splitlines() if line.startswith("  Claude")]
                self.assertEqual(len(claude), 1, shown)
                self.assertIn("NOT", claude[0])
                self.assertNotIn(": ok", shown)
        self.assertIn("NOT confirmed", self.check(cp.CASE_3))
        self.assertIn("looks signed out", self.check(cp.CASE_3))

    def test_lines_kept_in_the_env_file_are_judged_too(self):
        # check_services() runs after .env is written, so it reads the file Dewey will read.
        shown = self.check("the active sign-in is signed out", env_file_text="ANTHROPIC_PROFILE='default'\n")
        self.assertIn('found the saved sign-in "default"', shown)
        shown = self.check("the default sign-in", env_file_text="ANTHROPIC_PROFILE='gone'\n")
        self.assertIn("NOT found", shown)

    @unittest.skipIf(anthropic is None, "needs the anthropic package (requirements.txt)")
    def test_only_a_key_typed_into_the_wizard_is_tested_against_anthropic(self):
        with mock.patch.object(anthropic, "Anthropic") as client:
            shown = self.check(cp.CASE_3, {"ANTHROPIC_API_KEY": "sk-ant-typed"})
        self.assertIn("  Anthropic API key: ok", shown)
        client.assert_called_once_with(api_key="sk-ant-typed")
        client.return_value.models.list.assert_called_once_with(limit=1)
        # A key that is only in the terminal is reported, not used: a variable alone never makes
        # this step (or a test that calls it) contact Anthropic.
        with mock.patch.object(anthropic, "Anthropic") as client:
            shown = self.check(cp.CASE_3, ANTHROPIC_API_KEY=cp.API_KEY)
        client.assert_not_called()
        self.assertIn("  Anthropic API key: not checked: it is set in this terminal", shown)
        self.assertNotIn(cp.API_KEY, shown)
        self.assertNotIn(": ok", shown)

    def claude_lines(self, shown):
        return [line for line in shown.splitlines() if line.startswith(("  Claude", "  Anthropic"))]

    def test_a_blank_key_or_token_is_reported_instead_of_a_sign_in_that_was_found(self):
        # Dewey refuses to start with a blank ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN whatever else
        # is set, so this step must not say "found" (or "ok") while one is.
        blank_token_line = "ANTHROPIC_AUTH_TOKEN=''\n"
        for env, env_file_text, said in (
                ({"ANTHROPIC_API_KEY": "   "}, "", "ANTHROPIC_API_KEY is set but empty in this terminal"),
                ({"ANTHROPIC_AUTH_TOKEN": ""}, "", "ANTHROPIC_AUTH_TOKEN is set but empty in this terminal"),
                ({}, blank_token_line, "ANTHROPIC_AUTH_TOKEN is set but empty in .env")):
            shown = self.check("the default sign-in", env_file_text=env_file_text, **env)
            with self.subTest(said):
                claude = self.claude_lines(shown)
                self.assertEqual(len(claude), 1, shown)
                self.assertIn("NOT usable", claude[0])
                self.assertIn(said, claude[0])
                self.assertIn("won't start", claude[0])
                self.assertNotIn("found the saved sign-in", shown)
        # A real token in the terminal hides a blank line in .env, as it does for Dewey.
        shown = self.check("the default sign-in", env_file_text=blank_token_line, ANTHROPIC_AUTH_TOKEN=cp.AUTH_TOKEN)
        self.assertNotIn("NOT", shown)
        self.assertIn("ANTHROPIC_AUTH_TOKEN is set", shown)

    @unittest.skipIf(anthropic is None, "needs the anthropic package (requirements.txt)")
    def test_a_blank_token_is_reported_instead_of_ok_for_a_typed_key(self):
        # The key is fine, and Dewey still won't start: "ok" would be the wrong thing to read here.
        with mock.patch.object(anthropic, "Anthropic") as client:
            shown = self.check("nothing set and nothing saved", {"ANTHROPIC_API_KEY": "sk-ant-typed"},
                               env_file_text="ANTHROPIC_API_KEY='sk-ant-typed'\nANTHROPIC_AUTH_TOKEN=''\n")
        claude = self.claude_lines(shown)
        self.assertEqual(len(claude), 1, shown)
        self.assertIn("NOT usable", claude[0])
        self.assertIn("ANTHROPIC_AUTH_TOKEN is set but empty in .env", claude[0])
        self.assertIn("not tested", claude[0])
        self.assertNotIn(": ok", shown)
        self.assertNotIn("sk-ant-typed", shown)
        client.assert_not_called()
        # A blank key exported in the terminal hides the key that was typed and saved.
        with mock.patch.object(anthropic, "Anthropic") as client:
            shown = self.check("nothing set and nothing saved", {"ANTHROPIC_API_KEY": "sk-ant-typed"},
                               env_file_text="ANTHROPIC_API_KEY='sk-ant-typed'\n", ANTHROPIC_API_KEY="")
        self.assertIn("NOT usable: ANTHROPIC_API_KEY is set but empty in this terminal", shown)
        client.assert_not_called()

    @unittest.skipIf(anthropic is None, "needs the anthropic package (requirements.txt)")
    def test_a_web_address_the_http_library_cannot_read_is_not_quoted_back(self):
        # Its error quotes the part it stopped at, which is part of the password here ("Invalid port: 'Zq7'").
        # The client is never built, so nothing is sent.
        shown = self.check(cp.CASE_3, {"ANTHROPIC_API_KEY": "sk-ant-typed"},
                           ANTHROPIC_BASE_URL="https://reader:Zq7/Kx9@claude-proxy.example.org")
        claude = self.claude_lines(shown)
        self.assertEqual(len(claude), 1, shown)
        self.assertIn("NOT checked", claude[0])
        self.assertIn("ANTHROPIC_BASE_URL", claude[0])
        self.assertIn("not shown", claude[0])
        for part in ("Zq7", "Kx9", "reader", "claude-proxy", "Invalid port", "sk-ant-typed"):
            self.assertNotIn(part, shown)


class WithoutTheSharedCheckTest(unittest.TestCase):
    """install.py without app/providers/claude_credentials.py next to it (a partial copy of the
    repository): it can't look for sign-ins, says so in one line, and counts only a typed API key."""

    def setUp(self):
        self.wizard, _ = copy_of_the_wizard(self, with_shared_check=False)
        self.env = cp.build(self, cp.scenario("the default sign-in")).env

    def test_only_a_typed_key_counts_and_one_line_says_why(self):
        self.assertIsNone(self.wizard.load_claude_credentials())
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(self.wizard.missing_required(dict(ANTHROPIC)), ["ANTHROPIC_API_KEY"])
            self.assertFalse(self.wizard.anthropic_credential_present(dict(ANTHROPIC)))
            lines = self.wizard.summary_report(dict(ANTHROPIC), ["ANTHROPIC_API_KEY"])
            self.assertEqual(len(lines), 2, lines)
            self.assertTrue(lines[0].startswith("Still missing: ANTHROPIC_API_KEY."))
            self.assertIn("could not check for a saved Claude sign-in", lines[1])
            typed = {**ANTHROPIC, "ANTHROPIC_API_KEY": "sk-ant-typed"}
            self.assertEqual(self.wizard.missing_required(typed), [])
            self.assertTrue(self.wizard.anthropic_credential_present(typed))
            self.assertEqual(self.wizard.summary_report(typed, []), [])

    def test_the_check_step_says_not_checked(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(out):
            self.wizard.check_services({"DEWEY_STACK": "anthropic", "EMBEDDING_PROVIDER": "local",
                                        "RERANK_PROVIDER": "none"})
        claude = [line for line in out.getvalue().splitlines() if line.startswith("  Claude")]
        self.assertEqual(len(claude), 1, out.getvalue())
        self.assertIn("NOT checked", claude[0])


class WizardRunTest(unittest.TestCase):
    """The whole wizard, in this process, on a copy of install.py in a temp folder.

    input() and getpass() are answered by what the question says, not by position, and every
    prompt is recorded: "the check was offered" is a fact about the prompts, which never reach the
    captured output. PATH holds one empty folder, so no docker and no ollama are found, and the
    answers install nothing, check nothing and import nothing.
    """

    # The last of these printed before "Choose a number or name" is the question being asked.
    CHOICES = [
        ("Dewey can run on three stacks", "anthropic"),
        ("Where should embeddings", "local"),
        ("Where should results be reranked", "none"),
        ("Do you have a Postgres database", "yes"),
        ("Where is your archive", "skip"),
    ]
    ANSWERS = [
        ("Paste your Anthropic API key", ""),             # Enter: no key
        ("Database URL", ANTHROPIC["DATABASE_URL"]),      # never contacted: the check is declined below
        ("Newsroom name", "The Example Times"),
        ("Earliest article date", ""),
        ("Region", "none"),
        ("Write this to .env", "y"),
        ("Install ", "n"),                                # either wording of the install question
        ("Check your credentials and services now", "n"),
    ]
    CHECK_OFFERED = "Check your credentials and services now"

    def run_wizard(self, name, with_shared_check=True, existing_env="", **env):
        """Run main() in the scenario. Returns the Summary's text, what was offered afterwards, and the folder."""
        built = cp.build(self, cp.scenario(name))
        empty_path = cp.temp_dir(self)
        out, prompts = io.StringIO(), []

        def answer(prompt=""):
            prompts.append(prompt)
            if "Choose a number or name" in prompt:
                shown = out.getvalue()
                asked = max(self.CHOICES, key=lambda choice: shown.rfind(choice[0]))
                if shown.rfind(asked[0]) >= 0:
                    return asked[1]
            for needle, reply in self.ANSWERS:
                if needle in prompt:
                    return reply
            raise AssertionError(f"unexpected prompt: {prompt!r}\noutput so far:\n{out.getvalue()}")

        with mock.patch.dict(os.environ, {**built.env, "PATH": str(empty_path), **env}, clear=True), cp.no_network():
            wizard, root = copy_of_the_wizard(self, with_shared_check)
            if existing_env:
                (root / ".env").write_text(existing_env, encoding="utf-8")
            with mock.patch("builtins.input", answer), mock.patch.object(wizard.getpass, "getpass", answer), \
                    mock.patch.object(wizard, "index_archive") as index_archive, \
                    mock.patch.object(sys, "argv", ["install.py", "--no-browser"]), contextlib.redirect_stdout(out):
                code = wizard.main()
        shown = out.getvalue()
        if code != 0 and not prompts and "Dewey needs Python" in shown:
            self.skipTest("the wizard doesn't run on this Python version: it stopped at its version check")
        self.assertEqual(code, 0, shown)
        summary = shown[shown.index("Summary"):shown.index("Next steps")]
        offered = {"check": any(self.CHECK_OFFERED in p for p in prompts), "indexing": index_archive.called}
        return summary, offered, root

    def test_signed_out_sign_in_is_reported_and_the_check_is_still_offered(self):
        summary, offered, root = self.run_wizard(cp.CASE_3)
        self.assertIn("Dewey could not confirm a Claude credential.", summary)
        self.assertIn("      ant auth status\n", summary)
        self.assertIn("      ant auth login --profile default\n", summary)
        self.assertNotIn("Still missing", summary)
        # Not counted as missing: the check and indexing must not hang on a verdict made from files.
        self.assertEqual(offered, {"check": True, "indexing": True})
        written = (root / ".env").read_text(encoding="utf-8")
        self.assertIn("DEWEY_STACK='anthropic'", written)
        self.assertNotIn("ANTHROPIC_API_KEY", written)

    def test_missing_named_sign_in_is_listed_as_missing_and_the_check_is_not_offered(self):
        summary, offered, _ = self.run_wizard(cp.CASE_1)
        self.assertIn("Still missing: ANTHROPIC_API_KEY.", summary)
        self.assertIn('ANTHROPIC_PROFILE is set to "gone"', summary)
        self.assertIn("      ant auth login --profile gone\n", summary)
        self.assertEqual(offered, {"check": False, "indexing": False})

    def test_missing_active_sign_in_is_listed_as_missing(self):
        summary, offered, _ = self.run_wizard(cp.CASE_2)
        self.assertIn("Still missing: ANTHROPIC_API_KEY.", summary)
        self.assertIn('The active Claude sign-in is "gone"', summary)
        self.assertEqual(offered, {"check": False, "indexing": False})

    def test_a_healthy_sign_in_needs_no_key_and_gets_no_warning(self):
        summary, offered, _ = self.run_wizard("the default sign-in")
        self.assertNotIn("Still missing", summary)
        self.assertNotIn("could not confirm", summary)
        self.assertEqual(offered, {"check": True, "indexing": True})

    def test_sign_in_settings_kept_in_the_env_file_are_judged(self):
        # ANTHROPIC_PROFILE written by hand in .env: the wizard keeps the line, and Dewey will read it.
        summary, offered, root = self.run_wizard("the default sign-in", existing_env="ANTHROPIC_PROFILE='gone'\n")
        self.assertIn("Still missing: ANTHROPIC_API_KEY.", summary)
        self.assertIn('ANTHROPIC_PROFILE is set to "gone"', summary)
        self.assertEqual(offered, {"check": False, "indexing": False})
        self.assertIn("ANTHROPIC_PROFILE='gone'", (root / ".env").read_text(encoding="utf-8"))

    def test_without_the_shared_check_the_wizard_still_runs_and_asks_for_a_key(self):
        summary, offered, _ = self.run_wizard("the default sign-in", with_shared_check=False)
        self.assertIn("Still missing: ANTHROPIC_API_KEY.", summary)
        self.assertEqual(summary.count("could not check for a saved Claude sign-in"), 1, summary)
        self.assertEqual(offered, {"check": False, "indexing": False})


if __name__ == "__main__":
    unittest.main()

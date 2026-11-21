"""Starting the MCP server on Claude when the saved sign-in can't be used (issue #19, box i).

In-process, with the real Claude client and the sign-in folders of tests/claude_profiles.py; only
the store and the MCP transport are stubbed. Building the client sends nothing, and connections
are refused to be sure. The environment is replaced, so the developer's own sign-in, .env and
DEWEY_MCP_TEST_STACK play no part.

- A sign-in that is named but not saved: Anthropic's client stops when it is built. Its error used
  to leave main() as a traceback; now it is one "configuration error" message and exit status 1.
- A sign-in that looks signed out: the client is built and fails on its first request. The server
  starts, because search_archive never calls Claude, and says so on stderr. stdout stays empty:
  in stdio mode it is the JSON-RPC channel.
- A web address (ANTHROPIC_BASE_URL) the client refuses or can't read: a configuration error too,
  and the address is never printed, since it can carry a password.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import contextlib
import io
import os
import unittest
from unittest import mock

import mcp_server
from tests import claude_profiles as cp

BASE_ENV = {"DEWEY_STACK": "anthropic", "NEWSROOM_NAME": "The Example Times",
            "DATABASE_URL": "postgresql://dewey:pw@db.invalid:5432/dewey"}
CONFIG_ERROR = "dewey-mcp: configuration error: DEWEY_STACK=anthropic needs a Claude credential."


class ClaudeStartupTest(unittest.TestCase):

    def start(self, name, argv=(), **env):
        built = cp.build(self, cp.scenario(name))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {**BASE_ENV, **built.env, **env}, clear=True), cp.no_network(), \
                mock.patch.object(mcp_server, "load_dotenv"), \
                mock.patch("providers.build_pgvector_store", return_value=object()), \
                mock.patch.object(mcp_server, "create_server") as create_server, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mcp_server.main(list(argv))
        if create_server.called:
            self.addCleanup(create_server.call_args.args[0].client.close)
        return code, out.getvalue(), err.getvalue(), create_server

    def test_missing_named_profile_is_a_configuration_error(self):
        code, out, err, create_server = self.start(cp.CASE_1)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith(CONFIG_ERROR), err)
        self.assertIn('ANTHROPIC_PROFILE is set to "gone"', err)
        self.assertIn("\n    ant auth login --profile gone\n", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(out, "")
        create_server.assert_not_called()

    def test_missing_active_profile_is_a_configuration_error(self):
        code, out, err, create_server = self.start(cp.CASE_2)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith(CONFIG_ERROR), err)
        self.assertIn('The active Claude sign-in is "gone"', err)
        self.assertEqual(out, "")
        create_server.assert_not_called()

    def test_signed_out_profile_starts_and_warns_on_stderr_only(self):
        code, out, err, create_server = self.start(cp.CASE_3)
        self.assertEqual(code, 0)
        create_server.return_value.run.assert_called_once_with(transport="stdio")
        self.assertIn("Dewey: warning: could not confirm a Claude credential.", err)
        self.assertIn("Searches (search_archive) work", err)
        self.assertIn("\n      ant auth login --profile default\n", err)
        self.assertEqual(out, "")

    def test_a_healthy_sign_in_starts_without_a_word(self):
        code, out, err, create_server = self.start("the default sign-in")
        self.assertEqual(code, 0)
        create_server.return_value.run.assert_called_once_with(transport="stdio")
        self.assertEqual((out, err), ("", ""))

    def test_a_web_address_with_a_password_is_never_printed_when_it_stops_the_server(self):
        # Two ways a web address stops Anthropic's client, and in neither may the address be shown.
        # http:// with a sign-in: the client refuses it and quotes it whole. A slash in the password:
        # the HTTP library can't read the address, and its error (once a traceback) quotes part of it.
        for name, address in (("the default sign-in", "http://reader:Zq7/Kx9@claude-proxy.example.org"),
                              ("an API key", "https://reader:Zq7/Kx9@claude-proxy.example.org")):
            code, out, err, create_server = self.start(name, ANTHROPIC_BASE_URL=address)
            with self.subTest(name):
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith(CONFIG_ERROR), err)
                self.assertIn("ANTHROPIC_BASE_URL", err)
                self.assertIn("not shown", err)
                for part in ("Traceback", "Zq7", "Kx9", "reader", "claude-proxy", "Invalid port"):
                    self.assertNotIn(part, err)
                self.assertEqual(out, "")
                create_server.assert_not_called()


if __name__ == "__main__":
    unittest.main()

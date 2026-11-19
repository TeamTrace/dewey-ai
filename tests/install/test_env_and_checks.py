"""
Tests for install.py's .env handling (what the Summary and --dry-run print) and
its credential check against the chat model server. No network: the model
servers are fake ones on 127.0.0.1.
"""
import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[2]


def load_install():
    spec = importlib.util.spec_from_file_location("dewey_install_env", REPO / "install.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


install = load_install()


class SummaryMaskingTests(unittest.TestCase):
    """README promises the dry run prints the .env 'with secrets masked'."""

    OLD_ENV = "\n".join([
        "DEWEY_STACK='anthropic'",
        "ANTHROPIC_API_KEY='sk-ant-MANAGEDSECRET'",
        "export VOYAGE_API_KEY=pa-EXPORTEDSECRET",
        "DATABASE_URL='postgresql://dewey:DBSECRET@db:5432/dewey'",
        "POSTGRES_PASSWORD='9f8e7d6c5b4a39281706'",       # docs tell Compose users to keep it here
        "WORDPRESS_APP_PASSWORD='abcd efgh ijkl mnop'",
        "GHOST_CONTENT_API_KEY='22444f78447824223cefc48062'",
        "SLACK_WEBHOOK='https://hooks.slack.example/services/T0/B0/HOOKSECRET'",  # a secret with an innocent name
        "EMBED_MODEL='voyage-4'",
        "",
    ])
    SECRETS = ["MANAGEDSECRET", "EXPORTEDSECRET", "DBSECRET", "9f8e7d6c5b4a39281706", "abcd efgh ijkl mnop",
               "22444f78447824223cefc48062", "HOOKSECRET"]

    def summary(self):
        """The text main() prints under Summary, after the same read_env -> render_env -> redact steps."""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp, ".env")
            path.write_text(self.OLD_ENV, encoding="utf-8")
            current = install.read_env(path)
        managed = {k for _, keys in install.SECTIONS for k in keys}
        values = {"DEWEY_STACK": "anthropic", "ANTHROPIC_API_KEY": current["ANTHROPIC_API_KEY"],
                  "VOYAGE_API_KEY": current["VOYAGE_API_KEY"], "DATABASE_URL": current["DATABASE_URL"],
                  "EMBEDDING_PROVIDER": "voyage", "RERANK_PROVIDER": "voyage"}
        content = install.render_env(values, {k: v for k, v in current.items() if k not in managed})
        return content, install.redact(content)

    def test_no_secret_is_printed(self):
        content, shown = self.summary()
        for secret in self.SECRETS:
            self.assertIn(secret, content)  # still written to .env
            self.assertNotIn(secret, shown)
        self.assertIn("POSTGRES_PASSWORD='********'", shown)
        self.assertIn("ANTHROPIC_API_KEY='********'", shown)
        self.assertIn("DEWEY_STACK='anthropic'", shown)  # managed settings stay readable

    def test_export_prefix_is_read_as_the_key(self):
        # `export VOYAGE_API_KEY=...` (as python-dotenv accepts) is the managed key, offered as the default.
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp, ".env")
            path.write_text(self.OLD_ENV, encoding="utf-8")
            current = install.read_env(path)
        self.assertEqual(current["VOYAGE_API_KEY"], "pa-EXPORTEDSECRET")
        self.assertNotIn("export VOYAGE_API_KEY", current)
        self.assertEqual(install.env_key("exported_value"), "exported_value")
        self.assertEqual(install.redact("export VOYAGE_API_KEY='x'"), "export VOYAGE_API_KEY='********'")


class _ModelServer(BaseHTTPRequestHandler):
    """Answers /v1/models; /moved/... redirects to redirect_base + the rest of the path."""
    redirect_base = None
    seen = None

    def do_GET(self):
        type(self).seen.append((self.server.server_address[1], self.path, self.headers.get("Authorization")))
        if self.path.startswith("/moved/"):
            self.send_response(302)
            self.send_header("Location", self.redirect_base + self.path[len("/moved"):])
            self.end_headers()
            return
        body = json.dumps({"data": [{"id": "qwen3:8b"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ModelServerCheckTests(unittest.TestCase):
    KEY = "sk-or-v1-SECRETKEY"

    def setUp(self):
        _ModelServer.seen = []
        self.servers = []
        for _ in range(2):
            server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelServer)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.servers.append(server)
        self.first, self.second = (s.server_address for s in self.servers)

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def check(self, base_url):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            install.check_services({"DEWEY_STACK": "open", "OPEN_LLM_BASE_URL": base_url,
                                    "OPEN_LLM_API_KEY": self.KEY, "OPEN_LLM_MODEL": "qwen3:8b"})
        return out.getvalue()

    def test_key_is_not_carried_to_another_origin_on_redirect(self):
        # urllib's default redirect handler copied Authorization to wherever the server pointed.
        _ModelServer.redirect_base = f"http://127.0.0.1:{self.second[1]}"
        shown = self.check(f"http://127.0.0.1:{self.first[1]}/moved/v1")
        self.assertIn("ok, 'qwen3:8b' is available", shown)
        self.assertEqual(_ModelServer.seen, [(self.first[1], "/moved/v1/models", f"Bearer {self.KEY}"),
                                             (self.second[1], "/v1/models", None)])

    def test_key_kept_on_a_same_origin_redirect(self):
        _ModelServer.redirect_base = f"http://127.0.0.1:{self.first[1]}"
        self.check(f"http://127.0.0.1:{self.first[1]}/moved/v1")
        self.assertEqual([auth for _, _, auth in _ModelServer.seen], [f"Bearer {self.KEY}"] * 2)

    def test_key_never_sent_over_plain_http_to_another_machine(self):
        with mock.patch.object(install, "http_get_json", side_effect=AssertionError("request made")):
            shown = self.check("http://models.newsroom.example/v1")
        self.assertIn("NOT checked", shown)
        self.assertIn("unencrypted", shown)
        self.assertNotIn(self.KEY, shown)
        self.assertTrue(install.sends_key_in_clear("http://openrouter.ai/api/v1"))
        for url in ("https://openrouter.ai/api/v1", "http://localhost:11434/v1", "http://127.0.0.1:8000/v1",
                    "http://[::1]:8000/v1"):
            self.assertFalse(install.sends_key_in_clear(url), url)

    def test_wizard_warns_when_a_key_would_go_over_http(self):
        answers = iter(["http://models.newsroom.example/v1", "qwen3:8b", self.KEY])
        ask = lambda *a, **k: next(answers)  # noqa: E731
        out = io.StringIO()
        with mock.patch("builtins.input", ask), mock.patch.object(install.getpass, "getpass", ask), \
                mock.patch.object(install.shutil, "which", return_value="/usr/bin/ollama"), \
                contextlib.redirect_stdout(out):
            values = install.ask_open_llm({}, interactive=False)
        self.assertEqual(values["OPEN_LLM_API_KEY"], self.KEY)
        self.assertIn("unencrypted", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class UnclaimedValueTests(unittest.TestCase):
    """A value the wizard produces that no SECTIONS entry claims must still reach .env.

    POSTGRES_PASSWORD is the case: it is deliberately unmanaged, because the docs tell
    Compose users to keep it in .env themselves and a managed key is dropped on a run
    that doesn't set one. The Docker step generates it, so it has to be written anyway.
    """

    DB_URL = "postgresql://dewey:pw@127.0.0.1:5432/dewey"

    def test_a_generated_password_is_written_and_masked_in_the_summary(self):
        content = install.render_env(
            {"DEWEY_STACK": "anthropic", "DATABASE_URL": self.DB_URL,
             "POSTGRES_PASSWORD": "abc123generated"}, {})
        self.assertIn("POSTGRES_PASSWORD='abc123generated'", content)
        self.assertIn("POSTGRES_PASSWORD='********'", install.redact(content))

    def test_an_existing_password_survives_a_run_that_does_not_set_one(self):
        content = install.render_env({"DEWEY_STACK": "anthropic"}, {"POSTGRES_PASSWORD": "kept-value"})
        self.assertIn("POSTGRES_PASSWORD='kept-value'", content)

    def test_a_produced_value_replaces_the_previous_one_exactly_once(self):
        content = install.render_env({"POSTGRES_PASSWORD": "new"}, {"POSTGRES_PASSWORD": "old"})
        self.assertEqual(content.count("POSTGRES_PASSWORD="), 1, content)
        self.assertIn("POSTGRES_PASSWORD='new'", content)

    def test_managed_keys_still_go_in_their_own_section(self):
        content = install.render_env({"DEWEY_STACK": "anthropic", "DATABASE_URL": self.DB_URL}, {})
        self.assertNotIn(install.EXTRA_HEADER, content)

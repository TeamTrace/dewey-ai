"""
Tests for install.py's .env handling (what the Summary and --dry-run print), its
credential check against the chat model server and its check of the database's
pgvector version. No network and no database: the model servers are fake ones on
127.0.0.1, and the Postgres driver is a stand-in. One more test runs the Postgres
check for real, only when DEWEY_TEST_DATABASE_URL names a throwaway database
(see tests/providers/test_pgvector_store.py).
"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get("DEWEY_TEST_DATABASE_URL", "").strip()


def load_install():
    spec = importlib.util.spec_from_file_location("dewey_install_env", REPO / "install.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


install = load_install()


class SummaryMaskingTests(unittest.TestCase):
    """docs/INSTALL.md promises the dry run prints the .env 'with secrets masked'."""

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


def postgres_result(shown):
    """The Postgres result in what check_services() printed: its line and the indented lines
    under it, whatever the other checks printed before or after."""
    lines = shown[shown.index("  Postgres: "):].splitlines(keepends=True)
    count = 1
    while count < len(lines) and lines[count].startswith("      "):
        count += 1
    return "".join(lines[:count])


class FakePostgres:
    """Stands in for the psycopg module. Its one database answers the check's query from
    `versions` ({"default_version": ..., "installed_version": ...}); None means the server has
    no pgvector at all. Columns come back in the order the query names them, as from a real
    database, so a query that leaves one out gets a shorter row."""

    def __init__(self, versions=None, error=None):
        self.versions, self.error, self.queries = versions, error, []

    def connect(self, url, **kwargs):
        if self.error:
            raise self.error
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.queries.append(sql)
        return self

    def fetchone(self):
        if self.versions is None:
            return None
        columns = re.search(r"SELECT (.*?) FROM pg_available_extensions", self.queries[-1]).group(1).split(",")
        return tuple(self.versions[column.strip()] for column in columns)


class PostgresCheckTests(unittest.TestCase):
    """check_services() judges the pgvector version Dewey will get: the one in the database the
    URL names or, before the extension exists there, the one the server would give it. Older
    than 0.8 is flagged with what to do; the check never stops the wizard. The driver is a
    stand-in and the Claude check is kept from running, so nothing here leaves the machine."""

    VALUES = {"DEWEY_STACK": "anthropic", "DATABASE_URL": "postgresql://dewey:DBSECRET@127.0.0.1:5432/dewey"}
    OLD = "older than 0.8: date and byline searches can miss stories"
    UPDATE = "ALTER EXTENSION vector UPDATE;"
    DOCS = "'Upgrade pgvector' in docs/STACKS.md"

    def check(self, postgres):
        """What check_services() prints about Postgres: its line and the lines under it."""
        module = None if postgres is None else types.SimpleNamespace(connect=postgres.connect)
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as empty, \
                mock.patch.dict(os.environ, {"ANTHROPIC_CONFIG_DIR": empty}, clear=True), \
                mock.patch.dict(sys.modules, {"psycopg": module, "anthropic": None}), \
                contextlib.redirect_stdout(out):
            returned = install.check_services(dict(self.VALUES))
        shown = out.getvalue()
        self.assertIsNone(returned)  # a result to read, never a reason to stop the wizard
        self.assertNotIn("DBSECRET", shown)  # the URL's password (the driver here is a stand-in)
        return postgres_result(shown)

    def database(self, installed, default):
        return FakePostgres({"installed_version": installed, "default_version": default})

    def test_an_old_pgvector_is_flagged_with_the_upgrade_steps(self):
        shown = self.check(self.database(installed="0.6.0", default="0.6.0"))
        # The guides in docs/ quote these first words, so that a reader can search for them.
        self.assertEqual(shown.splitlines()[0], "  Postgres: connected, but pgvector 0.6.0 is older than 0.8: "
                                                "date and byline searches can miss stories")
        for words in ("install pgvector 0.8 or newer on the database server", "as a Postgres superuser",
                      self.UPDATE, self.DOCS):
            self.assertIn(words, shown)
        for words in ("FAILED", "ok,", "works, but", "already has"):
            self.assertNotIn(words, shown)
        # Every line under the result is indented to sit beneath it, down to the pointer to the guide.
        self.assertEqual(shown.splitlines()[-1], f"      Step by step: {self.DOCS}.")

    def test_the_version_in_this_database_is_judged_not_the_one_on_the_server(self):
        # Packages upgraded, ALTER EXTENSION never run: Dewey still gets 0.6.0 at every search.
        shown = self.check(self.database(installed="0.6.0", default="0.8.7"))
        self.assertTrue(shown.startswith(f"  Postgres: connected, but pgvector 0.6.0 is {self.OLD}\n"), shown)
        self.assertIn("already has pgvector 0.8.7", shown)
        self.assertIn(self.UPDATE, shown)
        self.assertIn(self.DOCS, shown)
        self.assertNotIn("nstall pgvector", shown)  # nothing left to install: only the update
        # The other way round is fine: what this database has is new enough.
        self.assertEqual(self.check(self.database(installed="0.8.0", default="0.8.7")),
                         "  Postgres: ok, pgvector 0.8.0 available\n")

    def test_before_the_extension_exists_the_version_it_would_get_is_judged(self):
        shown = self.check(self.database(installed=None, default="0.6.0"))
        self.assertTrue(shown.startswith(f"  Postgres: connected, but pgvector 0.6.0 is {self.OLD}\n"), shown)
        self.assertIn("nstall pgvector 0.8 or newer on the database server", shown)
        self.assertIn(self.DOCS, shown)
        self.assertNotIn(self.UPDATE, shown)  # nothing in this database to update yet
        for installed, default in ((None, "0.8.7"), ("0.10.0", "0.10.0"), ("0.8", "0.8.7")):
            with self.subTest(installed=installed, default=default):
                self.assertEqual(self.check(self.database(installed, default)),
                                 f"  Postgres: ok, pgvector {installed or default} available\n")

    def test_a_version_the_check_cannot_read_is_flagged_and_never_repeated(self):
        # What the database reports reaches the terminal only as numbers.
        shown = self.check(self.database(installed="zzz\x1b[31m", default="zzz\x1b[31m"))
        self.assertTrue(shown.startswith("  Postgres: connected, but pgvector's version can't be read, so Dewey "
                                         f"treats it as {self.OLD}\n"), shown)
        self.assertIn(self.DOCS, shown)
        shown += self.check(self.database(installed="0.6.0\x1b[31mRED", default="0.8.7\x1b[31mRED"))
        self.assertIn(f"  Postgres: connected, but pgvector 0.6.0 is {self.OLD}\n", shown)
        self.assertIn("already has pgvector 0.8.7,", shown)
        for raw in ("zzz", "\x1b", "RED", "ok,"):
            self.assertNotIn(raw, shown)
        self.assertEqual(self.check(self.database(installed="0.8.7\x1b[31mRED", default="0.8.7")),
                         "  Postgres: ok, pgvector 0.8.7 available\n")

    def test_nothing_is_guessed_about_a_version_the_check_cannot_read(self):
        # The lines under the result say only what the check knows. It can't compare a version it
        # can't read with the server's, so that version gets both steps whatever the server has:
        # never "The database server already has X, but this database hasn't been updated to it".
        unreadable = ("  Postgres: connected, but pgvector's version can't be read, so Dewey "
                      f"treats it as {self.OLD}\n")
        in_database = {}
        for default in ("0.8.7", "0.6.0", "zzz", None):
            with self.subTest(installed="zzz", default=default):
                shown = in_database[default] = self.check(self.database(installed="zzz", default=default))
                self.assertTrue(shown.startswith(unreadable), shown)
                for words in ("install pgvector 0.8 or newer on the database server", "as a Postgres superuser",
                              self.UPDATE, self.DOCS):
                    self.assertIn(words, shown)
                for words in ("already has", "hasn't been", "0.8.7", "0.6.0", "zzz"):
                    self.assertNotIn(words, shown)
        with self.subTest("the same lines, whatever the server has"):
            self.assertEqual(len(set(in_database.values())), 1, in_database)
        # Before the extension exists in this database there is nothing in it to update, as for a
        # version the check can read. But the first line has named no version here, so the words
        # "that is the version the server would give it" would point at nothing.
        for default in ("zzz", None):
            with self.subTest(installed=None, default=default):
                shown = self.check(self.database(installed=None, default=default))
                self.assertTrue(shown.startswith(unreadable), shown)
                for words in ("pgvector isn't turned on in this database yet.\n",
                              "Install pgvector 0.8 or newer on the database server before you index.", self.DOCS):
                    self.assertIn(words, shown)
                for words in (self.UPDATE, "that is the version", "zzz"):
                    self.assertNotIn(words, shown)
        # After a first line that names the version, those words stay.
        self.assertIn("pgvector isn't turned on in this database yet; that is the version the server would give it.\n",
                      self.check(self.database(installed=None, default="0.6.0")))

    def test_the_guides_quote_the_result_as_it_is_printed(self):
        # A reader finds the right troubleshooting row by searching for the words on their screen,
        # and the result sends them to a section by its title.
        shown = self.check(self.database(installed="0.6.0", default="0.6.0"))
        quoted = shown.splitlines()[0].strip().rsplit(": ", 1)[0]
        self.assertEqual(quoted, "Postgres: connected, but pgvector 0.6.0 is older than 0.8")
        for guide in ("INSTALL.md", "STACKS.md"):
            text = (REPO / "docs" / guide).read_text(encoding="utf-8")
            self.assertTrue(f"`{quoted}`" in text, f"docs/{guide} no longer quotes {quoted!r}")
        self.assertIn(self.DOCS, shown)
        stacks = (REPO / "docs" / "STACKS.md").read_text(encoding="utf-8").splitlines()
        self.assertIn("Upgrade pgvector", [line.lstrip("#").strip() for line in stacks if line.startswith("#")],
                      "docs/STACKS.md has no section with that title, which the result names")

    def test_other_results_read_as_before(self):
        self.assertEqual(self.check(FakePostgres(versions=None)),
                         "  Postgres: connected, but the pgvector extension is NOT installed on this server\n")
        self.assertEqual(self.check(FakePostgres(error=RuntimeError("no route to host"))),
                         "  Postgres: FAILED: RuntimeError: no route to host\n")
        self.assertEqual(self.check(None), "  Postgres: skipped (install requirements first)\n")


@unittest.skipUnless(DATABASE_URL, "set DEWEY_TEST_DATABASE_URL to a throwaway Postgres with pgvector to run")
class RealPostgresCheckTests(unittest.TestCase):
    """The same check with the real psycopg, against a real database. It only reads."""

    def test_the_result_matches_the_pgvector_this_database_reports(self):
        # Written to pass whichever pgvector the database has: the expected line is worked out
        # from what the database itself reports.
        try:
            import psycopg
        except ImportError as e:
            self.skipTest(f"needs the packages in requirements.txt ({e})")
        with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
            row = conn.execute("SELECT default_version, installed_version FROM pg_available_extensions "
                               "WHERE name = 'vector'").fetchone()
        # Kept: libpq's own settings (PGPASSWORD, PGSSLMODE...), which may be how this database is reached.
        env = {name: value for name, value in os.environ.items() if name.startswith("PG")}
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as empty, \
                mock.patch.dict(os.environ, {**env, "ANTHROPIC_CONFIG_DIR": empty}, clear=True), \
                mock.patch.dict(sys.modules, {"anthropic": None}), \
                contextlib.redirect_stdout(out):
            install.check_services({"DEWEY_STACK": "anthropic", "DATABASE_URL": DATABASE_URL})
        shown = postgres_result(out.getvalue())
        if row is None:
            self.assertEqual(shown, "  Postgres: connected, but the pgvector extension is NOT installed on this server\n")
            return
        default, installed = row
        judged = default if installed is None else installed
        # A real pgvector reports plain numbers such as 0.8.7, so this doesn't lean on the wizard's own parser.
        if tuple(int(n) for n in judged.split(".")[:2]) >= (0, 8):
            self.assertEqual(shown, f"  Postgres: ok, pgvector {judged} available\n")
        else:
            self.assertTrue(shown.startswith(f"  Postgres: connected, but pgvector {judged} is older than 0.8: "
                                             "date and byline searches can miss stories\n"), shown)
            self.assertIn("'Upgrade pgvector' in docs/STACKS.md", shown)
            # The update is offered only when this database has the extension to update.
            self.assertEqual("ALTER EXTENSION vector UPDATE;" in shown, installed is not None)


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

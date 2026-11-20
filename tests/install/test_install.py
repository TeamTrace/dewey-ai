"""
Tests for install.py's "Get your archive into Dewey" step.

The wizard is driven through a pseudo-terminal with pexpect, the way a person
uses it (getpass reads from a real terminal), against a copy of install.py and
the importers in a temporary folder, so the repository's own .env and data/
are never touched. Those tests are skipped when pexpect isn't installed
(`pip install pexpect`); the unit tests at the top run everywhere.
No network: the WordPress test talks to a fake site on 127.0.0.1.
"""
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "app"))

import argparse  # noqa: E402
import base64  # noqa: E402
import importlib  # noqa: E402
import importlib.util  # noqa: E402
import io  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

import import_archive  # noqa: E402
from importers import IMPORTERS, common, drupal, ghost, wordpress_api  # noqa: E402
from tests.importers import test_drupal, test_ghost  # noqa: E402
from tests.importers.test_wordpress_api import APP_PASSWORD, APP_USER, FakeWordPress  # noqa: E402

try:
    import pexpect
except ImportError:  # pragma: no cover
    pexpect = None

FIXTURES = REPO / "tests" / "importers" / "fixtures"
CSV_FIXTURE = FIXTURES / "csv_import" / "articles.csv"            # 6 importable articles
US_DATES_CSV = FIXTURES / "csv_import" / "excel_us_bom.csv"       # stops until --date-format is given
WXR_FIXTURE = FIXTURES / "wordpress_export" / "ledger.WordPress.2024-05-01.xml"
GHOST_KEY = "0123456789abcdef0123456789"  # 26 hex digits, the shape of a Content API key
DRUPAL_PASSWORD = "drupal-pass-Zq81x"


def load_install():
    spec = importlib.util.spec_from_file_location("dewey_install", REPO / "install.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


install = load_install()


def typed(*answers):
    """Patch input() and getpass() to return these answers in order (both share one queue)."""
    queue = list(answers)

    def answer(prompt=""):
        if not queue:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        return queue.pop(0)

    return mock.patch.multiple("builtins", input=answer), mock.patch.object(install.getpass, "getpass", answer)


class ImportStepUnitTests(unittest.TestCase):
    """install.py's import helpers, without a terminal."""

    def run_questions(self, source, *answers, prev=None):
        patch_input, patch_getpass = typed(*answers)
        with patch_input, patch_getpass, mock.patch.object(install, "say"):
            return install.PLAN_QUESTIONS[source](prev)

    def test_sources_are_the_importers(self):
        names = [importlib.import_module(f"importers.{m}").NAME for m in IMPORTERS]
        self.assertEqual([v for v, _ in install.IMPORT_SOURCES], names + ["json", "skip"])
        self.assertEqual(sorted(install.PLAN_QUESTIONS), sorted(names))
        self.assertEqual(sorted(install.EXTRA_OPTION_EXAMPLES), sorted(names))

    def test_env_names_are_the_ones_the_importers_read(self):
        self.assertEqual(install.IMPORTER_ENV["wordpress-api"],
                         (wordpress_api.USER_ENV, wordpress_api.PASSWORD_ENV))
        self.assertEqual(install.IMPORTER_ENV["drupal"], (drupal.USER_ENV, drupal.PASSWORD_ENV))
        self.assertEqual(install.IMPORTER_ENV["ghost"], (ghost.KEY_ENV,))
        self.assertEqual(install.SECRET_PARAMS, common.SENSITIVE_PARAMS)

    def test_every_secret_flag_of_every_importer_is_blocked(self):
        parser, _ = import_archive.build_parser()
        subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        flags = {s for p in subparsers.choices.values() for a in p._actions for s in a.option_strings}
        secret_like = {f for f in flags if re.search(r"(password|key|token|secret)$", f)}  # not --password-col
        self.assertTrue(secret_like)
        self.assertLessEqual(secret_like, set(install.SECRET_FLAGS))
        self.assertLessEqual(set(install.SECRET_FLAGS) | set(install.MANAGED_FLAGS), flags)

    def test_wordpress_password_goes_to_the_environment_only(self):
        plan = self.run_questions("wordpress-api", "https://news.example.org", "y", APP_USER, APP_PASSWORD)
        cmd = install.importer_command(plan)
        self.assertEqual(cmd, ["python", "app/import_archive.py", "wordpress-api", "--site", "https://news.example.org"])
        self.assertEqual(plan.env, {"WORDPRESS_USER": APP_USER, "WORDPRESS_APP_PASSWORD": APP_PASSWORD})
        shown = "\n".join(plan.env_lines + [install.shell_join(cmd)])
        self.assertNotIn(APP_PASSWORD, shown)
        self.assertIn("read -rs WORDPRESS_APP_PASSWORD", shown)
        self.assertEqual(install.importer_env(plan)["WORDPRESS_APP_PASSWORD"], APP_PASSWORD)

    def test_no_login_means_no_credentials(self):
        plan = self.run_questions("wordpress-api", "news.example.org", "n")
        self.assertEqual(plan.args, ["--site", "https://news.example.org"])
        self.assertEqual((plan.env, plan.env_lines), ({}, []))

    def test_drupal_and_ghost_secrets(self):
        plan = self.run_questions("drupal", "https://www.example.org", "y", "dewey-reader", DRUPAL_PASSWORD)
        self.assertEqual(plan.env, {"DRUPAL_USER": "dewey-reader", "DRUPAL_PASSWORD": DRUPAL_PASSWORD})
        self.assertNotIn(DRUPAL_PASSWORD, " ".join(install.importer_command(plan) + plan.env_lines))
        plan = self.run_questions("ghost", "https://example.ghost.io", GHOST_KEY)
        self.assertEqual(plan.env, {"GHOST_CONTENT_API_KEY": GHOST_KEY})
        self.assertNotIn(GHOST_KEY, " ".join(install.importer_command(plan) + plan.env_lines))
        self.assertIsNone(self.run_questions("ghost", "https://example.ghost.io", ""))  # no key: no import

    def test_ghost_key_pasted_in_the_url_is_moved_out_of_it(self):
        plan = self.run_questions("ghost", f"https://example.ghost.io/ghost/api/content/posts/?key={GHOST_KEY}&limit=5",
                                  "")  # Enter keeps the key taken from the address
        self.assertEqual(plan.env, {"GHOST_CONTENT_API_KEY": GHOST_KEY})
        self.assertNotIn(GHOST_KEY, " ".join(install.importer_command(plan)))
        self.assertIn("limit=5", plan.args[1])

    def test_addresses_with_logins_or_tokens_are_refused(self):
        for raw in ("https://editor:hunter2@news.example.org", "https://news.example.org/feed/?token=abc123",
                    "not a url", "ftp://news.example.org"):
            url, key, problem = install.clean_url(raw)
            self.assertTrue(problem, raw)
            self.assertEqual((url, key), ("", ""))
        self.assertEqual(install.clean_url("www.example.com/feed/")[0], "https://www.example.com/feed/")

    def test_feed_url_or_saved_file(self):
        plan = self.run_questions("feed", "www.example.com/feed/")
        self.assertEqual(plan.args, ["--url", "https://www.example.com/feed/"])
        plan = self.run_questions("feed", "./missing-feed.xml", "https://editor:pw@example.com/feed/",
                                  str(FIXTURES / "feed" / "newsroom_rss.xml"))  # two refused, then a file
        self.assertEqual(plan.args, ["--file", str(FIXTURES / "feed" / "newsroom_rss.xml")])
        self.assertIsNone(self.run_questions("feed", ""))

    def test_retry_offers_previous_answers_and_secret(self):
        first = self.run_questions("wordpress-api", "https://news.example.org", "y", APP_USER, APP_PASSWORD)
        again = self.run_questions("wordpress-api", "", "", "", "", prev=first)  # Enter everywhere keeps them
        self.assertEqual(again.args, first.args)
        self.assertEqual(again.env, first.env)

    def test_extra_options(self):
        self.assertIsNone(install.extra_options_problem(["--date-format", "%m/%d/%Y", "--post-type", "story"]))
        for tokens in (["--app-password", "x"], ["--app-pass=x"], ["--key=abc"], ["--password", "x"],
                       ["--content-api-key", "x"], ["--limit", "5"], ["--out-dir", "/tmp"], ["--prefix=p"]):
            self.assertTrue(install.extra_options_problem(tokens), tokens)
        prev = install.ImportPlan("csv", ["--file", "a.csv"])
        plan = install.ImportPlan("csv", ["--file", "a.csv"])
        patch_input, patch_getpass = typed("--key abc", "--date-format '%m/%d/%Y %I:%M %p'")
        with patch_input, patch_getpass, mock.patch.object(install, "say"):
            install.ask_extra_options(plan, prev)
        self.assertEqual(plan.args, ["--file", "a.csv", "--date-format", "%m/%d/%Y %I:%M %p"])

    def test_extra_options_cannot_smuggle_a_secret_in_an_address(self):
        # The address questions refuse these; extra options must not be a way around that, since the
        # importers accept them (Ghost reads ?key= from --url) and the command is printed.
        for tokens in (["--url", f"https://example.ghost.io/?key={GHOST_KEY}"],
                       [f"--url=https://example.ghost.io/ghost/api/content/?limit=5&key={GHOST_KEY}"],
                       ["--api-url", "https://dewey-reader:hunter2@www.example.org/jsonapi"],
                       ["--site", "editor:hunter2@news.example.org"],
                       ["--url", "https://www.example.com/feed/?token=abc123"]):
            self.assertTrue(install.extra_options_problem(tokens), tokens)
        self.assertIsNone(install.extra_options_problem(["--api-root", "https://news.example.org/wp-json/",
                                                         "--base-url", "https://news.example.org/?p=1"]))
        plan = install.ImportPlan("ghost", ["--url", "https://x.ghost.io"])
        patch_input, patch_getpass = typed(f"--url https://x.ghost.io/?key={GHOST_KEY}", "")
        with patch_input, patch_getpass, mock.patch.object(install, "say") as said:
            install.ask_extra_options(plan, install.ImportPlan("ghost", ["--url", "https://x.ghost.io"]))
        self.assertEqual(plan.args, ["--url", "https://x.ghost.io"])
        self.assertNotIn(GHOST_KEY, "\n".join(str(c) for c in said.call_args_list))

    def test_help_is_not_an_import(self):
        # --help exits 0 without importing anything, which the wizard would report as a finished import.
        for tokens in (["-h"], ["--help"], ["--he"]):
            self.assertTrue(install.extra_options_problem(tokens), tokens)

    def test_importer_env_drops_logins_not_given_here(self):
        plan = install.ImportPlan("csv", ["--file", "a.csv"])
        with mock.patch.dict(os.environ, {"WORDPRESS_APP_PASSWORD": "from-shell", "DEWEY_KEEP": "1"}):
            env = install.importer_env(plan)
        self.assertNotIn("WORDPRESS_APP_PASSWORD", env)
        self.assertEqual(env["DEWEY_KEEP"], "1")

    def test_paths_typed_or_dragged_in(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="dewey-install-path-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        target = tmp / "My Export.xml"
        target.write_text("<rss/>")
        for raw in (str(target), f"'{target}'", f'"{target}"', str(target).replace(" ", "\\ ")):
            self.assertEqual(install.path_from_input(raw), target, raw)
        self.assertEqual(install.path_from_input("~/x.csv"), pathlib.Path.home() / "x.csv")
        self.assertTrue(install.path_from_input("relative.csv").is_absolute())


@unittest.skipIf(pexpect is None, "pexpect is not installed (pip install pexpect)")
@unittest.skipIf(os.name == "nt", "pexpect.spawn needs a POSIX pseudo-terminal")
class WizardImportTests(unittest.TestCase):
    """The whole wizard, on the open stack, up to and through the import step."""

    OPEN_STACK = [
        ("Choose a number or name", "open"),
        ("Server URL", ""),
        ("Model name", ""),
        ("Choose a number or name", "local"),    # embeddings
        ("Choose a number or name", "none"),     # reranking
        # "later": no database, so no checks, no indexing, nothing reaches Postgres
        (r"Do you have a Postgres database[\s\S]*?Choose a number or name", "later"),
        ("Newsroom name", ""),
        ("Earliest article date", ""),
        ("Region", "none"),
    ]
    WRITE_ENV = [
        ("Write this to .env", "y"),
        ("Install into the current Python anyway|Install requirements.txt now", "n"),
    ]
    WHERE = r"Where is your archive\?[\s\S]*?Choose a number or name"
    WHAT_NEXT = r"What next\?[\s\S]*?Choose a number or name"

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="dewey-install-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copy2(REPO / "install.py", self.root / "install.py")
        (self.root / "app").mkdir()
        shutil.copy2(REPO / "app" / "import_archive.py", self.root / "app" / "import_archive.py")
        shutil.copytree(REPO / "app" / "importers", self.root / "app" / "importers",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "data").mkdir()
        (self.root / "bin").mkdir()  # PATH: no ollama or docker, whatever this machine has

    def wizard(self, script, *args):
        env = {k: v for k, v in os.environ.items()
               if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")
               and k not in ("WORDPRESS_USER", "WORDPRESS_APP_PASSWORD", "DRUPAL_USER", "DRUPAL_PASSWORD",
                             "GHOST_CONTENT_API_KEY")}
        env.update(PATH=str(self.root / "bin"), NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
                   PYTHONDONTWRITEBYTECODE="1")
        transcript = io.StringIO()
        child = pexpect.spawn(sys.executable, ["install.py", "--no-browser", *args], cwd=str(self.root),
                              env=env, encoding="utf-8", timeout=60, dimensions=(50, 200))
        child.logfile_read = transcript
        try:
            for pattern, answer in script:
                child.expect(pattern)
                child.sendline(answer() if callable(answer) else answer)
            child.expect(pexpect.EOF)
        except (pexpect.TIMEOUT, pexpect.EOF) as e:
            raise AssertionError(f"wizard didn't reach {pattern!r}; output so far:\n{transcript.getvalue()}") from e
        finally:
            child.close()
        return transcript.getvalue(), child.exitstatus

    def data_files(self):
        return sorted(p.name for p in (self.root / "data").glob("*.json"))

    def test_csv_trial_then_full_import(self):
        out, status = self.wizard(self.OPEN_STACK + self.WRITE_ENV + [
            (self.WHERE, "csv"),
            ("Path to the CSV file", str(CSV_FIXTURE)),
            ("Try a small import first", "y"),
            (self.WHAT_NEXT, "full"),
            ("Remove the sample file", "y"),
        ])
        self.assertEqual(status, 0, out)
        self.assertIn(f"app/import_archive.py csv --file {CSV_FIXTURE} --limit 20", out)
        self.assertEqual(out.count("Imported 6 article(s) from csv."), 2, out)  # trial report, full report
        self.assertIn("Column mapping", out)
        self.assertEqual(self.data_files(), ["csv-articles-0001.json"])  # sample file removed
        self.assertIn(f"\n  python app/import_archive.py csv --file {CSV_FIXTURE}\r\n", out)
        self.assertIn("DEWEY_STACK='open'", (self.root / ".env").read_text())

    def test_failed_trial_can_be_retried_with_extra_options(self):
        out, status = self.wizard(self.OPEN_STACK + self.WRITE_ENV + [
            (self.WHERE, "csv"),
            ("Path to the CSV file", str(US_DATES_CSV)),
            ("Try a small import first", "y"),
            (self.WHAT_NEXT, ""),                               # default after a failed trial: change
            ("Path to the CSV file", ""),                       # Enter keeps the file
            ("Extra options", "--limit 5"),                     # refused: the wizard sets --limit
            ("Extra options", "--date-format '%m/%d/%Y %I:%M %p'"),
            ("Try a small import first", "n"),
        ])
        self.assertEqual(status, 0, out)
        self.assertIn("--date-format '%m/%d/%Y %I:%M %p'", out)
        self.assertIn("sets --limit itself", out)
        self.assertIn("Import failed", out)
        self.assertRegex(out, r"Imported \d+ article\(s\) from csv\.")
        self.assertEqual(self.data_files(), ["csv-excel_us_bom-0001.json"])

    def test_wordpress_password_is_never_shown(self):
        sites = []

        def start_site():
            # Started only once the wizard is running: forking while the server's threads run could deadlock.
            sites.append(FakeWordPress())
            self.addCleanup(sites[0].close)
            return sites[0].base

        out, status = self.wizard(self.OPEN_STACK + self.WRITE_ENV + [
            (self.WHERE, "wordpress-api"),
            ("Your site's public address", start_site),
            ("Does the site need a login", "y"),
            ("WordPress username", APP_USER),
            ("Application Password", APP_PASSWORD),
            ("Try a small import first", "y"),
            (self.WHAT_NEXT, "full"),
            ("Remove the sample file", "n"),
        ])
        self.assertEqual(status, 0, out)
        token = base64.b64encode(f"{APP_USER}:{APP_PASSWORD}".encode()).decode()
        for secret in (APP_PASSWORD, token):
            self.assertNotIn(secret, out)
            for path in self.root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(secret, path.read_text(encoding="utf-8", errors="replace"), path)
        # The password reached the importer through its environment: the site saw the login.
        self.assertIn(f"Basic {token}", {r["auth"] for r in sites[0].requests})
        self.assertIn("Signed in to WordPress", out)
        self.assertIn("export WORDPRESS_USER=editor", out)
        self.assertIn("read -rs WORDPRESS_APP_PASSWORD && export WORDPRESS_APP_PASSWORD", out)
        files = self.data_files()
        self.assertTrue(any("-sample-" in f for f in files), files)  # kept, as answered
        self.assertTrue(any(f.startswith("wordpress-") and "-sample-" not in f for f in files), files)

    def start_fake(self, factory, sites):
        """A callable answer: starts a fake site once the wizard runs (forking under its threads could deadlock)."""
        def start():
            sites.append(factory())
            self.addCleanup(sites[-1].close)
            return sites[-1].base
        return start

    def assert_nowhere(self, secret, out):
        self.assertNotIn(secret, out)
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret, path.read_text(encoding="utf-8", errors="replace"), path)

    def test_ghost_key_reaches_the_site_but_is_never_shown(self):
        sites = []
        out, status = self.wizard(self.OPEN_STACK + self.WRITE_ENV + [
            (self.WHERE, "ghost"),
            (r"API URL \(or", self.start_fake(test_ghost.FakeGhost, sites)),
            (r"Content API key \(hidden\)", test_ghost.KEY),
            ("Try a small import first", "y"),
            (self.WHAT_NEXT, "full"),
            ("Remove the sample file", "y"),
        ])
        self.assertEqual(status, 0, out)
        self.assert_nowhere(test_ghost.KEY, out)
        self.assertIn(test_ghost.KEY, {r["query"].get("key") for r in sites[0].requests})
        self.assertRegex(out, r"Imported \d+ article\(s\) from ghost\.")
        self.assertIn("read -rs GHOST_CONTENT_API_KEY && export GHOST_CONTENT_API_KEY", out)
        files = self.data_files()
        self.assertTrue(files and all(f.startswith("ghost-") and "-sample-" not in f for f in files), files)

    def test_drupal_password_reaches_the_site_but_is_never_shown(self):
        sites = []
        out, status = self.wizard(self.OPEN_STACK + self.WRITE_ENV + [
            (self.WHERE, "drupal"),
            ("Your site's public address", self.start_fake(lambda: test_drupal.FakeDrupal(block_anonymous=True),
                                                           sites)),
            ("Does it need a login", "y"),
            ("Drupal username", test_drupal.USERNAME),
            ("Password for that account", test_drupal.PASSWORD),
            ("Try a small import first", "n"),
        ])
        self.assertEqual(status, 0, out)
        token = base64.b64encode(f"{test_drupal.USERNAME}:{test_drupal.PASSWORD}".encode()).decode()
        for secret in (test_drupal.PASSWORD, token):
            self.assert_nowhere(secret, out)
        self.assertIn(test_drupal.USERNAME, {r["user"] for r in sites[0].requests})  # signed in
        self.assertRegex(out, r"Imported \d+ article\(s\) from drupal\.")
        self.assertIn("read -rs DRUPAL_PASSWORD && export DRUPAL_PASSWORD", out)

    def test_dry_run_prints_the_commands_and_imports_nothing(self):
        out, status = self.wizard(self.OPEN_STACK + [
            (self.WHERE, "wordpress-export"),
            ("Path to the export file", str(WXR_FIXTURE)),
        ], "--dry-run")
        self.assertEqual(status, 0, out)
        self.assertIn("nothing was written", out)
        self.assertIn("nothing is imported", out)
        self.assertIn(f"python app/import_archive.py wordpress-export --file {WXR_FIXTURE} --limit 20", out)
        self.assertNotIn("Imported ", out)
        self.assertNotIn("  $ ", out)  # no command was run
        self.assertEqual(self.data_files(), [])
        self.assertFalse((self.root / ".env").exists())

    def test_dry_run_with_a_key_never_shows_it(self):
        out, status = self.wizard(self.OPEN_STACK + [
            (self.WHERE, "ghost"),
            (r"API URL \(or", "https://example.ghost.io"),
            (r"Content API key \(hidden\)", GHOST_KEY),
        ], "--dry-run")
        self.assertEqual(status, 0, out)
        self.assertNotIn(GHOST_KEY, out)
        self.assertIn("read -rs GHOST_CONTENT_API_KEY", out)
        self.assertIn("python app/import_archive.py ghost --url https://example.ghost.io", out)
        self.assertIn("Beta: tested against sample exports only", out)
        self.assertEqual(self.data_files(), [])

    def test_skip_and_existing_json(self):
        out, status = self.wizard(self.OPEN_STACK + self.WRITE_ENV + [(self.WHERE, "skip")])
        self.assertEqual(status, 0, out)
        self.assertIn("python app/import_archive.py --help", out)
        (self.root / "data" / "mine.json").write_text("[]")
        out, status = self.wizard(self.OPEN_STACK + self.WRITE_ENV + [(self.WHERE, "")])  # default: json
        self.assertEqual(status, 0, out)
        self.assertIn("Using the 1 JSON file(s) already in data/.", out)


if __name__ == "__main__":
    unittest.main()

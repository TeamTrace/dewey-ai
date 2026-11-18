import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import auth
import manage_users

APP = pathlib.Path(__file__).resolve().parents[2] / "app"
PASSWORD = "correct horse battery staple"

try:
    import pexpect
except ImportError:  # pragma: no cover
    pexpect = None


def answers(*values):
    """A prompt function that returns the given values in order."""
    it = iter(values)
    return lambda _prompt="": next(it)


class ManageUsersFunctionsTest(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.path = self.dir / "users.txt"
        self.out = io.StringIO()
        self.err = io.StringIO()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def add(self, name, *passwords, confirm="y"):
        with contextlib.redirect_stderr(self.err):
            manage_users.add_user(self.path, name, prompt=answers(*passwords),
                                  confirm=lambda _p="": confirm, out=self.out)

    def store(self):
        return auth.load_user_store({"DEWEY_USERS_FILE": str(self.path)}, warn=self.fail)

    def assert_no_secrets_printed(self, *passwords):
        printed = self.out.getvalue() + self.err.getvalue()
        for password in passwords:
            self.assertNotIn(password, printed)
        for line in self.path.read_text().splitlines() if self.path.exists() else []:
            if not line.startswith("#"):
                self.assertNotIn(line.partition(":")[2], printed)

    def test_add_creates_private_file_that_dewey_accepts(self):
        self.add("alice", PASSWORD, PASSWORD)
        store = self.store()
        self.assertEqual(store.usernames(), ["alice"])
        self.assertTrue(store.verify("alice", PASSWORD))
        self.assertTrue(self.path.read_text().startswith("# Dewey login accounts"))
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertIn("Added alice", self.out.getvalue())
        self.assertIn("Restart Dewey", self.out.getvalue())
        self.assert_no_secrets_printed(PASSWORD)

    def test_add_second_user_and_replace_password(self):
        self.add("alice", PASSWORD, PASSWORD)
        self.add("bob", "bobs password 123", "bobs password 123")
        self.add("alice", "a brand new password", "a brand new password", confirm="y")
        store = self.store()
        self.assertEqual(store.usernames(), ["alice", "bob"])
        self.assertFalse(store.verify("alice", PASSWORD))
        self.assertTrue(store.verify("alice", "a brand new password"))
        self.assertTrue(store.verify("bob", "bobs password 123"))
        self.assertEqual(sum(1 for line in self.path.read_text().splitlines() if line.startswith("alice:")), 1)
        self.assert_no_secrets_printed(PASSWORD, "a brand new password", "bobs password 123")

    def test_replace_declined_changes_nothing(self):
        self.add("alice", PASSWORD, PASSWORD)
        before = self.path.read_text()
        self.add("alice", confirm="n")  # no password prompts expected
        self.assertEqual(self.path.read_text(), before)
        self.assertIn("Nothing changed", self.out.getvalue())

    def test_password_equal_to_user_name_is_rejected(self):
        name = "newsroom.editor"
        with self.assertRaises(manage_users.UsageError):
            self.add(name, name, "Newsroom.Editor", " newsroom.editor ")
        self.assertIn("can't be the user name", self.err.getvalue())
        self.assertFalse(self.path.exists())

    def test_short_and_mismatched_passwords_are_rejected(self):
        with self.assertRaises(manage_users.UsageError):
            self.add("alice", "short", "x" * 11, "y" * 5)  # three attempts, all too short
        self.assertFalse(self.path.exists())
        with self.assertRaises(manage_users.UsageError):
            self.add("alice", PASSWORD, PASSWORD + "x", PASSWORD, "nope", PASSWORD, "")
        self.assertFalse(self.path.exists())
        self.assertIn("at least 12", self.err.getvalue())
        self.assertIn("don't match", self.err.getvalue())
        self.assert_no_secrets_printed(PASSWORD)

    def test_retry_after_a_bad_attempt(self):
        self.add("alice", "short", PASSWORD, PASSWORD)
        self.assertTrue(self.store().verify("alice", PASSWORD))

    def test_invalid_user_name(self):
        with self.assertRaisesRegex(manage_users.UsageError, "User name"):
            self.add("bad name", PASSWORD, PASSWORD)
        with self.assertRaises(manage_users.UsageError):
            self.add("evil:name", PASSWORD, PASSWORD)

    def test_remove(self):
        self.add("alice", PASSWORD, PASSWORD)
        self.add("bob", "bobs password 123", "bobs password 123")
        manage_users.remove_user(self.path, "alice", out=self.out)
        self.assertEqual(self.store().usernames(), ["bob"])
        with self.assertRaisesRegex(manage_users.UsageError, "No user named"):
            manage_users.remove_user(self.path, "alice", out=self.out)

    def test_edits_keep_comments_and_other_lines(self):
        self.path.write_text("# newsroom data team\n" + f"bob:{auth.hash_password('bobs password 123')}\n"
                             "# end\n")
        self.add("alice", PASSWORD, PASSWORD)
        manage_users.remove_user(self.path, "bob", out=self.out)
        text = self.path.read_text()
        self.assertIn("# newsroom data team", text)
        self.assertIn("# end", text)
        self.assertEqual(self.store().usernames(), ["alice"])

    def test_list_prints_names_only(self):
        manage_users.list_users(self.path, out=self.out)
        self.assertIn("No users file", self.out.getvalue())
        self.add("bob", "bobs password 123", "bobs password 123")
        self.add("alice", PASSWORD, PASSWORD)
        self.out = io.StringIO()
        manage_users.list_users(self.path, out=self.out)
        self.assertEqual(self.out.getvalue().splitlines()[1:], ["  alice", "  bob"])
        self.assert_no_secrets_printed(PASSWORD)

    def test_main_uses_dewey_users_file_and_exit_codes(self):
        with mock.patch.dict(os.environ, {"DEWEY_USERS_FILE": str(self.path)}), \
                contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err):
            self.assertEqual(manage_users.main(["remove", "nobody"]), 1)
            self.assertEqual(manage_users.main(["list"]), 0)
        self.assertIn(str(self.path), self.out.getvalue())
        self.assertIn("Error: No user named", self.err.getvalue())

    def test_directory_in_place_of_file(self):
        self.path.mkdir()
        with self.assertRaisesRegex(manage_users.UsageError, "directory"):
            manage_users.list_users(self.path, out=self.out)


class DotenvAwarenessTest(unittest.TestCase):
    """main.py loads .env before reading DEWEY_USERS / DEWEY_USERS_FILE; this tool
    (standard library only) must not silently edit a file Dewey doesn't read."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.out, self.err = io.StringIO(), io.StringIO()
        self.h = auth.hash_password(PASSWORD)
        clean = {k: v for k, v in os.environ.items() if k not in ("DEWEY_USERS", "DEWEY_USERS_FILE")}
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        self.repo = mock.patch.object(manage_users, "REPO_ROOT", self.root)
        self.repo.start()

    def tearDown(self):
        self.repo.stop()
        self.env.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def run_main(self, *argv):
        with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err):
            return manage_users.main(list(argv))

    def test_users_file_from_dotenv_is_the_default(self):
        other = self.root / "elsewhere" / "accounts.txt"
        other.parent.mkdir()
        other.write_text(f"bob:{self.h}\n")
        (self.root / "users.txt").write_text(f"bob:{self.h}\n")
        (self.root / ".env").write_text(f"# comment\nexport DEWEY_USERS_FILE='{other}'  \n")
        self.assertEqual(self.run_main("remove", "bob"), 0)
        self.assertEqual(auth.parse_users(other.read_text()).records, {})        # the file Dewey reads
        self.assertIn("bob", auth.parse_users((self.root / "users.txt").read_text()).records)
        self.assertIn(str(other), self.out.getvalue())

    def test_shell_setting_wins_over_dotenv_like_python_dotenv(self):
        (self.root / ".env").write_text('DEWEY_USERS_FILE="/nonexistent/from-dotenv.txt"\n')
        shell = self.root / "shell.txt"
        os.environ["DEWEY_USERS_FILE"] = str(shell)
        self.assertEqual(manage_users.default_users_file(), shell)

    def test_warns_when_dewey_users_in_dotenv_overrides_the_file(self):
        (self.root / "users.txt").write_text(f"alice:{self.h}\nbob:{self.h}\n")
        (self.root / ".env").write_text(f"DEWEY_USERS='alice:{self.h},bob:{self.h}'\n")
        self.assertEqual(self.run_main("remove", "bob"), 0)
        printed = self.out.getvalue() + self.err.getvalue()
        self.assertIn("DEWEY_USERS", self.err.getvalue())
        self.assertIn(".env", self.err.getvalue())
        self.assertIn("bob", self.err.getvalue())  # says bob is still listed there
        self.assertNotIn(self.h.split(":")[-1], printed)

    def test_list_warns_when_dewey_users_is_in_use(self):
        (self.root / "users.txt").write_text(f"bob:{self.h}\n")
        os.environ["DEWEY_USERS"] = f"alice:{self.h}"
        self.assertEqual(self.run_main("list"), 0)
        self.assertIn("DEWEY_USERS is set in this shell's environment", self.err.getvalue())

    def test_no_dotenv_no_warning(self):
        (self.root / "users.txt").write_text(f"bob:{self.h}\n")
        self.assertEqual(self.run_main("remove", "bob"), 0)
        self.assertNotIn("DEWEY_USERS", self.err.getvalue())


@unittest.skipIf(pexpect is None or os.name != "posix", "needs pexpect and a POSIX terminal")
class ManageUsersTerminalTest(unittest.TestCase):
    """Runs the real command in a pseudo-terminal, as a person would."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.path = self.dir / "users.txt"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def spawn(self, *args):
        env = {k: v for k, v in os.environ.items() if k not in ("DEWEY_USERS", "DEWEY_USERS_FILE")}
        return pexpect.spawn(sys.executable, [str(APP / "manage_users.py"), "--file", str(self.path), *args],
                             env=env, encoding="utf-8", timeout=30)

    def test_add_list_remove(self):
        child = self.spawn("add", "alice")
        child.expect("New password for alice")
        child.sendline("too short")
        child.expect("Too short")
        child.expect("New password for alice")
        child.sendline(PASSWORD)
        child.expect("Repeat the password")
        child.sendline(PASSWORD)
        child.expect(pexpect.EOF)
        transcript = child.before
        child.close()
        self.assertEqual(child.exitstatus, 0, transcript)
        self.assertNotIn(PASSWORD, transcript)  # hidden input is not echoed
        self.assertTrue(auth.verify_password(PASSWORD, self.path.read_text().splitlines()[-1].partition(":")[2]))

        listing = subprocess.run([sys.executable, str(APP / "manage_users.py"), "--file", str(self.path), "list"],
                                 capture_output=True, text=True, check=True).stdout
        self.assertIn("1 user(s)", listing)
        self.assertIn("  alice", listing)
        self.assertNotIn("scrypt", listing)

        child = self.spawn("add", "alice")
        child.expect(r"already exists. Replace their password\? \[y/N\]")
        child.sendline("n")
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, 0)

        done = subprocess.run([sys.executable, str(APP / "manage_users.py"), "--file", str(self.path),
                               "remove", "alice"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(auth.parse_users(self.path.read_text()).records, {})

    def test_ctrl_c_changes_nothing(self):
        child = self.spawn("add", "alice")
        child.expect("New password for alice")
        child.sendintr()
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, 130)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()

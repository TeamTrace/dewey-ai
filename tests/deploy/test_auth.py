import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import hashlib
import hmac
import os
import re
import tempfile
import unittest
from unittest import mock

import auth

PASSWORD = "correct horse battery staple"


class HashingTest(unittest.TestCase):
    def test_round_trip_and_wrong_password(self):
        encoded = auth.hash_password(PASSWORD)
        self.assertTrue(auth.verify_password(PASSWORD, encoded))
        self.assertFalse(auth.verify_password(PASSWORD + "!", encoded))
        self.assertFalse(auth.verify_password("", encoded))

    def test_stores_algorithm_parameters_salt_and_key(self):
        encoded = auth.hash_password(PASSWORD)
        algorithm, n, r, p, salt, key = encoded.split(":")
        self.assertEqual((algorithm, n, r, p), ("scrypt", str(auth.SCRYPT_N), str(auth.SCRYPT_R), str(auth.SCRYPT_P)))
        decoded = auth.PasswordHash.decode(encoded)
        self.assertEqual(len(decoded.salt), auth.SALT_BYTES)
        self.assertEqual(len(decoded.key), auth.KEY_BYTES)
        self.assertEqual(decoded.encode(), encoded)

    def test_random_salt(self):
        a, b = auth.hash_password(PASSWORD), auth.hash_password(PASSWORD)
        self.assertNotEqual(a, b)
        self.assertNotEqual(auth.PasswordHash.decode(a).salt, auth.PasswordHash.decode(b).salt)

    def test_encoding_is_shell_and_env_safe(self):
        encoded = auth.hash_password(PASSWORD)
        self.assertRegex(encoded, r"^[A-Za-z0-9:_-]+$")  # no $, quotes, spaces, commas, '=' padding
        self.assertNotIn(PASSWORD, encoded)

    def test_matches_stdlib_scrypt(self):
        stored = auth.PasswordHash.decode(auth.hash_password(PASSWORD))
        n, r, p = stored.params
        expected = hashlib.scrypt(PASSWORD.encode(), salt=stored.salt, n=n, r=r, p=p,
                                  maxmem=128 * r * (n + p + 2) + 2 ** 20, dklen=len(stored.key))
        self.assertEqual(expected, stored.key)

    def test_stored_parameters_are_used_not_current_defaults(self):
        # An account hashed with older (cheaper) settings keeps working after the defaults change.
        with mock.patch.object(auth, "SCRYPT_N", 2 ** 10):
            old = auth.hash_password(PASSWORD)
        self.assertEqual(old.split(":")[1], "1024")
        self.assertTrue(auth.verify_password(PASSWORD, old))

    def test_pbkdf2_hashes_are_accepted(self):
        salt = os.urandom(16)
        key = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt, 20_000, dklen=32)
        encoded = auth.PasswordHash("pbkdf2_sha256", (20_000,), salt, key).encode()
        self.assertTrue(auth.verify_password(PASSWORD, encoded))
        self.assertFalse(auth.verify_password("nope nope nope", encoded))

    def test_rejects_bad_hashes(self):
        good = auth.hash_password(PASSWORD)
        _, n, r, p, salt, key = good.split(":")
        bad = {
            "unknown algorithm": f"md5:{salt}:{key}",
            "too few fields": f"scrypt:{n}:{r}:{salt}:{key}",
            "non-numeric cost": f"scrypt:abc:{r}:{p}:{salt}:{key}",
            "n not a power of two": f"scrypt:1000:{r}:{p}:{salt}:{key}",
            "too much memory": f"scrypt:{2 ** 22}:{r}:{p}:{salt}:{key}",
            "bad base64": f"scrypt:{n}:{r}:{p}:{salt}:not*base64",
            "short key": f"scrypt:{n}:{r}:{p}:{salt}:AAAA",
            "pbkdf2 too few iterations": f"pbkdf2_sha256:10:{salt}:{key}",
        }
        for label, text in bad.items():
            with self.subTest(label):
                with self.assertRaises(ValueError) as cm:
                    auth.PasswordHash.decode(text)
                # error messages never echo the hash material
                self.assertNotIn(salt, str(cm.exception))
                self.assertNotIn(key, str(cm.exception))
                self.assertFalse(auth.verify_password(PASSWORD, text))


class UserStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.alice_hash = auth.hash_password(PASSWORD)
        cls.store = auth.UserStore(auth.parse_users(f"alice:{cls.alice_hash}\n").records, "test")

    def test_verify(self):
        self.assertTrue(self.store.verify("alice", PASSWORD))
        self.assertFalse(self.store.verify("alice", "wrong password here"))
        self.assertFalse(self.store.verify("bob", PASSWORD))  # unknown user
        self.assertFalse(self.store.verify("Alice", PASSWORD))  # names are case-sensitive
        self.assertFalse(self.store.verify("alice", PASSWORD * 100))  # over MAX_PASSWORD_LENGTH
        self.assertFalse(self.store.verify(None, PASSWORD))
        self.assertFalse(self.store.verify("alice", None))

    def test_uses_constant_time_compare_for_every_outcome(self):
        cases = [("alice", PASSWORD, True), ("alice", "wrong password here", False),
                 ("bob", PASSWORD, False), ("alice", "x" * (auth.MAX_PASSWORD_LENGTH + 1), False)]
        for user, password, expected in cases:
            with self.subTest(user=user, expected=expected):
                with mock.patch.object(auth.hmac, "compare_digest", wraps=hmac.compare_digest) as spy, \
                        mock.patch.object(auth, "_derive", wraps=auth._derive) as derive:
                    self.assertIs(self.store.verify(user, password), expected)
                self.assertEqual(spy.call_count, 1)
                self.assertEqual(derive.call_count, 1)  # unknown users cost one hash too

    def test_unknown_user_uses_same_cost_parameters(self):
        with mock.patch.object(auth, "_derive", wraps=auth._derive) as derive:
            self.store.verify("bob", PASSWORD)
            self.store.verify("alice", PASSWORD)
        (_, unknown_alg, unknown_params, _, unknown_len), (_, known_alg, known_params, _, known_len) = (
            derive.call_args_list[0].args, derive.call_args_list[1].args)
        self.assertEqual((unknown_alg, unknown_params, unknown_len), (known_alg, known_params, known_len))

    def test_usernames(self):
        self.assertEqual(self.store.usernames(), ["alice"])
        self.assertEqual(len(self.store), 1)


class ParseUsersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = auth.hash_password(PASSWORD)

    def test_ignores_blank_and_comment_lines(self):
        text = f"# Dewey users\n\n   \n  # indented comment\nalice:{self.h}\n\r\n"
        parsed = auth.parse_users(text)
        self.assertEqual(list(parsed.records), ["alice"])
        self.assertEqual(parsed.problems, [])
        self.assertEqual(parsed.entries, 1)

    def test_malformed_lines_are_skipped_without_echoing_them(self):
        secretish = "hunter2hunter2"
        text = "\n".join([
            f"alice:{self.h}",
            secretish,                         # no colon (maybe a password typed by mistake)
            f"bad name:{self.h}",              # space in name
            f":{self.h}",                      # empty name
            "carol:" + secretish,              # not a hash
            f"dave:{self.h},erin:{self.h}",    # commas are not separators in a file
            f"alice:{auth.hash_password('another password')}",  # duplicate
            "﻿zed:" + self.h,             # stray BOM in the middle
        ])
        parsed = auth.parse_users(text)
        self.assertEqual(list(parsed.records), ["alice"])
        self.assertEqual(parsed.entries, 8)
        self.assertEqual(len(parsed.problems), 7)
        joined = "\n".join(parsed.problems)
        self.assertNotIn(secretish, joined)
        self.assertNotIn(self.h.split(":")[-1], joined)
        self.assertIn("line 2", joined)
        self.assertIn("more than once", joined)
        # the first entry wins for duplicates
        self.assertTrue(auth.UserStore(parsed.records, "t").verify("alice", PASSWORD))

    def test_comma_separated_mode_for_env_var(self):
        bob = auth.hash_password("bobs password 123")
        text = f"# Dewey users,alice:{self.h} , bob:{bob},,\n"
        parsed = auth.parse_users(text, commas=True)
        self.assertEqual(sorted(parsed.records), ["alice", "bob"])
        self.assertEqual(parsed.problems, [])

    def test_username_rules(self):
        for name in ["alice", "a", "first.last", "j_doe", "x@newsroom.org", "a-b", "A9"]:
            self.assertTrue(auth.valid_username(name), name)
        for name in ["", ".alice", "-x", "has space", "colon:name", "comma,name", "é", "x" * 65]:
            self.assertFalse(auth.valid_username(name), name)


class LoadUserStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = auth.hash_password(PASSWORD)

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.warnings = []

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def load(self, env):
        return auth.load_user_store(env, repo_root=self.dir, warn=self.warnings.append)

    def test_no_configuration_means_no_store(self):
        self.assertIsNone(self.load({}))

    def test_default_file_in_repo_root(self):
        (self.dir / "users.txt").write_text(f"alice:{self.h}\n")
        store = self.load({})
        self.assertEqual(store.usernames(), ["alice"])
        self.assertIn(str(self.dir / "users.txt"), store.source)

    def test_explicit_file(self):
        path = self.dir / "elsewhere.txt"
        path.write_text(f"alice:{self.h}\n")
        self.assertEqual(self.load({"DEWEY_USERS_FILE": str(path)}).usernames(), ["alice"])

    def test_explicit_missing_file_is_an_error(self):
        with self.assertRaisesRegex(auth.UsersConfigError, "does not exist"):
            self.load({"DEWEY_USERS_FILE": str(self.dir / "missing.txt")})

    def test_directory_is_an_error(self):
        (self.dir / "users.txt").mkdir()
        with self.assertRaisesRegex(auth.UsersConfigError, "directory"):
            self.load({})

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0, "root can read any file")
    def test_unreadable_file_is_an_error(self):
        path = self.dir / "users.txt"
        path.write_text(f"alice:{self.h}\n")
        path.chmod(0)
        with self.assertRaisesRegex(auth.UsersConfigError, "permission denied"):
            self.load({})

    def test_unreadable_file_is_an_error_even_as_root(self):
        with mock.patch.object(pathlib.Path, "read_bytes", side_effect=PermissionError(13, "denied")):
            (self.dir / "users.txt").write_text("x")
            with self.assertRaisesRegex(auth.UsersConfigError, "permission denied"):
                self.load({})

    def test_not_utf8_is_an_error(self):
        (self.dir / "users.txt").write_bytes(b"alice:\xff\xfe\n")
        with self.assertRaisesRegex(auth.UsersConfigError, "UTF-8"):
            self.load({})

    def test_file_with_only_comments_has_no_accounts(self):
        (self.dir / "users.txt").write_text("# nobody yet\n")
        store = self.load({})
        self.assertIsNotNone(store)
        self.assertEqual(len(store), 0)

    def test_file_where_every_entry_is_malformed_is_an_error(self):
        (self.dir / "users.txt").write_text("alice:garbage\nbob\n")
        with self.assertRaisesRegex(auth.UsersConfigError, "none of them is valid"):
            self.load({})
        self.assertEqual(len(self.warnings), 2)

    def test_partly_malformed_file_warns_and_keeps_valid_accounts(self):
        (self.dir / "users.txt").write_text(f"alice:{self.h}\nbob:garbage\n")
        store = self.load({})
        self.assertEqual(store.usernames(), ["alice"])
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("line 2", self.warnings[0])
        self.assertNotIn("garbage", self.warnings[0])

    def test_env_var_wins_over_default_file(self):
        (self.dir / "users.txt").write_text(f"carol:{self.h}\n")
        bob = auth.hash_password("bobs password 123")
        store = self.load({"DEWEY_USERS": f"alice:{self.h},bob:{bob}"})
        self.assertEqual(store.usernames(), ["alice", "bob"])
        self.assertIn("DEWEY_USERS", store.source)
        self.assertTrue(any("ignored" in w for w in self.warnings))

    def test_env_var_and_explicit_file_together_is_an_error(self):
        with self.assertRaisesRegex(auth.UsersConfigError, "not both"):
            self.load({"DEWEY_USERS": f"alice:{self.h}", "DEWEY_USERS_FILE": str(self.dir / "u.txt")})

    def test_env_var_with_no_valid_entries_is_an_error(self):
        with self.assertRaises(auth.UsersConfigError):
            self.load({"DEWEY_USERS": "alice:not-a-hash"})

    def test_blank_env_var_is_ignored(self):
        self.assertIsNone(self.load({"DEWEY_USERS": "   "}))


class WriteAtomicTest(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    @unittest.skipIf(os.name != "posix", "POSIX permissions")
    def test_new_file_is_private_and_existing_mode_is_kept(self):
        path = self.dir / "users.txt"
        auth.write_text_atomic(path, "a\n")
        self.assertEqual(path.read_text(), "a\n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.chmod(0o644)
        auth.write_text_atomic(path, "b\n")
        self.assertEqual(path.read_text(), "b\n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["users.txt"])  # no temp files left

    def test_failure_leaves_original_and_no_temp_file(self):
        path = self.dir / "users.txt"
        path.write_text("original\n")
        with mock.patch.object(auth.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                auth.write_text_atomic(path, "new\n")
        self.assertEqual(path.read_text(), "original\n")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["users.txt"])


class LoginFloodTest(unittest.TestCase):
    """Gradio runs /login in a thread pool (40 threads by default) with no rate limit.
    Each scrypt check takes 32 MiB, so unbounded parallel checks let anyone on the
    network exhaust memory (about 1.2 GB for 40 attempts) without an account."""

    def setUp(self):
        self.store = auth.UserStore(auth.parse_users(f"alice:{auth.hash_password(PASSWORD)}\n").records, "t")

    def test_parallel_login_checks_are_capped(self):
        import threading
        import time

        lock = threading.Lock()
        state = {"now": 0, "peak": 0}
        real_derive = auth._derive

        def slow_derive(*args, **kwargs):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            try:
                time.sleep(0.05)
                return real_derive(*args, **kwargs)
            finally:
                with lock:
                    state["now"] -= 1

        results = []
        with mock.patch.object(auth, "_derive", side_effect=slow_derive):
            threads = [threading.Thread(target=lambda: results.append(self.store.verify("mallory", "guess" * 3)))
                       for _ in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(results, [False] * 12)
        self.assertGreaterEqual(auth.MAX_PARALLEL_HASHES, 1)
        self.assertLessEqual(state["peak"], auth.MAX_PARALLEL_HASHES)

    def test_attempt_that_cannot_get_a_slot_is_rejected_without_hashing(self):
        import contextlib
        import io

        slots = auth._hash_slots
        held = 0
        try:
            while slots.acquire(blocking=False):
                held += 1
            err = io.StringIO()
            with mock.patch.object(auth, "HASH_WAIT_SECONDS", 0.05), \
                    mock.patch.object(auth, "_derive") as derive, contextlib.redirect_stderr(err):
                self.assertFalse(self.store.verify("alice", PASSWORD))  # even the right password
            derive.assert_not_called()
            self.assertNotIn(PASSWORD, err.getvalue())
        finally:
            for _ in range(held):
                slots.release()
        self.assertTrue(self.store.verify("alice", PASSWORD))  # works again once slots are free


class StandardLibraryOnlyTest(unittest.TestCase):
    def test_auth_and_manage_users_import_only_the_standard_library(self):
        app = pathlib.Path(auth.__file__).parent
        allowed_local = {"auth"}
        for name in ("auth.py", "manage_users.py"):
            source = (app / name).read_text()
            for module in re.findall(r"^(?:from|import)\s+([A-Za-z_][\w]*)", source, re.M):
                if module in allowed_local or module == "__future__":
                    continue
                self.assertIn(module, sys.stdlib_module_names, f"{name} imports non-stdlib {module}")


if __name__ == "__main__":
    unittest.main()

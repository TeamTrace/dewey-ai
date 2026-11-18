import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import contextlib
import http.client
import http.cookiejar
import io
import os
import shutil
import socket
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

import auth
import main

PASSWORD = "correct horse battery staple"

try:
    import gradio  # noqa: F401
    HAVE_GRADIO = True
except ImportError:  # pragma: no cover
    HAVE_GRADIO = False


class TempRepo:
    """A temporary 'repo root' so tests never read the developer's real users.txt."""

    def __enter__(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.root, ignore_errors=True)

    def add_users(self, *names):
        lines = [f"{n}:{auth.hash_password(PASSWORD)}" for n in names]
        (self.root / "users.txt").write_text("\n".join(lines) + "\n")


def plan(env, root):
    return main.build_launch_plan(env, repo_root=root)


class LoopbackTest(unittest.TestCase):
    def test_loopback_addresses(self):
        for host in ["127.0.0.1", "localhost", "LOCALHOST", "::1", "[::1]", "127.0.0.2", " 127.0.0.1 "]:
            self.assertTrue(main.is_loopback(host), host)

    def test_network_addresses(self):
        for host in ["0.0.0.0", "::", "[::]", "10.0.0.5", "192.168.1.20", "dewey.example.org",
                     "my-laptop", "localhost.example.org", ""]:
            self.assertFalse(main.is_loopback(host), host)


class LaunchPlanTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo().__enter__()
        self.root = self.repo.root

    def tearDown(self):
        self.repo.__exit__(None, None, None)

    def test_defaults_are_local_and_hardened(self):
        p = plan({}, self.root)
        kw = p.launch_kwargs
        self.assertEqual(kw["server_name"], "127.0.0.1")
        self.assertIsNone(kw["server_port"])  # Gradio searches from 7860
        self.assertIsNone(kw["auth"])
        self.assertIs(kw["share"], False)
        self.assertIs(kw["show_api"], False)
        self.assertIs(kw["enable_monitoring"], False)
        self.assertIs(kw["mcp_server"], False)
        self.assertEqual(kw["max_file_size"], "1mb")
        self.assertEqual(p.queue_kwargs, {"api_open": False, "default_concurrency_limit": 4})
        self.assertTrue(any("Login: off" in line and "Only this computer" in line for line in p.summary))

    def test_refuses_network_address_without_accounts(self):
        for host in ["0.0.0.0", "::", "10.1.2.3", "dewey.internal"]:
            with self.subTest(host=host):
                with self.assertRaisesRegex(main.StartupError, "Refusing to start") as cm:
                    plan({"GRADIO_SERVER_NAME": host}, self.root)
                self.assertIn("manage_users.py add", str(cm.exception))
                self.assertIn("DEWEY_ALLOW_NO_AUTH", str(cm.exception))

    def test_refuses_when_users_file_has_no_accounts(self):
        (self.root / "users.txt").write_text("# nobody yet\n")
        with self.assertRaises(main.StartupError):
            plan({"GRADIO_SERVER_NAME": "0.0.0.0"}, self.root)
        # ...but runs locally, saying the file is empty
        p = plan({}, self.root)
        self.assertIsNone(p.launch_kwargs["auth"])
        self.assertTrue(any("has no accounts" in line for line in p.summary))

    def test_allow_no_auth_permits_network_without_accounts(self):
        for value in ["true", "TRUE", "1", "yes", "on"]:
            with self.subTest(value=value):
                p = plan({"GRADIO_SERVER_NAME": "0.0.0.0", "DEWEY_ALLOW_NO_AUTH": value}, self.root)
                self.assertIsNone(p.launch_kwargs["auth"])
                self.assertTrue(any("Login: OFF" in line for line in p.summary))

    def test_allow_no_auth_false_values_still_refuse(self):
        for value in ["", "false", "0", "no", "off"]:
            with self.subTest(value=value):
                with self.assertRaises(main.StartupError):
                    plan({"GRADIO_SERVER_NAME": "0.0.0.0", "DEWEY_ALLOW_NO_AUTH": value}, self.root)

    def test_allow_no_auth_typo_is_an_error_not_false(self):
        with self.assertRaisesRegex(main.StartupError, "DEWEY_ALLOW_NO_AUTH must be true or false"):
            plan({"DEWEY_ALLOW_NO_AUTH": "ture"}, self.root)

    def test_accounts_enable_login_on_any_address(self):
        self.repo.add_users("alice", "bob")
        p = plan({"GRADIO_SERVER_NAME": "0.0.0.0", "GRADIO_SERVER_PORT": "7860",
                  "DEWEY_AUTH_MESSAGE": "Newsroom staff only"}, self.root)
        login = p.launch_kwargs["auth"]
        self.assertTrue(callable(login))
        self.assertTrue(login("alice", PASSWORD))
        self.assertFalse(login("alice", "wrong password here"))
        self.assertFalse(login("mallory", PASSWORD))
        self.assertEqual(p.launch_kwargs["auth_message"], "Newsroom staff only")
        self.assertTrue(any("Login required: 2 account(s)" in line for line in p.summary))

    def test_accounts_win_over_allow_no_auth(self):
        self.repo.add_users("alice")
        p = plan({"GRADIO_SERVER_NAME": "0.0.0.0", "DEWEY_ALLOW_NO_AUTH": "true"}, self.root)
        self.assertTrue(callable(p.launch_kwargs["auth"]))
        self.assertTrue(any("ignored" in line for line in p.summary))

    def test_accounts_from_env_var(self):
        p = plan({"GRADIO_SERVER_NAME": "0.0.0.0",
                  "DEWEY_USERS": f"alice:{auth.hash_password(PASSWORD)}"}, self.root)
        self.assertTrue(p.launch_kwargs["auth"]("alice", PASSWORD))

    def test_broken_users_configuration_stops_startup(self):
        with self.assertRaisesRegex(main.StartupError, "does not exist"):
            plan({"DEWEY_USERS_FILE": str(self.root / "missing.txt")}, self.root)
        (self.root / "users.txt").write_text("alice:not-a-hash\n")
        with self.assertRaisesRegex(main.StartupError, "none of them is valid"):
            plan({}, self.root)  # even on loopback: someone meant to set up a login

    def test_summary_and_warnings_never_contain_secrets(self):
        hashed = auth.hash_password(PASSWORD)
        (self.root / "users.txt").write_text(f"alice:{hashed}\nbob:broken-hash-text\n")
        p = plan({"GRADIO_SERVER_NAME": "0.0.0.0"}, self.root)
        text = "\n".join(p.summary + p.warnings)
        self.assertNotIn(hashed.split(":")[-1], text)
        self.assertNotIn("broken-hash-text", text)
        self.assertNotIn(PASSWORD, text)

    def test_ports(self):
        cases = [
            ({}, None),
            ({"GRADIO_SERVER_PORT": "8000"}, 8000),
            ({"PORT": "10000"}, 10000),
            ({"GRADIO_SERVER_PORT": "7860", "PORT": "10000"}, 7860),
            ({"GRADIO_SERVER_PORT": " ", "PORT": "10000"}, 10000),
        ]
        for env, expected in cases:
            with self.subTest(env=env):
                self.assertEqual(plan(env, self.root).launch_kwargs["server_port"], expected)

    def test_port_mismatch_warns(self):
        p = plan({"GRADIO_SERVER_PORT": "7860", "PORT": "10000"}, self.root)
        self.assertTrue(any("PORT" in w and "GRADIO_SERVER_PORT" in w for w in p.warnings))
        self.assertEqual(plan({"GRADIO_SERVER_PORT": "7860", "PORT": "7860"}, self.root).warnings, [])

    def test_invalid_numbers(self):
        for env in [{"GRADIO_SERVER_PORT": "http"}, {"GRADIO_SERVER_PORT": "70000"}, {"PORT": "0"},
                    {"DEWEY_CONCURRENCY": "0"}, {"DEWEY_CONCURRENCY": "many"}, {"DEWEY_CONCURRENCY": "1000"}]:
            with self.subTest(env=env):
                with self.assertRaises(main.StartupError):
                    plan(env, self.root)

    def test_concurrency(self):
        self.assertEqual(plan({"DEWEY_CONCURRENCY": "8"}, self.root).queue_kwargs["default_concurrency_limit"], 8)
        self.assertEqual(plan({"DEWEY_CONCURRENCY": ""}, self.root).queue_kwargs["default_concurrency_limit"], 4)


class FakeDewey:
    def __init__(self, *args):
        pass

    def process(self, message, history):
        yield "Look", [{"title": "Searching", "status": "pending", "content": "question"}]
        yield "Look [1](https://example.com/a)", [{"title": "Searching", "status": "done", "content": "question"}]


class ChatHandlerTest(unittest.TestCase):
    def test_streams_steps_and_answer_like_before(self):
        handler = main.make_chat_handler(FakeDewey())
        state = {"session_id": None, "user_data": {}}
        outputs = list(handler("Who covered the 1985 MOVE bombing?", [], state))
        self.assertEqual(len(outputs), 3)
        first_history, _ = outputs[0]
        self.assertEqual(first_history, [{"role": "user", "content": "Who covered the 1985 MOVE bombing?"}])
        final_history, final_state = outputs[-1]
        self.assertEqual(final_history[-1], {"role": "assistant", "content": "Look [1](https://example.com/a)"})
        self.assertEqual(final_history[1]["metadata"], {"title": "Searching", "status": "done"})
        self.assertEqual(len(final_history), 3)  # user, one step, answer (earlier partials replaced)
        self.assertIsNotNone(final_state["session_id"])
        self.assertEqual(final_state["user_data"]["message_count"], 1)


class MainFunctionTest(unittest.TestCase):
    """main() with a fake .env loader, so the developer's real .env never leaks in."""

    def run_main(self, env):
        out, err = io.StringIO(), io.StringIO()
        with TempRepo() as repo, \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(main, "REPO_ROOT", repo.root), \
                mock.patch("dotenv.load_dotenv"), \
                mock.patch.object(main, "build_ui") as build_ui, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            build_plan = main.build_launch_plan
            with mock.patch.object(main, "build_launch_plan",
                                   side_effect=lambda e: build_plan(e, repo_root=repo.root)):
                code = main.main()
        return code, out.getvalue(), err.getvalue(), build_ui

    def test_guard_exits_2_before_building_anything(self):
        code, out, err, build_ui = self.run_main({"GRADIO_SERVER_NAME": "0.0.0.0"})
        self.assertEqual(code, 2)
        self.assertIn("Refusing to start", err)
        build_ui.assert_not_called()

    def test_bad_setting_exits_2(self):
        code, _, err, build_ui = self.run_main({"GRADIO_SERVER_PORT": "abc"})
        self.assertEqual(code, 2)
        self.assertIn("GRADIO_SERVER_PORT must be a whole number", err)
        build_ui.assert_not_called()

    def test_stack_config_error_exits_1_with_message(self):
        code, out, err, build_ui = self.run_main({"DEWEY_STACK": "anthropic"})
        self.assertEqual(code, 1)
        self.assertIn("ANTHROPIC_API_KEY", err)
        self.assertIn("Listening on 127.0.0.1", out)
        build_ui.assert_not_called()

    def test_blank_gradio_variables_are_removed_before_gradio_reads_them(self):
        with mock.patch.dict(os.environ, {"GRADIO_SERVER_PORT": "", "GRADIO_SERVER_NAME": " "}, clear=True), \
                mock.patch("dotenv.load_dotenv"), \
                mock.patch.object(main, "build_launch_plan", side_effect=main.StartupError("stop")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main.main(), 2)
            self.assertNotIn("GRADIO_SERVER_PORT", os.environ)
            self.assertNotIn("GRADIO_SERVER_NAME", os.environ)

    @unittest.skipUnless(HAVE_GRADIO, "gradio not installed")
    def test_launch_receives_the_plan(self):
        import gradio as gr

        with TempRepo() as repo:
            repo.add_users("alice")
            env = {"GRADIO_SERVER_NAME": "0.0.0.0", "GRADIO_SERVER_PORT": "7999", "DEWEY_CONCURRENCY": "3"}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(main, "REPO_ROOT", repo.root), \
                    mock.patch("dotenv.load_dotenv"), \
                    mock.patch("providers.build_stack", return_value=(object(), object())), \
                    mock.patch("dewey.Dewey", FakeDewey), \
                    mock.patch.object(gr.Blocks, "queue") as queue, \
                    mock.patch.object(gr.Blocks, "launch") as launch, \
                    contextlib.redirect_stdout(io.StringIO()):
                original = main.build_launch_plan
                with mock.patch.object(main, "build_launch_plan",
                                       side_effect=lambda e: original(e, repo_root=repo.root)):
                    self.assertEqual(main.main(), 0)
        # Gradio's Blocks.__exit__ calls queue() with defaults first; Dewey's call must come last.
        queue.assert_called_with(api_open=False, default_concurrency_limit=3)
        kwargs = launch.call_args.kwargs
        self.assertEqual(kwargs["server_name"], "0.0.0.0")
        self.assertEqual(kwargs["server_port"], 7999)
        self.assertTrue(kwargs["auth"]("alice", PASSWORD))
        self.assertIs(kwargs["show_api"], False)
        self.assertIs(kwargs["enable_monitoring"], False)
        self.assertIs(kwargs["share"], False)

    @unittest.skipUnless(HAVE_GRADIO, "gradio not installed")
    def test_ui_has_telemetry_off(self):
        demo = main.build_ui(FakeDewey())
        self.assertIs(demo.analytics_enabled, False)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(HAVE_GRADIO, "gradio not installed")
class LiveServerTest(unittest.TestCase):
    """Starts the real Gradio app on 127.0.0.1 (no outside network) and logs in."""

    @classmethod
    def setUpClass(cls):
        cls.repo = TempRepo().__enter__()
        cls.repo.add_users("alice")
        port = free_port()
        env = {"GRADIO_SERVER_PORT": str(port), "DEWEY_AUTH_MESSAGE": "Newsroom staff only"}
        p = main.build_launch_plan(env, repo_root=cls.repo.root)
        cls.demo = main.build_ui(FakeDewey())
        cls.demo.queue(**p.queue_kwargs)
        with contextlib.redirect_stdout(io.StringIO()):
            cls.demo.launch(**p.launch_kwargs, prevent_thread_lock=True, quiet=True)
        cls.base = f"http://127.0.0.1:{port}"

    @classmethod
    def tearDownClass(cls):
        with contextlib.redirect_stdout(io.StringIO()):
            cls.demo.close()
        cls.repo.__exit__(None, None, None)

    def request(self, path, opener=None, data=None):
        opener = opener or urllib.request.build_opener()
        try:
            with opener.open(self.base + path, data=data, timeout=15) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def login(self, opener, username, password):
        return self.request("/login", opener, urllib.parse.urlencode({"username": username, "password": password}).encode())

    def test_root_answers_200_for_health_checks(self):
        self.assertEqual(self.request("/")[0], 200)

    def test_app_config_needs_login(self):
        status, body = self.request("/config")
        self.assertEqual(status, 401)
        self.assertIn("Newsroom staff only", body)

    def test_wrong_password_and_unknown_user_are_rejected(self):
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.assertEqual(self.login(opener, "alice", "wrong password here")[0], 400)
        self.assertEqual(self.login(opener, "mallory", PASSWORD)[0], 400)
        self.assertEqual(self.request("/config", opener)[0], 401)

    def test_login_then_config_and_monitoring_stays_off(self):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.assertEqual(self.login(opener, "alice", PASSWORD)[0], 200)
        self.assertTrue(any(c.name.startswith("access-token") for c in jar))
        self.assertEqual(self.request("/config", opener)[0], 200)
        for path in ["/monitoring", "/monitoring/summary"]:
            with self.subTest(path=path):
                self.assertEqual(self.request(path, opener)[0], 404)

    def test_monitoring_summary_is_not_served_without_login(self):
        # Gradio 5.44.1 serves /monitoring/summary (per-function request counts and
        # timings) with no login check, even with enable_monitoring=False.
        self.assertEqual(self.request("/monitoring/summary")[0], 404)

    def raw_login(self, headers):
        port = int(self.base.rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            body = urllib.parse.urlencode({"username": "alice", "password": PASSWORD})
            conn.request("POST", "/login", body=body, headers={
                "Content-Type": "application/x-www-form-urlencoded", **headers})
            response = conn.getresponse()
            response.read()
            return response.status, response.headers.get_all("Set-Cookie") or []
        finally:
            conn.close()

    def test_https_login_sets_only_the_secure_cookie(self):
        # Gradio also sets a copy of the session cookie without the Secure flag
        # (access-token-unsecure-...). Behind an HTTPS proxy the browser would then
        # send the session token in clear text on any visit to the http:// address.
        status, cookies = self.raw_login({"X-Forwarded-Proto": "https"})
        self.assertEqual(status, 200)
        self.assertTrue(cookies)
        self.assertFalse([c for c in cookies if c.startswith("access-token-unsecure-")], cookies)
        (secure,) = [c for c in cookies if c.startswith("access-token-")]
        self.assertIn("secure", secure.lower())
        # ...and that cookie alone keeps the person logged in
        conn = http.client.HTTPConnection("127.0.0.1", int(self.base.rsplit(":", 1)[1]), timeout=15)
        try:
            conn.request("GET", "/config", headers={"Cookie": secure.split(";", 1)[0]})
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
        finally:
            conn.close()

    def test_plain_http_login_still_works(self):
        # Without HTTPS (local use) Gradio needs its non-Secure cookie; leave it alone.
        status, cookies = self.raw_login({})
        self.assertEqual(status, 200)
        self.assertTrue([c for c in cookies if c.startswith("access-token-unsecure-")], cookies)

    def test_settings_applied(self):
        self.assertIs(self.demo.analytics_enabled, False)
        self.assertIs(self.demo.api_open, False)
        self.assertIs(self.demo.show_api, False)
        self.assertEqual(self.demo.max_file_size, 1024 * 1024)
        self.assertEqual(self.demo._queue.default_concurrency_limit, 4)


def raw_get(port, path, host):
    """GET with an explicit Host header, as a browser does after DNS rebinding."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request("GET", path, headers={"Host": host})
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        conn.close()


class HostCheckTest(unittest.TestCase):
    def test_loopback_host_headers(self):
        for host in ["127.0.0.1", "127.0.0.1:7860", "localhost", "LOCALHOST:7860", "[::1]:7860", "[::1]",
                     "dewey.localhost:7860", "127.0.0.2:80"]:
            self.assertTrue(main.loopback_host_header(host), host)
        for host in ["evil.example", "evil.example:7860", "127.0.0.1.evil.example", "localhost.evil.example",
                     "0.0.0.0:7860", "10.0.0.5", "[::]:7860", "[::1", "", "localhost:7860@evil.example"]:
            self.assertFalse(main.loopback_host_header(host), host)

    def test_plan_turns_the_host_check_on_only_for_local_no_login(self):
        def guard(p):
            (middleware,) = p.launch_kwargs["app_kwargs"]["middleware"]
            return middleware.kwargs["loopback_only"]

        with TempRepo() as repo:
            self.assertIs(guard(main.build_launch_plan({}, repo_root=repo.root)), True)
            self.assertIs(guard(main.build_launch_plan(
                {"GRADIO_SERVER_NAME": "0.0.0.0", "DEWEY_ALLOW_NO_AUTH": "true"}, repo_root=repo.root)), False)
            self.assertIs(guard(main.build_launch_plan({"DEWEY_ALLOW_NO_AUTH": "true"}, repo_root=repo.root)), False)
            repo.add_users("alice")
            self.assertIs(guard(main.build_launch_plan({}, repo_root=repo.root)), False)


@unittest.skipUnless(HAVE_GRADIO, "gradio not installed")
class LocalNoLoginServerTest(unittest.TestCase):
    """The default mode (127.0.0.1, no accounts). A web page the person visits can point
    its own host name at 127.0.0.1 (DNS rebinding) and then use Dewey as same-origin,
    so Dewey must refuse requests whose Host isn't a loopback name."""

    @classmethod
    def setUpClass(cls):
        cls.repo = TempRepo().__enter__()
        cls.port = free_port()
        p = main.build_launch_plan({"GRADIO_SERVER_PORT": str(cls.port)}, repo_root=cls.repo.root)
        cls.demo = main.build_ui(FakeDewey())
        cls.demo.queue(**p.queue_kwargs)
        with contextlib.redirect_stdout(io.StringIO()):
            cls.demo.launch(**p.launch_kwargs, prevent_thread_lock=True, quiet=True)

    @classmethod
    def tearDownClass(cls):
        with contextlib.redirect_stdout(io.StringIO()):
            cls.demo.close()
        cls.repo.__exit__(None, None, None)

    def test_rebound_host_names_are_refused(self):
        for host in [f"evil.example:{self.port}", "evil.example", f"0.0.0.0:{self.port}"]:
            for path in ["/", "/config", "/gradio_api/info"]:
                with self.subTest(host=host, path=path):
                    status, body = raw_get(self.port, path, host)
                    self.assertEqual(status, 400)
                    self.assertNotIn("api_prefix", body)

    def test_loopback_host_names_work(self):
        for host in [f"127.0.0.1:{self.port}", f"localhost:{self.port}"]:
            with self.subTest(host=host):
                self.assertEqual(raw_get(self.port, "/config", host)[0], 200)
                self.assertEqual(raw_get(self.port, "/", host)[0], 200)

    def test_monitoring_summary_is_not_served(self):
        self.assertEqual(raw_get(self.port, "/monitoring/summary", f"127.0.0.1:{self.port}")[0], 404)

    def test_a_question_still_streams_through_the_guard(self):
        import json

        base = f"http://127.0.0.1:{self.port}"
        with urllib.request.urlopen(base + "/config", timeout=15) as r:
            config = json.load(r)
        (submit,) = [d for d in config["dependencies"]
                     if d["targets"] and d["targets"][0][1] == "submit" and len(d["inputs"]) == 3]
        body = json.dumps({"data": ["Who covered the 1985 MOVE bombing?", [], {"session_id": None, "user_data": {}}],
                           "fn_index": submit["id"], "trigger_id": submit["targets"][0][0],
                           "session_hash": "deploytest1", "event_data": None}).encode()
        request = urllib.request.Request(base + "/gradio_api/queue/join", data=body,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=15) as r:
            self.assertIn("event_id", json.load(r))
        with urllib.request.urlopen(base + "/gradio_api/queue/data?session_hash=deploytest1", timeout=15) as r:
            stream = r.read().decode("utf-8")
        self.assertIn("process_completed", stream)
        self.assertIn("https://example.com/a", stream)


if __name__ == "__main__":
    unittest.main()

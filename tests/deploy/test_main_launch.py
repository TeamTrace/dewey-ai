import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import contextlib
import http.client
import io
import json
import os
import socket
import unittest
import urllib.error
import urllib.request
from unittest import mock

import main

try:
    import gradio  # noqa: F401
    HAVE_GRADIO = True
except ImportError:  # pragma: no cover
    HAVE_GRADIO = False

PROXY = "DEWEY_DEMO_BEHIND_AUTH_PROXY"
PUBLISHED = "DEWEY_DEMO_PUBLISHED_ON_LOOPBACK"


def plan(env):
    return main.build_launch_plan(env)


def host_check(p):
    """Whether the plan's RequestGuard refuses non-loopback Host headers."""
    (middleware,) = p.launch_kwargs["app_kwargs"]["middleware"]
    return middleware.kwargs["loopback_only"]


class LoopbackTest(unittest.TestCase):
    def test_loopback_addresses(self):
        for host in ["127.0.0.1", "localhost", "LOCALHOST", "::1", "[::1]", "127.0.0.2", " 127.0.0.1 "]:
            self.assertTrue(main.is_loopback(host), host)

    def test_network_addresses(self):
        for host in ["0.0.0.0", "::", "[::]", "10.0.0.5", "192.168.1.20", "dewey.example.org",
                     "my-laptop", "localhost.example.org", ""]:
            self.assertFalse(main.is_loopback(host), host)


class LaunchPlanTest(unittest.TestCase):
    def test_defaults_are_local_and_hardened(self):
        p = plan({})
        kw = p.launch_kwargs
        self.assertEqual(kw["server_name"], "127.0.0.1")
        self.assertIsNone(kw["server_port"])  # Gradio searches from 7860
        self.assertNotIn("auth", kw)  # no login system: the demo is for this computer
        self.assertIs(kw["share"], False)
        self.assertIs(kw["show_api"], False)
        self.assertIs(kw["enable_monitoring"], False)
        self.assertIs(kw["mcp_server"], False)
        self.assertEqual(kw["max_file_size"], "1mb")
        self.assertEqual(p.queue_kwargs, {"api_open": False, "default_concurrency_limit": 4})
        self.assertIs(host_check(p), True)
        self.assertTrue(any("no login" in line and "Only this computer" in line for line in p.summary))
        self.assertTrue(any("docs/MCP.md" in line for line in p.summary))

    def test_refuses_network_address_by_default(self):
        for host in ["0.0.0.0", "::", "10.1.2.3", "dewey.internal"]:
            with self.subTest(host=host):
                with self.assertRaisesRegex(main.StartupError, "Refusing to start") as cm:
                    plan({"GRADIO_SERVER_NAME": host})
                message = str(cm.exception)
                self.assertIn(PROXY, message)
                self.assertIn("mcp_server.py", message)
                self.assertIn("unset GRADIO_SERVER_NAME", message)
                # The Compose demo service sets DEWEY_DEMO_PUBLISHED_ON_LOOPBACK itself. Offering it
                # here would invite someone on a bare host to set it just to make the error go away,
                # which puts the no-login page on the network (the Host check doesn't stop a direct
                # request that sends Host: 127.0.0.1).
                self.assertNotIn(PUBLISHED, message)
                self.assertIn("docker compose --profile demo up", message)

    def test_loopback_addresses_need_no_flag(self):
        for host in ["127.0.0.1", "localhost", "::1"]:
            with self.subTest(host=host):
                self.assertEqual(plan({"GRADIO_SERVER_NAME": host}).launch_kwargs["server_name"], host)

    def test_behind_auth_proxy_permits_network_and_turns_the_host_check_off(self):
        # A sign-in proxy forwards its own public Host header, so the demo can't insist on localhost.
        for value in ["true", "TRUE", "1", "yes", "on"]:
            with self.subTest(value=value):
                p = plan({"GRADIO_SERVER_NAME": "0.0.0.0", PROXY: value})
                self.assertEqual(p.launch_kwargs["server_name"], "0.0.0.0")
                self.assertIs(host_check(p), False)
                self.assertTrue(any(f"{PROXY}=true" in line for line in p.summary))
                self.assertFalse(any("Requests must be addressed to localhost" in line for line in p.summary))
        # also for a proxy on the same machine forwarding to 127.0.0.1
        self.assertIs(host_check(plan({PROXY: "true"})), False)

    def test_published_on_loopback_permits_network_and_keeps_the_host_check(self):
        p = plan({"GRADIO_SERVER_NAME": "0.0.0.0", "GRADIO_SERVER_PORT": "7860", PUBLISHED: "true"})
        self.assertEqual(p.launch_kwargs["server_name"], "0.0.0.0")
        self.assertIs(host_check(p), True)  # the browser on the host sends Host: 127.0.0.1:7860
        self.assertTrue(any(f"{PUBLISHED}=true" in line for line in p.summary))
        self.assertTrue(any("Requests must be addressed to localhost" in line for line in p.summary))

    def test_both_flags_the_proxy_wins(self):
        p = plan({"GRADIO_SERVER_NAME": "0.0.0.0", PROXY: "true", PUBLISHED: "true"})
        self.assertIs(host_check(p), False)

    def test_false_values_still_refuse(self):
        for name in [PROXY, PUBLISHED]:
            for value in ["", "false", "0", "no", "off"]:
                with self.subTest(name=name, value=value):
                    with self.assertRaises(main.StartupError):
                        plan({"GRADIO_SERVER_NAME": "0.0.0.0", name: value})

    def test_flag_typo_is_an_error_not_false(self):
        for name in [PROXY, PUBLISHED]:
            with self.subTest(name=name):
                with self.assertRaisesRegex(main.StartupError, f"{name} must be true or false"):
                    plan({name: "ture"})

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
                self.assertEqual(plan(env).launch_kwargs["server_port"], expected)

    def test_port_mismatch_warns(self):
        p = plan({"GRADIO_SERVER_PORT": "7860", "PORT": "10000"})
        self.assertTrue(any("PORT" in w and "GRADIO_SERVER_PORT" in w for w in p.warnings))
        self.assertEqual(plan({"GRADIO_SERVER_PORT": "7860", "PORT": "7860"}).warnings, [])

    def test_invalid_numbers(self):
        for env in [{"GRADIO_SERVER_PORT": "http"}, {"GRADIO_SERVER_PORT": "70000"}, {"PORT": "0"},
                    {"DEWEY_CONCURRENCY": "0"}, {"DEWEY_CONCURRENCY": "many"}, {"DEWEY_CONCURRENCY": "1000"}]:
            with self.subTest(env=env):
                with self.assertRaises(main.StartupError):
                    plan(env)

    def test_concurrency(self):
        self.assertEqual(plan({"DEWEY_CONCURRENCY": "8"}).queue_kwargs["default_concurrency_limit"], 8)
        self.assertEqual(plan({"DEWEY_CONCURRENCY": ""}).queue_kwargs["default_concurrency_limit"], 4)


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
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("dotenv.load_dotenv"), \
                mock.patch.object(main, "build_ui") as build_ui, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.main()
        return code, out.getvalue(), err.getvalue(), build_ui

    def test_guard_exits_2_before_building_anything(self):
        code, out, err, build_ui = self.run_main({"GRADIO_SERVER_NAME": "0.0.0.0"})
        self.assertEqual(code, 2)
        self.assertIn("Refusing to start", err)
        self.assertEqual(out, "")
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

        env = {"GRADIO_SERVER_NAME": "0.0.0.0", "GRADIO_SERVER_PORT": "7999", "DEWEY_CONCURRENCY": "3",
               PUBLISHED: "true"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("dotenv.load_dotenv"), \
                mock.patch("providers.build_stack", return_value=(object(), object())), \
                mock.patch("dewey.Dewey", FakeDewey), \
                mock.patch.object(gr.Blocks, "queue") as queue, \
                mock.patch.object(gr.Blocks, "launch") as launch, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(), 0)
        # Gradio's Blocks.__exit__ calls queue() with defaults first; Dewey's call must come last.
        queue.assert_called_with(api_open=False, default_concurrency_limit=3)
        kwargs = launch.call_args.kwargs
        self.assertEqual(kwargs["server_name"], "0.0.0.0")
        self.assertEqual(kwargs["server_port"], 7999)
        self.assertNotIn("auth", kwargs)
        self.assertIs(kwargs["show_api"], False)
        self.assertIs(kwargs["enable_monitoring"], False)
        self.assertIs(kwargs["share"], False)
        self.assertIs(kwargs["mcp_server"], False)

    @unittest.skipUnless(HAVE_GRADIO, "gradio not installed")
    def test_ui_has_telemetry_off(self):
        demo = main.build_ui(FakeDewey())
        self.assertIs(demo.analytics_enabled, False)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def raw_get(port, path, host):
    """GET with an explicit Host header, as a browser sends after DNS rebinding."""
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
                     "0.0.0.0:7860", "10.0.0.5", "[::]:7860", "[::1", "", "localhost:7860@evil.example",
                     "demo:7860"]:
            self.assertFalse(main.loopback_host_header(host), host)


class LiveDemo:
    """The real Gradio app on 127.0.0.1 (no outside network), with a fake Dewey."""

    def __init__(self, env):
        self.port = free_port()
        p = main.build_launch_plan({**env, "GRADIO_SERVER_PORT": str(self.port)})
        self.demo = main.build_ui(FakeDewey())
        self.demo.queue(**p.queue_kwargs)
        with contextlib.redirect_stdout(io.StringIO()):
            self.demo.launch(**p.launch_kwargs, prevent_thread_lock=True, quiet=True)
        self.base = f"http://127.0.0.1:{self.port}"

    def close(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.demo.close()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=15) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")


@unittest.skipUnless(HAVE_GRADIO, "gradio not installed")
class LocalDemoServerTest(unittest.TestCase):
    """The default mode: 127.0.0.1, no login. A web page the person visits can point its own
    host name at 127.0.0.1 (DNS rebinding) and then use the demo as same-origin, so the demo
    must refuse requests whose Host isn't a loopback name."""

    @classmethod
    def setUpClass(cls):
        cls.server = LiveDemo({})
        cls.port = cls.server.port

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_root_and_config_answer_200(self):
        self.assertEqual(self.server.get("/")[0], 200)
        self.assertEqual(self.server.get("/config")[0], 200)

    def test_monitoring_is_not_served(self):
        # Gradio 5.44.1 serves /monitoring/summary (per-function request counts and
        # timings) even with enable_monitoring=False.
        for path in ["/monitoring", "/monitoring/summary"]:
            with self.subTest(path=path):
                self.assertEqual(self.server.get(path)[0], 404)

    def test_rebound_host_names_are_refused(self):
        for host in [f"evil.example:{self.port}", "evil.example", f"0.0.0.0:{self.port}"]:
            for path in ["/", "/config", "/gradio_api/info"]:
                with self.subTest(host=host, path=path):
                    status, body = raw_get(self.port, path, host)
                    self.assertEqual(status, 400)
                    self.assertNotIn("api_prefix", body)
                    self.assertIn("127.0.0.1", body)

    def test_loopback_host_names_work(self):
        for host in [f"127.0.0.1:{self.port}", f"localhost:{self.port}"]:
            with self.subTest(host=host):
                self.assertEqual(raw_get(self.port, "/config", host)[0], 200)
                self.assertEqual(raw_get(self.port, "/", host)[0], 200)

    def test_settings_applied(self):
        demo = self.server.demo
        self.assertIs(demo.analytics_enabled, False)
        self.assertIs(demo.api_open, False)
        self.assertIs(demo.show_api, False)
        self.assertEqual(demo.max_file_size, 1024 * 1024)
        self.assertEqual(demo._queue.default_concurrency_limit, 4)

    def test_a_question_still_streams_through_the_guard(self):
        base = self.server.base
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


@unittest.skipUnless(HAVE_GRADIO, "gradio not installed")
class BehindAuthProxyServerTest(unittest.TestCase):
    """DEWEY_DEMO_BEHIND_AUTH_PROXY=true on 127.0.0.1: a sign-in proxy on the same machine
    forwards the public Host header, which the demo must then accept."""

    @classmethod
    def setUpClass(cls):
        cls.server = LiveDemo({PROXY: "true"})

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_public_host_name_is_accepted(self):
        self.assertEqual(raw_get(self.server.port, "/config", "dewey.example.org")[0], 200)

    def test_monitoring_is_still_not_served(self):
        self.assertEqual(raw_get(self.server.port, "/monitoring/summary", "dewey.example.org")[0], 404)


@unittest.skipUnless(HAVE_GRADIO, "gradio not installed")
class PublishedOnLoopbackServerTest(unittest.TestCase):
    """The Compose `demo` service: 0.0.0.0 with DEWEY_DEMO_PUBLISHED_ON_LOOPBACK=true and the
    Host check on. launch() itself requests the app's local URL before returning; if Gradio
    addressed that request to 0.0.0.0, the guard would refuse it and the container would never
    start. The Compose healthcheck uses http://127.0.0.1:7860/."""

    @classmethod
    def setUpClass(cls):
        cls.server = LiveDemo({"GRADIO_SERVER_NAME": "0.0.0.0", PUBLISHED: "true"})

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_launch_succeeded_and_loopback_names_work(self):
        for host in [f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}"]:
            with self.subTest(host=host):
                self.assertEqual(raw_get(self.server.port, "/", host)[0], 200)

    def test_other_names_are_still_refused(self):
        for host in [f"demo:{self.server.port}", f"0.0.0.0:{self.server.port}", "evil.example"]:
            with self.subTest(host=host):
                self.assertEqual(raw_get(self.server.port, "/config", host)[0], 400)


if __name__ == "__main__":
    unittest.main()

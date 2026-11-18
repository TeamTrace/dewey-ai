"""In-process tests of the MCP server: input validation, tool output, auth settings. No subprocess."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import asyncio
import contextlib
import io
import json
import threading
import unittest

from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult

import mcp_server
from mcp_server import (
    HttpConfig, StartupError, StaticTokenVerifier, build_metadata, create_server, is_loopback,
    parse_args, resolve_http_config, stdout_to_stderr, transport_security_for,
)
from models import NewsroomConfig

from .fake_stack import STRAY_STDOUT_MARKER, FakeLLM, FakeRetriever
from .helpers import TEST_TOKEN


def make_server(**kwargs):
    return create_server(FakeLLM(), FakeRetriever(), NewsroomConfig(publisher="The Example Times"), **kwargs)


class BuildMetadataTest(unittest.TestCase):
    def test_same_shape_as_the_llm_providers(self):
        self.assertEqual(
            build_metadata("  budget votes ", "2023-01-01", "2023-12-31", ["Jane Doe", "", " Jane Doe ", "John Roe"]),
            {
                "question": "budget votes",
                "date_range": {"start_date": "2023-01-01", "end_date": "2023-12-31"},
                "authors": [{"name": "Jane Doe"}, {"name": "John Roe"}],
            },
        )

    def test_optional_filters_default_to_none(self):
        self.assertEqual(
            build_metadata("q", "", None, None),
            {"question": "q", "date_range": {"start_date": None, "end_date": None}, "authors": []},
        )

    def test_rejects_bad_input(self):
        cases = {
            "empty question": dict(question="   "),
            "long question": dict(question="x" * (mcp_server.MAX_QUESTION_CHARS + 1)),
            "slashes": dict(question="q", start_date="2024/01/01"),
            "basic ISO without dashes": dict(question="q", start_date="20240101"),
            "ISO week date": dict(question="q", end_date="2024-W01-1"),
            "datetime": dict(question="q", end_date="2024-01-01T00:00:00"),
            "impossible date": dict(question="q", start_date="2023-02-30"),
            "start after end": dict(question="q", start_date="2024-02-01", end_date="2024-01-31"),
            "too many authors": dict(question="q", authors=[f"A{i}" for i in range(mcp_server.MAX_AUTHORS + 1)]),
            "long author": dict(question="q", authors=["x" * (mcp_server.MAX_AUTHOR_CHARS + 1)]),
        }
        for label, kwargs in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                build_metadata(**kwargs)


class ToolsTest(unittest.TestCase):
    def call(self, server, name, arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = asyncio.run(server.call_tool(name, arguments))
        return result, out.getvalue(), err.getvalue()

    def test_lists_two_read_only_tools_with_safety_notes(self):
        tools = {t.name: t for t in asyncio.run(make_server().list_tools())}
        self.assertEqual(set(tools), {"search_archive", "ask_archive"})
        for tool in tools.values():
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertIn("untrusted", tool.description)
            self.assertIn("The Example Times", tool.description)
        self.assertIn("cite its url", tools["search_archive"].description)
        self.assertIn("no language model", tools["search_archive"].description)
        self.assertIn("costs money", tools["ask_archive"].description)
        self.assertEqual(
            set(tools["search_archive"].inputSchema["properties"]), {"question", "start_date", "end_date", "authors"}
        )
        self.assertEqual(tools["search_archive"].inputSchema["required"], ["question"])
        self.assertEqual(set(tools["ask_archive"].inputSchema["properties"]), {"question"})

    def test_search_archive_filters_and_numbers_sources(self):
        result, out, err = self.call(
            make_server(), "search_archive", {"question": "budget", "start_date": "2000-01-01", "authors": ["Jane Doe"]}
        )
        self.assertIsInstance(result, CallToolResult)
        rows = result.structuredContent["result"]
        self.assertEqual(json.loads(result.content[0].text), rows)
        self.assertEqual([r["source_id"] for r in rows], ["SRC1"])
        self.assertEqual(rows[0], {
            "source_id": "SRC1",
            "headline": "Council approves 2023 budget",
            "url": "https://news.example.org/2023/03/council-budget",
            "publish_date": "2023-03-01",
            "authors": ["Jane Doe"],
            "text": "The city council approved the 2023 budget on a 9-4 vote. (query: budget)",
        })
        # The fake retriever printed to stdout; the server sent it to stderr instead.
        self.assertEqual(out, "")
        self.assertIn(STRAY_STDOUT_MARKER, err)

    def test_search_archive_empty_result_is_an_empty_json_list(self):
        result, _, _ = self.call(make_server(), "search_archive", {"question": "nothing matches"})
        self.assertEqual(result.structuredContent, {"result": []})
        self.assertEqual([c.text for c in result.content], ["[]"])

    def test_invalid_arguments_become_tool_errors(self):
        server = make_server()
        for args, message in [
            ({"question": "q", "end_date": "May 2024"}, "YYYY-MM-DD"),
            ({"question": "q", "authors": [f"A{i}" for i in range(11)]}, "authors"),
            ({"question": ""}, "question must not be empty"),
        ]:
            with self.subTest(args=args), self.assertRaises(ToolError) as cm:
                self.call(server, "search_archive", args)
            self.assertIn(message, str(cm.exception))

    def test_provider_errors_become_tool_errors_and_are_logged(self):
        with self.assertLogs("dewey.mcp", "ERROR") as logs, self.assertRaises(ToolError) as cm:
            self.call(make_server(), "search_archive", {"question": "explode"})
        self.assertIn("fake retriever failure", str(cm.exception))
        self.assertIn("RuntimeError: fake retriever failure", logs.output[0])

    def test_ask_archive_returns_the_final_linked_answer_and_its_sources(self):
        (content, structured), out, err = self.call(
            make_server(), "ask_archive", {"question": "What happened with the budget?"}
        )
        self.assertEqual(json.loads(content[0].text), structured)
        self.assertEqual(
            structured["answer"],
            "The council approved the budget [[1]](https://news.example.org/2023/03/council-budget)."
            " Parking got more expensive [[2]](https://news.example.org/2023/09/parking-fees)."
            " Final sentence cites a missing source [[9]](#).",
        )
        self.assertEqual([s["source_id"] for s in structured["sources"]], ["SRC1", "SRC2", "SRC3"])
        self.assertEqual(structured["sources"][1]["url"], "https://news.example.org/2023/09/parking-fees")
        self.assertEqual(out, "")
        self.assertIn(STRAY_STDOUT_MARKER, err)

    def test_ask_archive_uses_the_llms_search_plan(self):
        (_, structured), _, _ = self.call(make_server(), "ask_archive", {"question": "What did Jane Doe write?"})
        self.assertTrue(all("Jane Doe" in s["authors"] for s in structured["sources"]))
        self.assertEqual(len(structured["sources"]), 2)

    def test_ask_archive_with_no_sources(self):
        (_, structured), _, _ = self.call(make_server(), "ask_archive", {"question": "nothing at all"})
        self.assertEqual(structured["sources"], [])
        self.assertIn("could not find", structured["answer"])


class StdoutToStderrTest(unittest.TestCase):
    def test_overlapping_threads_restore_stdout_only_at_the_end(self):
        real_out, fake_err = io.StringIO(), io.StringIO()
        first_in, second_in, first_out = threading.Event(), threading.Event(), threading.Event()
        seen = {}

        def first():
            with stdout_to_stderr():
                first_in.set()
                second_in.wait(5)
            first_out.set()

        def second():
            first_in.wait(5)
            with stdout_to_stderr():
                second_in.set()
                first_out.wait(5)
                # contextlib.redirect_stdout would have restored the real stdout here.
                seen["during"] = sys.stdout
                print("from second")
            seen["after"] = sys.stdout

        with contextlib.redirect_stdout(real_out), contextlib.redirect_stderr(fake_err):
            threads = [threading.Thread(target=first), threading.Thread(target=second)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertIs(seen["during"], fake_err)
        self.assertIs(seen["after"], real_out)
        self.assertEqual(real_out.getvalue(), "")
        self.assertIn("from second", fake_err.getvalue())


class HttpSettingsTest(unittest.TestCase):
    def resolve(self, argv, token=""):
        return resolve_http_config(parse_args(["--http", *argv]), {"DEWEY_MCP_TOKEN": token})

    def test_token_required_and_checked(self):
        self.assertEqual(self.resolve([], TEST_TOKEN).token, TEST_TOKEN)
        for argv, token, message in [
            ([], "", "secrets.token_urlsafe"),
            (["--host", "0.0.0.0"], "", "DEWEY_MCP_TOKEN"),
            ([], "too-short", "at least 32"),
            ([], "x" * 31, "at least 32"),
            ([], "has spaces " + "x" * 40, "only letters"),
            ([], "é" * 40, "only letters"),
        ]:
            with self.subTest(argv=argv, token=token), self.assertRaises(StartupError) as cm:
                self.resolve(argv, token)
            self.assertIn(message, str(cm.exception))

    def test_no_auth_only_on_loopback(self):
        for host in ["127.0.0.1", "localhost", "::1", "127.0.0.2"]:
            with self.subTest(host=host):
                self.assertIsNone(self.resolve(["--host", host, "--no-auth"]).token)
        for host in ["0.0.0.0", "::", "192.168.1.20", "dewey.example.org"]:
            with self.subTest(host=host), self.assertRaises(StartupError) as cm:
                self.resolve(["--host", host, "--no-auth"], TEST_TOKEN)
            self.assertIn("loopback", str(cm.exception))

    def test_no_auth_refused_with_public_url(self):
        # --public-url says a reverse proxy publishes this server, so "loopback only" no longer holds.
        for host in ["127.0.0.1", "localhost", "::1"]:
            with self.subTest(host=host), self.assertRaises(StartupError) as cm:
                self.resolve(["--host", host, "--no-auth", "--public-url", "https://dewey.example.org/mcp"])
            self.assertIn("--public-url", str(cm.exception))

    def test_http_options_need_http(self):
        for argv in (["--host", "0.0.0.0"], ["--port", "9000"], ["--no-auth"], ["--public-url", "https://x.org/mcp"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(argv)

    def test_public_url_validation(self):
        self.assertEqual(
            self.resolve(["--public-url", "https://dewey.example.org/mcp/"], TEST_TOKEN).public_url,
            "https://dewey.example.org/mcp",
        )
        for url in ["dewey.example.org/mcp", "ftp://dewey.example.org/mcp", "https://u:p@dewey.example.org/mcp",
                    "https://dewey.example.org/mcp?x=1", "https://dewey.example.org:99999/mcp"]:
            with self.subTest(url=url), self.assertRaises(StartupError):
                self.resolve(["--public-url", url], TEST_TOKEN)

    def test_is_loopback(self):
        for host, expected in [("127.0.0.1", True), ("127.9.9.9", True), ("::1", True), ("[::1]", True),
                               ("localhost", True), ("0.0.0.0", False), ("::", False), ("10.0.0.5", False),
                               ("dewey.internal", False)]:
            with self.subTest(host=host):
                self.assertEqual(is_loopback(host), expected)

    def test_token_verifier(self):
        verifier = StaticTokenVerifier(TEST_TOKEN)
        self.assertEqual(asyncio.run(verifier.verify_token(TEST_TOKEN)).client_id, "dewey-mcp-client")
        for wrong in ["", TEST_TOKEN[:-1], TEST_TOKEN + "x", "é" * 51]:
            with self.subTest(wrong=wrong):
                self.assertIsNone(asyncio.run(verifier.verify_token(wrong)))

    def test_transport_security(self):
        settings = transport_security_for(HttpConfig("127.0.0.1", 8765, TEST_TOKEN, "https://dewey.example.org/mcp"))
        self.assertTrue(settings.enable_dns_rebinding_protection)
        self.assertIn("dewey.example.org", settings.allowed_hosts)
        self.assertIn("https://dewey.example.org", settings.allowed_origins)
        self.assertIn("[::1]:*", transport_security_for(HttpConfig("::1", 8765, TEST_TOKEN)).allowed_hosts)
        self.assertIsNone(transport_security_for(HttpConfig("0.0.0.0", 8765, TEST_TOKEN)))

    def test_transport_security_matches_the_host_header_proxies_send(self):
        # Proxies and clients send the host in lower case, and without the default port.
        for public_url, host, origin in [
            ("https://Dewey.Example.org/mcp", "dewey.example.org", "https://dewey.example.org"),
            ("https://dewey.example.org:443/mcp", "dewey.example.org", "https://dewey.example.org"),
            ("https://dewey.example.org:8443/mcp", "dewey.example.org:8443", "https://dewey.example.org:8443"),
            ("https://[2001:db8::1]/mcp", "[2001:db8::1]", "https://[2001:db8::1]"),
        ]:
            with self.subTest(public_url=public_url):
                public = resolve_http_config(
                    parse_args(["--http", "--public-url", public_url]), {"DEWEY_MCP_TOKEN": TEST_TOKEN}
                ).public_url
                settings = transport_security_for(HttpConfig("127.0.0.1", 8765, TEST_TOKEN, public))
                self.assertIn(host, settings.allowed_hosts)
                self.assertIn(origin, settings.allowed_origins)

    def test_http_app_builds_for_any_bind_address(self):
        for host in ["127.0.0.1", "0.0.0.0", "::", "::1"]:
            with self.subTest(host=host):
                server = make_server(http=HttpConfig(host, 8765, TEST_TOKEN))
                self.assertIsNotNone(server.settings.auth)
                self.assertFalse(server.settings.auth.validate_token_resource)
                server.streamable_http_app()


if __name__ == "__main__":
    unittest.main()

"""
Streamable HTTP mode: app/mcp_server.py --http on a free loopback port, fake stack.
Requests go straight to 127.0.0.1 (trust_env=False, so no proxy settings apply).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import asyncio
import unittest

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .helpers import TEST_TOKEN, HttpServer, run_server, server_env

INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "http-test", "version": "0"}},
}
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def post_initialize(url, headers=None):
    with httpx.Client(trust_env=False, timeout=30) as client:
        return client.post(url, json=INITIALIZE, headers={**MCP_HEADERS, **(headers or {})})


class RefusesToStartTest(unittest.TestCase):
    def check_refusal(self, args, env, message):
        result = run_server(["--http", *args], env)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("refusing to start", result.stderr)
        self.assertIn(message, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_without_a_token(self):
        self.check_refusal([], server_env(), "secrets.token_urlsafe(32)")
        self.check_refusal(["--host", "0.0.0.0"], server_env(), "DEWEY_MCP_TOKEN")

    def test_with_a_short_token(self):
        self.check_refusal([], server_env(DEWEY_MCP_TOKEN="short"), "at least 32")

    def test_no_auth_on_a_non_loopback_host(self):
        self.check_refusal(["--host", "0.0.0.0", "--no-auth"], server_env(DEWEY_MCP_TOKEN=TEST_TOKEN), "loopback")

    def test_no_auth_behind_a_reverse_proxy(self):
        self.check_refusal(["--no-auth", "--public-url", "https://dewey.example.org/mcp"], server_env(), "--public-url")


async def use_tools_with_sdk_client(url, token):
    """The SDK client, sending the token the way docs/MCP.md tells MCP clients to."""
    async with httpx.AsyncClient(trust_env=False, headers={"Authorization": f"Bearer {token}"},
                                 timeout=httpx.Timeout(30, read=120)) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                search = await session.call_tool("search_archive", {"question": "parking", "start_date": "2023-06-01"})
                ask = await session.call_tool("ask_archive", {"question": "budget?"})
                return tools, search, ask


class TokenAuthTest(unittest.TestCase):
    def test_token_required(self):
        with HttpServer([], server_env(DEWEY_MCP_TOKEN=TEST_TOKEN)) as server:
            for headers in [None, {"Authorization": "Bearer wrong-" + "x" * 40},
                            {"Authorization": TEST_TOKEN}, {"Authorization": f"Basic {TEST_TOKEN}"}]:
                with self.subTest(headers=headers):
                    response = post_initialize(server.url, headers)
                    self.assertEqual(response.status_code, 401)
                    self.assertTrue(response.headers["www-authenticate"].startswith("Bearer"))
                    self.assertNotIn("mcp-session-id", response.headers)

            response = post_initialize(server.url, {"Authorization": f"Bearer {TEST_TOKEN}"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("mcp-session-id", response.headers)

            tools, search, ask = asyncio.run(use_tools_with_sdk_client(server.url, TEST_TOKEN))
            self.assertEqual(tools, {"search_archive", "ask_archive"})
            self.assertFalse(search.isError)
            self.assertEqual([r["url"] for r in search.structuredContent["result"]],
                             ["https://news.example.org/2023/09/parking-fees"])
            self.assertFalse(ask.isError)
            self.assertIn("[[1]](https://news.example.org/", ask.structuredContent["answer"])
            self.assertIn("bearer token required", server.stderr())


class HostHeaderTest(unittest.TestCase):
    """DNS-rebinding protection on loopback: only local Host headers, plus the --public-url host."""

    def test_public_url_host_is_allowed(self):
        auth = {"Authorization": f"Bearer {TEST_TOKEN}"}
        with HttpServer(["--public-url", "https://dewey.example.org/mcp"], server_env(DEWEY_MCP_TOKEN=TEST_TOKEN)) as server:
            self.assertEqual(post_initialize(server.url, {**auth, "Host": "dewey.example.org"}).status_code, 200)
            self.assertEqual(post_initialize(server.url, {**auth, "Host": "evil.example.net"}).status_code, 421)
            self.assertIn("public URL https://dewey.example.org/mcp", server.stderr())

    def test_without_public_url_only_local_hosts(self):
        auth = {"Authorization": f"Bearer {TEST_TOKEN}"}
        with HttpServer([], server_env(DEWEY_MCP_TOKEN=TEST_TOKEN)) as server:
            self.assertEqual(post_initialize(server.url, {**auth, "Host": f"localhost:{server.port}"}).status_code, 200)
            self.assertEqual(post_initialize(server.url, {**auth, "Host": "dewey.example.org"}).status_code, 421)


class NonLoopbackBindTest(unittest.TestCase):
    """--host 0.0.0.0: no Host-header check (other machines use other names), so the token is the only guard."""

    def test_token_enforced_on_every_interface(self):
        auth = {"Authorization": f"Bearer {TEST_TOKEN}"}
        with HttpServer(["--host", "0.0.0.0"], server_env(DEWEY_MCP_TOKEN=TEST_TOKEN)) as server:
            self.assertEqual(post_initialize(server.url).status_code, 401)
            self.assertEqual(post_initialize(server.url, {"Host": "dewey.lan:8765"}).status_code, 401)
            self.assertEqual(post_initialize(server.url, {**auth, "Host": "dewey.lan:8765"}).status_code, 200)
            self.assertIn("TLS reverse proxy", server.stderr())


class NoAuthTest(unittest.TestCase):
    def test_no_auth_on_loopback(self):
        with HttpServer(["--no-auth"], server_env()) as server:
            response = post_initialize(server.url)
            self.assertEqual(response.status_code, 200)
            self.assertIn("NO AUTHENTICATION", server.stderr())


if __name__ == "__main__":
    unittest.main()

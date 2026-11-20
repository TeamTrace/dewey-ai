"""
End to end over stdio: app/mcp_server.py runs as a subprocess with the fake stack
(DEWEY_MCP_TEST_STACK=1), the way Hermes Agent or Claude Code would start it.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import json
import queue
import subprocess
import tempfile
import threading
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .fake_stack import STRAY_STDOUT_MARKER
from .helpers import PYTHON, REPO_ROOT, SERVER, run_server, server_env

TIMEOUT = 60


class StdioClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_tools_over_stdio(self):
        params = StdioServerParameters(command=PYTHON, args=[str(SERVER)], env=server_env(), cwd=str(REPO_ROOT))
        with tempfile.TemporaryFile("w+") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    self.assertEqual(init.serverInfo.name, "dewey")
                    self.assertIn("untrusted", init.instructions)

                    tools = {t.name: t for t in (await session.list_tools()).tools}
                    self.assertEqual(set(tools), {"search_archive", "ask_archive"})
                    self.assertTrue(tools["search_archive"].annotations.readOnlyHint)

                    result = await session.call_tool(
                        "search_archive", {"question": "budget", "end_date": "2023-06-30", "authors": ["Jane Doe"]}
                    )
                    self.assertFalse(result.isError)
                    rows = result.structuredContent["result"]
                    self.assertEqual([(r["source_id"], r["publish_date"]) for r in rows],
                                     [("SRC1", "2023-03-01"), ("SRC2", "1999-05-05")])
                    self.assertEqual(json.loads(result.content[0].text), rows)

                    result = await session.call_tool("ask_archive", {"question": "What happened with the budget?"})
                    self.assertFalse(result.isError)
                    answer = result.structuredContent["answer"]
                    self.assertTrue(answer.startswith("The council approved the budget [[1]](https://news.example.org/"))
                    # The last streamed delta made it into the returned answer.
                    self.assertTrue(answer.endswith("Final sentence cites a missing source [[9]](#)."))
                    self.assertEqual(len(result.structuredContent["sources"]), 3)

                    result = await session.call_tool("search_archive", {"question": "q", "start_date": "last week"})
                    self.assertTrue(result.isError)
                    self.assertIn("YYYY-MM-DD", result.content[0].text)

                    result = await session.call_tool("search_archive", {"question": "explode"})
                    self.assertTrue(result.isError)
                    # The exception's text is logged on stderr, not sent to the client.
                    self.assertNotIn("fake retriever failure", result.content[0].text)
                    self.assertIn("problem on the Dewey server", result.content[0].text)

                    # Still serving after a failed call.
                    result = await session.call_tool("search_archive", {"question": "nothing"})
                    self.assertEqual(result.structuredContent, {"result": []})
            errlog.seek(0)
            stderr = errlog.read()
        self.assertIn("FAKE test stack", stderr)
        self.assertIn(STRAY_STDOUT_MARKER, stderr)
        # The failed call's details are in the server's log (the log handler wraps long lines).
        self.assertIn("Dewey tool call failed", stderr)
        self.assertIn("RuntimeError", stderr)

    async def test_started_like_an_agent_does(self):
        # Agents start the server from their own folder, with only the variables in their config.
        env = {"PATH": server_env()["PATH"], "DEWEY_MCP_TEST_STACK": "1", "DEWEY_MCP_TOKEN": "", "PYTHONUNBUFFERED": "1"}
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryFile("w+") as errlog:
            params = StdioServerParameters(command=PYTHON, args=[str(SERVER)], env=env, cwd=cwd)
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("ask_archive", {"question": "What happened with the budget?"})
            self.assertFalse(result.isError, result.content)
            self.assertIn("[[1]](https://news.example.org/", result.structuredContent["answer"])


class RawStdoutTest(unittest.TestCase):
    """Speak JSON-RPC by hand and check that every line on stdout is a JSON-RPC message."""

    def test_stray_prints_never_reach_stdout(self):
        proc = subprocess.Popen(
            # server_env sets PYTHONUNBUFFERED=1 so a stray write reaches the pipe at once. Without it,
            # a small unflushed write can be lost at exit (the SDK's own stdout wrapper closes the
            # shared buffer), and this test could not see it; a large one would corrupt the stream.
            [PYTHON, str(SERVER)], env=server_env(), cwd=REPO_ROOT, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        lines: "queue.Queue[str]" = queue.Queue()
        stderr_chunks = []
        readers = [
            threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True),
            threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True),
        ]
        for reader in readers:
            reader.start()

        def send(message):
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()

        def wait_for(request_id):
            while True:
                line = lines.get(timeout=TIMEOUT)
                received.append(line)
                message = json.loads(line)  # fails the test on any non-JSON line
                if message.get("id") == request_id:
                    return message

        received = []
        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "raw-test", "version": "0"}}})
            self.assertIn("result", wait_for(1))
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "search_archive", "arguments": {"question": "budget"}}})
            search = wait_for(2)
            send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "ask_archive", "arguments": {"question": "budget?"}}})
            ask = wait_for(3)
        finally:
            proc.stdin.close()  # EOF on stdin: the server shuts down
            proc.wait(timeout=TIMEOUT)
            for reader in readers:
                reader.join(TIMEOUT)
            proc.stdout.close()
            proc.stderr.close()
        # Everything else the server wrote before exiting (late or buffered output included).
        while not lines.empty():
            received.append(lines.get_nowait())

        self.assertFalse(search["result"]["isError"])
        self.assertEqual(len(search["result"]["structuredContent"]["result"]), 3)
        self.assertIn("[[1]](https://news.example.org/", ask["result"]["structuredContent"]["answer"])
        for line in received:
            self.assertEqual(json.loads(line)["jsonrpc"], "2.0")
            self.assertNotIn(STRAY_STDOUT_MARKER, line)
        # Not lost, just moved: the fake stack's prints arrived on stderr.
        self.assertIn(STRAY_STDOUT_MARKER, "".join(stderr_chunks))


class StartupTest(unittest.TestCase):
    def test_bad_configuration_exits_with_a_message_on_stderr(self):
        # The test hook is off here, so this is the real build_stack() refusing DEWEY_STACK.
        result = run_server([], server_env(fake_stack=False, DEWEY_STACK="bogus"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("DEWEY_STACK must be one of", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_test_hook_needs_exactly_1(self):
        result = run_server([], server_env(fake_stack=False, DEWEY_STACK="bogus", DEWEY_MCP_TEST_STACK="true"))
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("FAKE", result.stderr)


if __name__ == "__main__":
    unittest.main()

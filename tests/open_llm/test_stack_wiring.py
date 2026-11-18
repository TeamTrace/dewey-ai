"""build_stack() -> OpenLLM -> HTTP request, for the open stack's startup checks (no network).

The end-to-end test uses an httpx.MockTransport, so the client really targets openrouter.ai
(OpenRouter detection runs on the configured URL, not a test override) but nothing leaves the process.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import contextlib
import io
import json
import os
import unittest
from unittest import mock

import httpx
import openai

import providers
from models import NewsroomConfig
from providers import open_llm
from providers.open_llm import DEFAULT_BASE_URL, OpenRouterRoutingError, is_openrouter_url

from .mock_server import ANSWER_DELTAS, METADATA, MockChatServer, chunk, completion

NEWSROOM = NewsroomConfig(publisher="The Example Times")
DATE = "Wednesday, November 18, 2026"
QUESTION = [{"role": "user", "content": "What did the school board decide about the budget?"}]
MODEL = "example-org/example-model"
BASE_ENV = {"DEWEY_STACK": "open", "OPEN_LLM_MODEL": MODEL, "DATABASE_URL": "postgresql://u:p@localhost/dewey"}


def build(**env):
    """build_stack() for the open stack with the Postgres store stubbed out; returns (llm, stderr text)."""
    err = io.StringIO()
    with mock.patch.dict(os.environ, {**BASE_ENV, **env}, clear=True), \
            mock.patch.object(providers, "build_pgvector_store", return_value=object()), \
            contextlib.redirect_stderr(err):
        llm, _ = providers.build_stack(NEWSROOM)
    return llm, err.getvalue()


class StartupTest(unittest.TestCase):
    def test_blank_base_url_means_the_ollama_default(self):
        # install.py writes OPEN_LLM_BASE_URL='' when the user keeps the Ollama default.
        for blank in ("", "   "):
            with self.subTest(value=blank):
                llm, _ = build(OPEN_LLM_BASE_URL=blank)
                self.assertEqual(str(llm.client.base_url).rstrip("/"), DEFAULT_BASE_URL.rstrip("/"))
                self.assertFalse(llm.openrouter)

    def test_base_url_whitespace_is_trimmed(self):
        llm, _ = build(OPEN_LLM_BASE_URL="  https://openrouter.ai/api/v1  ", OPEN_LLM_API_KEY="sk-or-test")
        self.assertTrue(llm.openrouter)
        self.assertEqual(str(llm.client.base_url), "https://openrouter.ai/api/v1/")

    def test_openrouter_over_plain_http_is_refused(self):
        # The API key and the journalists' questions would cross the network unencrypted.
        with self.assertRaises(providers.StackConfigError) as ctx:
            build(OPEN_LLM_BASE_URL="http://openrouter.ai/api/v1", OPEN_LLM_API_KEY="sk-or-test")
        self.assertIn("https://", str(ctx.exception))
        self.assertNotIn("sk-or-test", str(ctx.exception))

    def test_trailing_dot_host_is_still_openrouter(self):
        # "openrouter.ai." resolves to the same host; treating it as "not OpenRouter" would drop the privacy prefs.
        self.assertTrue(is_openrouter_url("https://openrouter.ai./api/v1"))
        self.assertTrue(is_openrouter_url("https://eu.openrouter.ai./api/v1"))
        self.assertFalse(is_openrouter_url("https://openrouter.ai.example.com./v1"))

    def test_privacy_setting_that_cannot_apply_is_reported(self):
        # E.g. OpenRouter behind the newsroom's own gateway: Dewey can't tell, so it must say the setting is unused.
        llm, err = build(OPEN_LLM_BASE_URL="https://llm-gateway.example.org/v1", OPENROUTER_PRIVACY="strict")
        self.assertFalse(llm.openrouter)
        self.assertIn("OPENROUTER_PRIVACY", err)
        self.assertIn("ignored", err)

    def test_no_warning_without_the_setting(self):
        _, err = build(OPEN_LLM_BASE_URL="http://localhost:8000/v1")
        self.assertEqual(err, "")
        _, err = build(OPEN_LLM_BASE_URL="https://openrouter.ai/api/v1", OPEN_LLM_API_KEY="k",
                       OPENROUTER_PRIVACY="standard")
        self.assertEqual(err, "")


class OverTheWireTest(unittest.TestCase):
    """The configured OpenRouter URL, detected for real, produces these HTTP requests."""

    def setUp(self):
        self.requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.requests.append((request, body))
            if body.get("stream"):
                events = [chunk(d) for d in ANSWER_DELTAS] + [chunk(None, "stop")]
                sse = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=sse.encode())
            return httpx.Response(200, json=completion(json.dumps(METADATA)))

        real_client = openai.OpenAI
        transport = httpx.MockTransport(handler)
        patcher = mock.patch.object(
            open_llm.openai, "OpenAI",
            lambda **kw: real_client(**kw, max_retries=0, http_client=httpx.Client(transport=transport)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_build_stack_sends_privacy_preferences_to_openrouter(self):
        llm, _ = build(OPEN_LLM_BASE_URL="https://openrouter.ai/api/v1", OPEN_LLM_API_KEY="sk-or-test")
        self.assertEqual(llm.generate_metadata(QUESTION, DATE), METADATA)
        self.assertEqual("".join(llm.stream_answer(QUESTION, DATE)), "".join(ANSWER_DELTAS))

        (planning_req, planning), (answer_req, answer) = self.requests
        for req in (planning_req, answer_req):
            self.assertEqual(req.url.scheme, "https")
            self.assertEqual(req.url.host, "openrouter.ai")
            self.assertEqual(req.url.path, "/api/v1/chat/completions")
            self.assertEqual(req.headers["Authorization"], "Bearer sk-or-test")
        self.assertEqual(planning["provider"], {"data_collection": "deny", "zdr": True, "require_parameters": True})
        self.assertEqual(planning["response_format"]["type"], "json_schema")
        self.assertEqual(answer["provider"], {"data_collection": "deny", "zdr": True})

    def test_build_stack_sends_nothing_extra_to_other_servers(self):
        llm, _ = build(OPEN_LLM_BASE_URL="http://localhost:8000/v1")
        llm.generate_metadata(QUESTION, DATE)
        list(llm.stream_answer(QUESTION, DATE))
        (_, planning), (_, answer) = self.requests
        self.assertEqual(set(planning), {"model", "messages", "response_format"})
        self.assertEqual(set(answer), {"model", "messages", "stream"})


class FallbackRetryTest(unittest.TestCase):
    def test_routing_error_on_the_json_mode_retry_is_still_explained(self):
        # A non-routing 400 starts the JSON-mode retry; if that retry can't be routed, say so (don't return junk).
        with MockChatServer() as server, contextlib.redirect_stderr(io.StringIO()):
            server.fail(400, "Provider returned error: invalid schema for response_format")
            server.fail(404, "No endpoints found that can handle the requested parameters.")
            llm = open_llm.OpenLLM(NEWSROOM, model=MODEL, client=server.client(), openrouter=True)
            with self.assertRaises(OpenRouterRoutingError) as ctx:
                llm.generate_metadata(QUESTION, DATE)
        self.assertEqual([b["response_format"]["type"] for b in server.bodies()], ["json_schema", "json_object"])
        self.assertIn("require_parameters=true", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

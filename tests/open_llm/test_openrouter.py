"""OpenRouter support in the open stack's LLM provider, against a local mock server (no network).

The mock server stands in for OpenRouter: OpenLLM is pointed at 127.0.0.1 with openrouter=True,
so the request bodies it records are the ones OpenRouter would receive.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import contextlib
import io
import logging
import os
import unittest
from unittest import mock

import openai

import providers
from models import NewsroomConfig
from providers.open_llm import (
    DEFAULT_OPENROUTER_PRIVACY, OPENROUTER_STRUCTURED_MODELS_URL, OpenLLM, OpenRouterRoutingError,
    is_openrouter_url, parse_openrouter_privacy,
)

from .mock_server import ANSWER_DELTAS, METADATA, MockChatServer, completion

NEWSROOM = NewsroomConfig(publisher="The Example Times")
DATE = "Wednesday, November 18, 2026"
QUESTION = [{"role": "user", "content": "What did the school board decide about the budget?"}]
MODEL = "example-org/example-model"

STRICT = {"data_collection": "deny", "zdr": True}
STANDARD = {"data_collection": "deny"}


def make_llm(server: MockChatServer, openrouter=True, privacy=None) -> OpenLLM:
    return OpenLLM(NEWSROOM, model=MODEL, client=server.client(), openrouter=openrouter, privacy=privacy)


def run_both_steps(llm: OpenLLM):
    """Search planning, then the streamed answer, the way Dewey.process() calls them."""
    metadata = llm.generate_metadata(QUESTION, DATE)
    answer = "".join(llm.stream_answer(QUESTION, DATE))
    return metadata, answer


_httpx_logger = logging.getLogger("httpx")
_httpx_level = _httpx_logger.level


def setUpModule():
    # Other test modules may configure logging at INFO; httpx would then log every mock request.
    _httpx_logger.setLevel(logging.WARNING)


def tearDownModule():
    _httpx_logger.setLevel(_httpx_level)


@contextlib.contextmanager
def quiet_stderr():
    with contextlib.redirect_stderr(io.StringIO()) as err:
        yield err


class DetectionTest(unittest.TestCase):
    def test_openrouter_hosts(self):
        for url in ("https://openrouter.ai/api/v1", "https://OpenRouter.ai/api/v1/", "https://eu.openrouter.ai/api/v1"):
            self.assertTrue(is_openrouter_url(url), url)

    def test_other_hosts(self):
        for url in ("http://localhost:11434/v1", "http://127.0.0.1:8000/v1", "https://notopenrouter.ai/api/v1",
                    "https://openrouter.ai.example.com/v1", "http://ollama:11434/v1?x=openrouter.ai", "", None):
            self.assertFalse(is_openrouter_url(url), url)

    def test_detected_from_base_url_without_a_request(self):
        llm = OpenLLM(NEWSROOM, model=MODEL, base_url="https://openrouter.ai/api/v1", api_key="k")
        self.assertTrue(llm.openrouter)
        self.assertEqual(llm.privacy, "strict")
        self.assertFalse(OpenLLM(NEWSROOM, model="qwen3:8b").openrouter)  # default: local Ollama

    def test_detected_from_an_injected_client(self):
        client = openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key="k")
        self.assertTrue(OpenLLM(NEWSROOM, model=MODEL, client=client).openrouter)

    def test_explicit_override_wins(self):
        llm = OpenLLM(NEWSROOM, model=MODEL, base_url="https://openrouter.ai/api/v1", api_key="k", openrouter=False)
        self.assertFalse(llm.openrouter)
        self.assertIsNone(llm.provider_preferences(require_parameters=True))


class PrivacySettingTest(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(DEFAULT_OPENROUTER_PRIVACY, "strict")
        self.assertEqual(parse_openrouter_privacy(None), "strict")
        self.assertEqual(parse_openrouter_privacy("  "), "strict")
        self.assertEqual(parse_openrouter_privacy(" Standard "), "standard")
        self.assertEqual(parse_openrouter_privacy("OFF"), "off")

    def test_invalid_level_names_the_choices(self):
        with self.assertRaises(ValueError) as ctx:
            parse_openrouter_privacy("maximum")
        self.assertIn("strict, standard, off", str(ctx.exception))
        with self.assertRaises(ValueError):
            OpenLLM(NEWSROOM, model=MODEL, base_url="https://openrouter.ai/api/v1", api_key="k", privacy="maximum")

    def test_ignored_for_other_servers(self):
        llm = OpenLLM(NEWSROOM, model="qwen3:8b", privacy="maximum")
        self.assertIsNone(llm.privacy)


class RequestBodyTest(unittest.TestCase):
    def assert_steps_work(self, metadata, answer):
        self.assertEqual(metadata, METADATA)
        self.assertEqual(answer, "".join(ANSWER_DELTAS))

    def test_other_servers_get_no_provider_object_and_unchanged_requests(self):
        with MockChatServer() as server:
            self.assert_steps_work(*run_both_steps(make_llm(server, openrouter=False, privacy="strict")))
        planning, answer = server.bodies()
        self.assertEqual(set(planning), {"model", "messages", "response_format"})
        self.assertEqual(planning["response_format"]["type"], "json_schema")
        self.assertEqual(set(answer), {"model", "messages", "stream"})

    def test_strict_is_the_default(self):
        with MockChatServer() as server:
            self.assert_steps_work(*run_both_steps(make_llm(server)))
        planning, answer = server.bodies()
        self.assertEqual(planning["provider"], {**STRICT, "require_parameters": True})
        self.assertEqual(planning["response_format"]["type"], "json_schema")
        self.assertEqual(answer["provider"], STRICT)
        self.assertTrue(answer["stream"])
        self.assertEqual(server.requests[0]["headers"].get("Authorization"), "Bearer test-key")

    def test_each_privacy_level(self):
        cases = {
            "strict": ({**STRICT, "require_parameters": True}, STRICT),
            "standard": ({**STANDARD, "require_parameters": True}, STANDARD),
            "off": ({"require_parameters": True}, None),
        }
        for level, (planning_prefs, answer_prefs) in cases.items():
            with self.subTest(level=level), MockChatServer() as server:
                self.assert_steps_work(*run_both_steps(make_llm(server, privacy=level)))
                planning, answer = server.bodies()
                self.assertEqual(planning["provider"], planning_prefs)
                if answer_prefs is None:
                    self.assertNotIn("provider", answer)
                else:
                    self.assertEqual(answer["provider"], answer_prefs)

    def test_require_parameters_only_on_search_planning(self):
        with MockChatServer() as server:
            run_both_steps(make_llm(server, privacy="standard"))
        planning, answer = server.bodies()
        self.assertIs(planning["provider"].get("require_parameters"), True)
        self.assertNotIn("require_parameters", answer["provider"])

    def test_json_mode_fallback_keeps_the_preferences(self):
        # A 400 that isn't about routing (e.g. a provider rejecting the schema) still falls back to JSON mode.
        with MockChatServer() as server, quiet_stderr():
            server.fail(400, "Provider returned error: invalid schema for response_format")
            metadata = make_llm(server).generate_metadata(QUESTION, DATE)
        self.assertEqual(metadata, METADATA)
        first, second = server.bodies()
        self.assertEqual(first["response_format"]["type"], "json_schema")
        self.assertEqual(second["response_format"], {"type": "json_object"})
        self.assertEqual(second["provider"], {**STRICT, "require_parameters": True})


class RoutingErrorTest(unittest.TestCase):
    def test_no_endpoint_for_the_privacy_and_parameter_preferences(self):
        with MockChatServer() as server:
            server.fail(404, "No endpoints found that can handle the requested parameters.")
            with self.assertRaises(OpenRouterRoutingError) as ctx:
                make_llm(server).generate_metadata(QUESTION, DATE)
            self.assertEqual(len(server.requests), 1, "a routing error must not trigger the JSON-mode retry")
        message = str(ctx.exception)
        for expected in ("search-planning", MODEL, "HTTP 404", "No endpoints found that can handle",
                         'OPENROUTER_PRIVACY is "strict"', 'OPENROUTER_PRIVACY="standard"', "zdr=true",
                         "data_collection=deny", "require_parameters=true", "structured outputs",
                         OPENROUTER_STRUCTURED_MODELS_URL, "OPEN_LLM_MODEL"):
            self.assertIn(expected, message)
        self.assertIsInstance(ctx.exception.__cause__, openai.NotFoundError)

    def test_routing_400_is_not_masked_by_the_json_mode_fallback(self):
        with MockChatServer() as server, quiet_stderr() as err:
            server.fail(400, "No endpoints found matching your data policy (Zero data retention).")
            with self.assertRaises(OpenRouterRoutingError):
                make_llm(server).generate_metadata(QUESTION, DATE)
            self.assertEqual(len(server.requests), 1)
        self.assertNotIn("retrying with JSON mode", err.getvalue())

    def test_503_no_available_provider(self):
        with MockChatServer() as server:
            server.fail(503, "No available model provider meets your routing requirements.")
            with self.assertRaises(OpenRouterRoutingError) as ctx:
                make_llm(server, privacy="standard").generate_metadata(QUESTION, DATE)
        message = str(ctx.exception)
        self.assertIn('OPENROUTER_PRIVACY is "standard"', message)
        self.assertIn('OPENROUTER_PRIVACY="off"', message)
        self.assertNotIn("zdr=true", message)
        self.assertIn("temporarily unavailable", message)

    def test_overloaded_provider_is_not_a_routing_error(self):
        with MockChatServer() as server:
            server.fail(503, "Provider is overloaded", error_type="provider_overloaded")
            with self.assertRaises(openai.InternalServerError):
                make_llm(server).generate_metadata(QUESTION, DATE)

    def test_answer_step(self):
        with MockChatServer() as server:
            server.fail(404, "No allowed providers are available for the selected model.")
            with self.assertRaises(OpenRouterRoutingError) as ctx:
                list(make_llm(server).stream_answer(QUESTION, DATE))
            self.assertTrue(server.bodies()[0]["stream"])
        message = str(ctx.exception)
        self.assertIn("answer request", message)
        self.assertNotIn("require_parameters", message)
        self.assertIn("Zero Data Retention", message)

    def test_privacy_off_says_no_preferences_were_sent(self):
        with MockChatServer() as server:
            server.fail(404, "No allowed providers are available for the selected model.")
            with self.assertRaises(OpenRouterRoutingError) as ctx:
                list(make_llm(server, privacy="off").stream_answer(QUESTION, DATE))
        message = str(ctx.exception)
        self.assertIn("Dewey sent no provider preferences.", message)
        self.assertNotIn('OPENROUTER_PRIVACY="', message)  # nothing left to relax

    def test_other_servers_keep_their_original_errors(self):
        with MockChatServer() as server:
            server.fail(404, "model 'qwen3:8b' not found")
            with self.assertRaises(openai.NotFoundError):
                make_llm(server, openrouter=False).generate_metadata(QUESTION, DATE)

    def test_other_servers_still_fall_back_to_json_mode_on_any_400(self):
        with MockChatServer() as server, quiet_stderr():
            server.fail(400, "No endpoints found, json_schema unsupported")
            self.assertEqual(make_llm(server, openrouter=False).generate_metadata(QUESTION, DATE), METADATA)
        self.assertEqual([b["response_format"]["type"] for b in server.bodies()], ["json_schema", "json_object"])

    def test_auth_and_credit_errors_pass_through(self):
        for status, error in ((401, openai.AuthenticationError), (402, openai.APIStatusError),
                              (429, openai.RateLimitError)):
            with self.subTest(status=status), MockChatServer() as server:
                server.fail(status, "nope")
                with self.assertRaises(error) as ctx:
                    make_llm(server).generate_metadata(QUESTION, DATE)
                self.assertNotIsInstance(ctx.exception, OpenRouterRoutingError)

    def test_error_body_with_status_200(self):
        with MockChatServer() as server:
            server.reply(200, {"id": "gen-1", "error": {"code": 502, "message": "Upstream provider failed"}})
            with self.assertRaises(RuntimeError) as ctx:
                make_llm(server).generate_metadata(QUESTION, DATE)
        self.assertIn("Upstream provider failed", str(ctx.exception))

    def test_empty_reply_still_validates_on_other_servers(self):
        with MockChatServer() as server, quiet_stderr():
            server.reply(200, completion(None))
            metadata = make_llm(server, openrouter=False).generate_metadata(QUESTION, DATE)
        self.assertEqual(metadata["question"], QUESTION[0]["content"])  # unfiltered-search fallback


class BuildStackTest(unittest.TestCase):
    BASE_ENV = {"DEWEY_STACK": "open", "OPEN_LLM_MODEL": MODEL, "DATABASE_URL": "postgresql://u:p@localhost/dewey"}

    def build(self, **env):
        with mock.patch.dict(os.environ, {**self.BASE_ENV, **env}, clear=True), \
                mock.patch.object(providers, "build_pgvector_store", return_value=object()):
            llm, _ = providers.build_stack(NEWSROOM)
        return llm

    def test_openrouter_base_url(self):
        llm = self.build(OPEN_LLM_BASE_URL="https://openrouter.ai/api/v1", OPEN_LLM_API_KEY="sk-or-test",
                         OPENROUTER_PRIVACY="standard")
        self.assertTrue(llm.openrouter)
        self.assertEqual(llm.privacy, "standard")
        self.assertEqual(llm.provider_preferences(), STANDARD)

    def test_openrouter_defaults_to_strict(self):
        llm = self.build(OPEN_LLM_BASE_URL="https://openrouter.ai/api/v1", OPEN_LLM_API_KEY="sk-or-test")
        self.assertEqual(llm.provider_preferences(require_parameters=True), {**STRICT, "require_parameters": True})

    def test_openrouter_needs_a_key(self):
        with self.assertRaises(providers.StackConfigError) as ctx:
            self.build(OPEN_LLM_BASE_URL="https://openrouter.ai/api/v1")
        self.assertIn("OPEN_LLM_API_KEY", str(ctx.exception))

    def test_invalid_privacy_is_a_config_error(self):
        with self.assertRaises(providers.StackConfigError) as ctx:
            self.build(OPEN_LLM_BASE_URL="https://openrouter.ai/api/v1", OPEN_LLM_API_KEY="k",
                       OPENROUTER_PRIVACY="paranoid")
        self.assertIn("OPENROUTER_PRIVACY", str(ctx.exception))

    def test_local_server_unchanged(self):
        with quiet_stderr() as err:
            llm = self.build(OPENROUTER_PRIVACY="paranoid")  # ignored (with a warning): not OpenRouter
        self.assertIn("OPENROUTER_PRIVACY is set but ignored", err.getvalue())
        self.assertFalse(llm.openrouter)
        self.assertIsNone(llm.provider_preferences(require_parameters=True))


if __name__ == "__main__":
    unittest.main()

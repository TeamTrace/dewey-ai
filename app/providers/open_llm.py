"""
Open-weight chat model behind an OpenAI-compatible API (DEWEY_STACK=open).

Works with any server that implements `/v1/chat/completions`, e.g. Ollama
(default URL http://localhost:11434/v1, MIT-licensed), vLLM or llama.cpp's
server, or the hosted OpenRouter API. Only the `openai` client library is used
here; nothing is sent to OpenAI.

Search planning asks for JSON matching `SearchMetadata`:
1. strict `response_format: json_schema` first;
2. if the server rejects that, plain JSON mode with the schema in the prompt.
Either way the reply is validated with Pydantic; anything invalid degrades to
an unfiltered search on the user's own words, like the Claude provider does.

Some open models (e.g. Qwen3) write their reasoning inside <think>...</think>
in the reply text. That text is stripped so it never reaches the JSON parser
or the journalist.

OpenRouter (base URL on openrouter.ai): every request carries OpenRouter
provider preferences (`"provider": {...}` in the request body). The privacy
part comes from OPENROUTER_PRIVACY:
    strict    data_collection="deny" and zdr=true   [default]
    standard  data_collection="deny"
    off       none (only the OpenRouter account's own settings apply)
The search-planning request also sets require_parameters=true, so OpenRouter
only routes it to endpoints that support json_schema output. When no endpoint
meets these preferences, OpenRouterRoutingError says what to change; that
error never triggers the JSON-mode fallback. Other servers receive exactly the
requests they did before OpenRouter support was added.
"""
import json
import re
import sys
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlsplit

import openai
from pydantic import ValidationError

from models import NewsroomConfig, SearchMetadata
from tools import load_answer_prompt, load_search_prompt

from .base import UserFacingError

DEFAULT_BASE_URL = "http://localhost:11434/v1"  # Ollama
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# OPENROUTER_PRIVACY level -> privacy keys of OpenRouter's "provider" preferences.
OPENROUTER_PRIVACY_LEVELS: Dict[str, Dict[str, Any]] = {
    "strict": {"data_collection": "deny", "zdr": True},
    "standard": {"data_collection": "deny"},
    "off": {},
}
DEFAULT_OPENROUTER_PRIVACY = "strict"
OPENROUTER_STRUCTURED_MODELS_URL = "https://openrouter.ai/models?supported_parameters=structured_outputs"
OPENROUTER_PRIVACY_SETTINGS_URL = "https://openrouter.ai/settings/privacy"
# A 400 counts as a routing failure only when its message says no endpoint or
# provider was usable, or names a data policy; 404 and 503 always do.
ROUTING_MESSAGE_RE = re.compile(
    r"\bno\b[^.]*\b(endpoints?|providers?)\b|data polic|zero data retention|\bzdr\b", re.IGNORECASE
)


def is_openrouter_url(base_url: str) -> bool:
    """True for https://openrouter.ai/... and its subdomains (e.g. eu.openrouter.ai)."""
    # A trailing dot ("openrouter.ai.") is the same host in DNS, so it must not skip the privacy preferences.
    host = (urlsplit((base_url or "").strip()).hostname or "").lower().rstrip(".")
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def parse_openrouter_privacy(value: Optional[str]) -> str:
    level = (value or "").strip().lower() or DEFAULT_OPENROUTER_PRIVACY
    if level not in OPENROUTER_PRIVACY_LEVELS:
        raise ValueError(
            f"OPENROUTER_PRIVACY must be one of {', '.join(OPENROUTER_PRIVACY_LEVELS)}, got {value!r}"
        )
    return level


class OpenRouterRoutingError(UserFacingError, RuntimeError):
    """OpenRouter found no endpoint that meets the request's privacy and parameter preferences."""


def _error_body(e: openai.APIStatusError) -> Dict[str, Any]:
    return e.body if isinstance(e.body, dict) else {}


def _error_message(e: openai.APIStatusError) -> str:
    message = _error_body(e).get("message")
    return message if isinstance(message, str) and message else e.message


def is_routing_error(e: openai.APIStatusError) -> bool:
    """Does this OpenRouter error mean that no endpoint met the routing preferences?"""
    metadata = _error_body(e).get("metadata")
    if isinstance(metadata, dict) and metadata.get("error_type") == "provider_overloaded":
        return False  # 503, but a busy provider rather than a routing problem
    if e.status_code in (404, 503):
        return True
    return e.status_code == 400 and bool(ROUTING_MESSAGE_RE.search(_error_message(e)))


def to_chat_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Drop Gradio's extra keys and empty step messages."""
    return [
        {"role": m["role"], "content": m["content"]}
        for m in messages
        if m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str)
        and m["content"].strip()
    ]


FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def strip_think(text: str) -> str:
    return THINK_RE.sub("", text).strip()


def extract_json(text: str) -> str:
    """Remove <think> blocks and a surrounding ```json fence, which some models add in JSON mode."""
    text = strip_think(text)
    match = FENCE_RE.match(text)
    return match.group(1) if match else text


class ThinkFilter:
    """Streaming version of strip_think(): drops text inside <think>...</think>."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buffer = ""
        self.in_think = False

    def feed(self, delta: str) -> str:
        self.buffer += delta
        out = []
        while True:
            tag = self.CLOSE if self.in_think else self.OPEN
            i = self.buffer.find(tag)
            if i >= 0:
                if not self.in_think:
                    out.append(self.buffer[:i])
                self.buffer = self.buffer[i + len(tag):]
                self.in_think = not self.in_think
                continue
            # Hold back a possible partial tag at the end of the buffer.
            keep = next((n for n in range(len(tag) - 1, 0, -1) if self.buffer.endswith(tag[:n])), 0)
            emit, self.buffer = self.buffer[:len(self.buffer) - keep], self.buffer[len(self.buffer) - keep:]
            if not self.in_think:
                out.append(emit)
            return "".join(out)

    def flush(self) -> str:
        rest, self.buffer = ("" if self.in_think else self.buffer), ""
        return rest


class OpenLLM:
    def __init__(self, newsroom: NewsroomConfig, model: str, base_url: str = DEFAULT_BASE_URL,
                 api_key: str = "", client: openai.OpenAI = None, openrouter: Optional[bool] = None,
                 privacy: Optional[str] = None):
        """`openrouter`: None detects OpenRouter from the base URL (of `client` when one is given);
        True/False overrides that, e.g. for tests against a local mock server.
        `privacy`: an OPENROUTER_PRIVACY level, checked only when talking to OpenRouter."""
        # Local servers ignore the key, but the client requires a non-empty value.
        self.client = client or openai.OpenAI(base_url=base_url, api_key=api_key or "not-needed")
        self.model = model
        self.newsroom = newsroom
        if openrouter is None:
            openrouter = is_openrouter_url(str(client.base_url) if client is not None else base_url)
        self.openrouter = openrouter
        self.privacy = parse_openrouter_privacy(privacy) if openrouter else None

    def provider_preferences(self, require_parameters: bool = False) -> Optional[Dict[str, Any]]:
        """OpenRouter's "provider" object for a request, or None to send none (always None off OpenRouter)."""
        if not self.openrouter:
            return None
        prefs = dict(OPENROUTER_PRIVACY_LEVELS[self.privacy])
        if require_parameters:
            prefs["require_parameters"] = True
        return prefs or None

    def _chat(self, step: str, require_parameters: bool = False, **kwargs):
        prefs = self.provider_preferences(require_parameters)
        if prefs:
            kwargs["extra_body"] = {"provider": prefs}
        try:
            return self.client.chat.completions.create(model=self.model, **kwargs)
        except openai.APIStatusError as e:
            if self.openrouter and is_routing_error(e):
                raise OpenRouterRoutingError(self._routing_help(e, step, prefs or {})) from e
            raise

    def _routing_help(self, e: openai.APIStatusError, step: str, prefs: Dict[str, Any]) -> str:
        asked = []
        if prefs.get("data_collection") == "deny":
            asked.append("providers that don't collect your data (data_collection=deny)")
        if prefs.get("zdr"):
            asked.append("Zero Data Retention endpoints (zdr=true)")
        if prefs.get("require_parameters"):
            asked.append("endpoints that support every request parameter, including json_schema "
                         "structured outputs (require_parameters=true)")
        lines = [
            f"OpenRouter could not route Dewey's {step} request for model {self.model!r} "
            f"(HTTP {e.status_code}): {_error_message(e)}",
            ("Dewey asked OpenRouter for: " + "; ".join(asked) + "."
             if asked else "Dewey sent no provider preferences."),
            f'OPENROUTER_PRIVACY is "{self.privacy}". To fix this, do one of:',
            "  - Check that OPEN_LLM_MODEL is the exact model id shown on openrouter.ai/models.",
        ]
        need = [label for key, label in (("require_parameters", "structured outputs"), ("zdr", "Zero Data Retention"))
                if prefs.get(key)]
        if need:
            lines.append(f"  - Pick a model with endpoints that offer {' and '.join(need)}: start from "
                         f"{OPENROUTER_STRUCTURED_MODELS_URL} and check the Providers section of the model's page.")
        if self.privacy == "strict":
            lines.append('  - If your newsroom\'s policy allows it, set OPENROUTER_PRIVACY="standard" in .env '
                         "(drops the Zero Data Retention requirement; keeps data_collection=deny).")
        elif self.privacy == "standard":
            lines.append('  - If your newsroom\'s policy allows it, set OPENROUTER_PRIVACY="off" in .env '
                         "(no per-request privacy preferences).")
        lines.append(f"Your OpenRouter account's privacy settings ({OPENROUTER_PRIVACY_SETTINGS_URL}) also apply, "
                     "and a request can't switch off account-wide Zero Data Retention.")
        if e.status_code == 503:
            lines.append("If this model worked before, its matching endpoints may be temporarily unavailable; "
                         "try again later.")
        return "\n".join(lines)

    def _reply_text(self, response) -> str:
        choices = getattr(response, "choices", None)
        if not choices and self.openrouter:
            # OpenRouter can answer 200 with only an `error` object when a provider fails mid-request.
            error = (getattr(response, "model_extra", None) or {}).get("error")
            message = error.get("message") if isinstance(error, dict) else error
            raise RuntimeError(f"OpenRouter returned an error instead of a reply: {message or 'no details'}")
        return choices[0].message.content or ""

    def _fallback_metadata(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return {"question": last_user, "date_range": {"start_date": None, "end_date": None}, "authors": []}

    def generate_metadata(self, messages: List[Dict[str, Any]], current_date: str) -> Dict[str, Any]:
        chat = to_chat_messages(messages)
        system = load_search_prompt(current_date, self.newsroom)
        schema = SearchMetadata.model_json_schema()

        try:
            response = self._chat(
                "search-planning",
                require_parameters=True,
                messages=[{"role": "system", "content": system}, *chat],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "search_archive", "schema": schema, "strict": True},
                },
            )
        except openai.BadRequestError as e:
            # OpenRouterRoutingError is not a BadRequestError, so routing failures are never masked here.
            print(f"Server rejected json_schema output ({e}); retrying with JSON mode.", file=sys.stderr)
            system += (
                "\n\nRespond with only a JSON object matching this JSON Schema, and nothing else:\n"
                + json.dumps(schema)
            )
            response = self._chat(
                "search-planning",
                require_parameters=True,
                messages=[{"role": "system", "content": system}, *chat],
                response_format={"type": "json_object"},
            )

        text = extract_json(self._reply_text(response))
        try:
            return SearchMetadata.model_validate_json(text).model_dump()
        except ValidationError as e:
            print(f"Search-planning output did not match the schema; falling back to unfiltered search. {e}",
                  file=sys.stderr)
            return self._fallback_metadata(chat)

    def stream_answer(self, messages: List[Dict[str, Any]], current_date: str) -> Iterator[str]:
        stream = self._chat(
            "answer",
            messages=[{"role": "system", "content": load_answer_prompt(current_date)}, *to_chat_messages(messages)],
            stream=True,
        )
        think = ThinkFilter()
        finish_reason = None
        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            finish_reason = choice.finish_reason or finish_reason
            if choice.delta and choice.delta.content:
                if text := think.feed(choice.delta.content):
                    yield text
        if text := think.flush():
            yield text
        if finish_reason == "length":
            yield "\n\n_This answer hit the length limit and is incomplete._"

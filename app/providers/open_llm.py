"""
Open-weight chat model served locally behind an OpenAI-compatible API
(DEWEY_STACK=open).

Works with any server that implements `/v1/chat/completions`, e.g. Ollama
(default URL http://localhost:11434/v1, MIT-licensed), vLLM or llama.cpp's
server. Only the `openai` client library is used here; nothing is sent to
OpenAI.

Search planning asks for JSON matching `SearchMetadata`:
1. strict `response_format: json_schema` first;
2. if the server rejects that, plain JSON mode with the schema in the prompt.
Either way the reply is validated with Pydantic; anything invalid degrades to
an unfiltered search on the user's own words, like the Claude provider does.

Some open models (e.g. Qwen3) write their reasoning inside <think>...</think>
in the reply text. That text is stripped so it never reaches the JSON parser
or the journalist.
"""
import json
import re
from typing import Any, Dict, Iterator, List

import openai
from pydantic import ValidationError

from models import NewsroomConfig, SearchMetadata
from tools import load_answer_prompt, load_search_prompt

DEFAULT_BASE_URL = "http://localhost:11434/v1"  # Ollama
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


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
                 api_key: str = "", client: openai.OpenAI = None):
        # Local servers ignore the key, but the client requires a non-empty value.
        self.client = client or openai.OpenAI(base_url=base_url, api_key=api_key or "not-needed")
        self.model = model
        self.newsroom = newsroom

    def _fallback_metadata(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return {"question": last_user, "date_range": {"start_date": None, "end_date": None}, "authors": []}

    def generate_metadata(self, messages: List[Dict[str, Any]], current_date: str) -> Dict[str, Any]:
        chat = to_chat_messages(messages)
        system = load_search_prompt(current_date, self.newsroom)
        schema = SearchMetadata.model_json_schema()

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, *chat],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "search_archive", "schema": schema, "strict": True},
                },
            )
        except openai.BadRequestError as e:
            print(f"Server rejected json_schema output ({e}); retrying with JSON mode.")
            system += (
                "\n\nRespond with only a JSON object matching this JSON Schema, and nothing else:\n"
                + json.dumps(schema)
            )
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, *chat],
                response_format={"type": "json_object"},
            )

        text = extract_json(response.choices[0].message.content or "")
        try:
            return SearchMetadata.model_validate_json(text).model_dump()
        except ValidationError as e:
            print(f"Search-planning output did not match the schema; falling back to unfiltered search. {e}")
            return self._fallback_metadata(chat)

    def stream_answer(self, messages: List[Dict[str, Any]], current_date: str) -> Iterator[str]:
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": load_answer_prompt(current_date, self.newsroom)}, *to_chat_messages(messages)],
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

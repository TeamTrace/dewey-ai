"""
Claude (Anthropic API) chat provider.

Differences from the Azure OpenAI provider, and why:
- Metadata extraction uses structured outputs (`messages.parse` + a Pydantic
  schema) instead of a forced tool call: current Claude models reject forced
  `tool_choice` with a 400.
- Requests opt into server-side refusal fallbacks (`fallbacks="default"`): if a
  safety classifier declines, the API retries on the model Anthropic
  recommends for that refusal category, inside the same call. That requires
  the beta endpoint and header below.
- `stop_reason == "refusal"` is checked before reading output, because a
  decline arrives as HTTP 200, not as an error.
"""
from typing import Any, Dict, Iterator, List, Optional

import anthropic

from models import NewsroomConfig, SearchMetadata
from tools import load_answer_prompt, load_search_prompt

FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_MODEL = "claude-opus-5"

REFUSAL_NOTICE = (
    "\n\n_Dewey's language model declined to answer this request. "
    "Try rephrasing the question._"
)
TRUNCATED_NOTICE = "\n\n_This answer hit the length limit and is incomplete._"


def to_claude_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """
    Keep only user/assistant text turns with non-empty content. Gradio history
    carries extra keys (`metadata`) and empty step messages; the Messages API
    rejects empty text content.
    """
    cleaned = []
    for m in messages:
        role, content = m.get("role"), m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        cleaned.append({"role": role, "content": content})
    # The Messages API requires the first message to be from the user.
    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)
    return cleaned


def _last_user_text(messages: List[Dict[str, str]]) -> str:
    for m in reversed(messages):
        if m["role"] == "user":
            return m["content"]
    return ""


class AnthropicLLM:
    def __init__(
        self,
        newsroom: NewsroomConfig,
        model: str = DEFAULT_MODEL,
        metadata_effort: str = "low",
        answer_effort: str = "medium",
        client: Optional[anthropic.Anthropic] = None,
    ):
        # anthropic.Anthropic() reads ANTHROPIC_API_KEY from the environment.
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.metadata_effort = metadata_effort
        self.answer_effort = answer_effort
        self.newsroom = newsroom

    def generate_metadata(self, messages: List[Dict[str, Any]], current_date: str) -> Dict[str, Any]:
        claude_messages = to_claude_messages(messages)
        response = self.client.beta.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=load_search_prompt(current_date, self.newsroom),
            messages=claude_messages,
            output_format=SearchMetadata,
            output_config={"effort": self.metadata_effort},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )

        if response.stop_reason == "refusal" or response.parsed_output is None:
            # Degrade to an unfiltered search on the user's own words rather
            # than failing the whole request.
            print(f"Metadata step returned no structured output (stop_reason={response.stop_reason}); "
                  "falling back to unfiltered search.")
            return {
                "question": _last_user_text(claude_messages),
                "date_range": {"start_date": None, "end_date": None},
                "authors": [],
            }

        return response.parsed_output.model_dump()

    def stream_answer(self, messages: List[Dict[str, Any]], current_date: str) -> Iterator[str]:
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=16000,
            system=load_answer_prompt(current_date),
            messages=to_claude_messages(messages),
            output_config={"effort": self.answer_effort},
            # Caches the conversation prefix so follow-up turns re-read the
            # previously retrieved sources at the cache-read rate.
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            for text in stream.text_stream:
                yield text
            final = stream.get_final_message()

        if final.stop_reason == "refusal":
            yield REFUSAL_NOTICE
        elif final.stop_reason == "max_tokens":
            yield TRUNCATED_NOTICE

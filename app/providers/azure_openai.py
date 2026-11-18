"""Azure OpenAI chat provider (original Dewey behavior, moved out of dewey.py)."""
import json
from typing import Any, Dict, Iterator, List

from openai import AzureOpenAI

from models import AzureOpenAIConfig, NewsroomConfig
from tools import load_answer_prompt, load_search_prompt, load_search_tool


def make_client(config: AzureOpenAIConfig) -> AzureOpenAI:
    return AzureOpenAI(
        api_key=config.api_key,
        azure_endpoint=config.endpoint,
        api_version="2025-03-01-preview",
    )


class AzureOpenAILLM:
    def __init__(self, client: AzureOpenAI, config: AzureOpenAIConfig, newsroom: NewsroomConfig):
        self.client = client
        self.config = config
        self.newsroom = newsroom

    def generate_metadata(self, messages: List[Dict[str, Any]], current_date: str) -> Dict[str, Any]:
        response = self.client.responses.create(
            model=self.config.chat_deployment,
            input=messages,
            instructions=load_search_prompt(current_date, self.newsroom),
            tools=[load_search_tool()],
            tool_choice={"type": "function", "name": "search_archive"},
        )
        return json.loads(response.output[-1].arguments)

    def stream_answer(self, messages: List[Dict[str, Any]], current_date: str) -> Iterator[str]:
        response = self.client.responses.create(
            model=self.config.chat_deployment,
            instructions=load_answer_prompt(current_date),
            input=messages,
            stream=True,
        )
        for chunk in response:
            if chunk.type == "response.output_text.delta" and chunk.delta:
                yield chunk.delta

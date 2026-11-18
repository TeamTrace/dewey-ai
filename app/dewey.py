from typing import List
from datetime import datetime
import json
import re
from tips import TipFormatter
from contextlib import contextmanager

from providers.base import LLMProvider, Retriever


class _SilentStep:
    """Step used when show_steps=False (e.g. the MCP server): reports nothing."""

    def start(self, content=""):
        return None

    def complete(self, content=""):
        return None


class Dewey:
    def __init__(self, llm: LLMProvider, retriever: Retriever):
        # Vendor-specific work lives in providers/; see providers/__init__.py
        self.llm = llm
        self.retriever = retriever
        self.sessions = {}
        self.tip_formatter = TipFormatter

    @contextmanager
    def step(self, title, show_steps=True):
        if not show_steps:
            # start()/complete() return None, so process() yields only the answer text.
            yield _SilentStep()
            return
            
        step = {"title": title, "status": "pending"}
        if not hasattr(self, '_current_steps'):
            self._current_steps = []
        
        self._current_steps.append(step)
        
        class StepYielder:
            def __init__(self, step, steps_list):
                self.step = step
                self.steps_list = steps_list
                self.has_started = False
            
            def start(self, content=""):
                if not self.has_started:
                    if content:
                        self.step["content"] = content
                    self.has_started = True
                    return "", self.steps_list.copy()
                return None
            
            def complete(self, content=""):
                self.step["status"] = "done"
                if content:
                    self.step["content"] = content
                return "", self.steps_list.copy()
        
        yielder = StepYielder(step, self._current_steps)
        try:
            yield yielder
        finally:
            step["status"] = "done"

    def process(self, message: str, history: List, show_steps: bool=True):
        # Reset steps
        self._current_steps = []

        # Grab the current day
        date_today = datetime.now()
        formatted_date_today = date_today.strftime("%A, %B %d, %Y")
        
        # Build messages
        messages = [{"role": turn["role"], "content": turn["content"]} for turn in history]
        messages.append({
            "role": "user",
            "content": message
        })
        
        # Step 1: Generate metadata
        with self.step("Generating metadata", show_steps) as step:
            if result := step.start("🔍 I'm planning my approach."):
                yield result
            metadata = self.llm.generate_metadata(messages, formatted_date_today)
            metadata_tip = self.tip_formatter.tip_metadata(metadata)
            if result := step.complete(metadata_tip):
                yield result
        
        # Step 2: Search articles  
        with self.step("Searching articles", show_steps) as step:
            if result := step.start("🔍 Digging through the archives"):
                yield result
            sources = self.retriever.retrieve(metadata)
            sources_tip = self.tip_formatter.tip_search(sources)
            if result := step.complete(sources_tip):
                yield result

        # Step 3: Generate final response with sources
        # Label each source with the [SRCn] ID the answer prompt tells the model
        # to cite; the numbering matches `source_urls` below.
        stacked_sources = '\n\n'.join(f"[SRC{i}] {source}" for i, source in enumerate(sources, 1))
        messages.append({"role": "user", "content": f"{message}\n\n## Sources\n{stacked_sources}"})

        # Create source URL lookup from sources list
        source_urls = {}
        for i, source_json in enumerate(sources, 1):
            source_data = json.loads(source_json)
            source_urls[i] = source_data["url"]
        
        partial = ""
        for delta in self.llm.stream_answer(messages, formatted_date_today):
            if delta:
                partial += delta
                # Replace citation patterns with hyperlinks
                processed_partial = re.sub(
                    r'\[SRC(\d+)\]',
                    lambda m: f"[[{m.group(1)}]]({source_urls.get(int(m.group(1)), '#')})",
                    partial
                )
                yield processed_partial, self._current_steps.copy()

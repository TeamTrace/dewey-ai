"""A tiny OpenAI-compatible chat server on 127.0.0.1 that records every request body.

Each test queues the replies it wants with `reply()`/`fail()`; with nothing queued, a
non-streaming request gets valid search metadata and a streaming request gets a short answer.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import httpx
import openai

METADATA = {"question": "school board budget", "date_range": {"start_date": None, "end_date": None}, "authors": []}
ANSWER_DELTAS = ["The board ", "approved it [SRC1]."]


def completion(content: Optional[str]) -> Dict[str, Any]:
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "test-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }


def chunk(content: Optional[str], finish_reason: Optional[str] = None) -> Dict[str, Any]:
    delta = {"content": content} if content is not None else {}
    return {
        "id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 0, "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


class MockChatServer:
    def __init__(self):
        self.requests: List[Dict[str, Any]] = []  # {"path", "headers", "body"}
        self._replies: List[Any] = []
        self._lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output clean
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                with server._lock:
                    server.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                    queued = server._replies.pop(0) if server._replies else None
                if queued is not None:
                    status, payload = queued
                    self._send_json(status, payload)
                elif body.get("stream"):
                    self._send_stream()
                else:
                    self._send_json(200, completion(json.dumps(METADATA)))

            def _send_json(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_stream(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                events = [chunk(d) for d in ANSWER_DELTAS] + [chunk(None, "stop")]
                for event in events:
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def reply(self, status: int, payload: Dict[str, Any]) -> None:
        """Queue one JSON reply for the next request."""
        with self._lock:
            self._replies.append((status, payload))

    def fail(self, status: int, message: str, **metadata) -> None:
        """Queue an OpenRouter-shaped error reply: {"error": {"code", "message", "metadata"?}}."""
        error: Dict[str, Any] = {"code": status, "message": message}
        if metadata:
            error["metadata"] = metadata
        self.reply(status, {"error": error})

    def bodies(self) -> List[Dict[str, Any]]:
        return [r["body"] for r in self.requests]

    def client(self) -> openai.OpenAI:
        # No retries (a 503 would otherwise be retried with backoff) and no proxy settings from the environment.
        return openai.OpenAI(base_url=self.base_url, api_key="test-key", max_retries=0,
                             http_client=httpx.Client(trust_env=False, timeout=10))

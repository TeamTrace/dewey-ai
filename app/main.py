"""
Dewey's web UI.

    python app/main.py

By default Dewey listens on 127.0.0.1, which only this computer can reach.
To let colleagues use it, add accounts with `python app/manage_users.py add
NAME`, listen on the network (GRADIO_SERVER_NAME=0.0.0.0) and put HTTPS in
front; see docs/DEPLOYMENT.md. Dewey refuses to listen on a network address
with no accounts unless DEWEY_ALLOW_NO_AUTH=true, which is only for
deployments behind a single-sign-on proxy that already checks who people are.

Settings (environment or .env):
  GRADIO_SERVER_NAME   address to listen on (default 127.0.0.1)
  GRADIO_SERVER_PORT   port (default: PORT if set, else the first free port from 7860)
  PORT                 used when GRADIO_SERVER_PORT is unset; Render and similar platforms set it
  DEWEY_USERS_FILE     accounts file (default: users.txt in the repo root)
  DEWEY_USERS          accounts in one value, separated by commas (instead of the file)
  DEWEY_ALLOW_NO_AUTH  true: allow no accounts on a network address (SSO proxy only)
  DEWEY_AUTH_MESSAGE   text shown on the login page
  DEWEY_CONCURRENCY    how many questions are answered at the same time (default 4)

Telemetry, the API docs page, the /monitoring pages, MCP, share links and file
uploads over 1 MB are always off. Without accounts on 127.0.0.1, only requests
addressed to localhost/127.0.0.1 are answered (DNS rebinding); over HTTPS,
Gradio's non-Secure copy of the login cookie is dropped (see RequestGuard).
"""
from __future__ import annotations

import ipaddress
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import auth

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 100
MAX_UPLOAD = "1mb"  # Dewey has no upload fields; this caps Gradio's generic /upload route
EXIT_CONFIG_ERROR = 2

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("", "0", "false", "no", "off")


class StartupError(Exception):
    """A setting that must stop Dewey before it serves anyone (exit status 2)."""


@dataclass
class LaunchPlan:
    launch_kwargs: Dict[str, Any]
    queue_kwargs: Dict[str, Any]
    summary: List[str] = field(default_factory=list)  # printed at startup; never contains secrets
    warnings: List[str] = field(default_factory=list)


def env_flag(env: Mapping[str, str], name: str) -> bool:
    raw = env.get(name, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise StartupError(f"{name} must be true or false, got {raw!r}.")


def env_int(env: Mapping[str, str], name: str, low: int, high: int) -> Optional[int]:
    raw = env.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise StartupError(f"{name} must be a whole number, got {raw!r}.") from None
    if not low <= value <= high:
        raise StartupError(f"{name} must be between {low} and {high}, got {value}.")
    return value


def server_address(env: Mapping[str, str]) -> Tuple[str, Optional[int], List[str]]:
    """(host, port or None for "first free port from 7860", warnings)."""
    host = env.get("GRADIO_SERVER_NAME", "").strip() or DEFAULT_HOST
    port = env_int(env, "GRADIO_SERVER_PORT", 1, 65535)
    warnings: List[str] = []
    if port is None:
        return host, env_int(env, "PORT", 1, 65535), warnings
    platform_port = env.get("PORT", "").strip()
    if platform_port and platform_port != str(port):
        warnings.append(
            f"PORT is {platform_port!r} but GRADIO_SERVER_PORT={port} takes precedence. If your hosting "
            "platform sends traffic to PORT, set GRADIO_SERVER_PORT to the same value."
        )
    return host, port, warnings


def is_loopback(host: str) -> bool:
    """True for addresses only this computer can reach (127.0.0.0/8, ::1, localhost)."""
    name = host.strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False  # a host name or 0.0.0.0/:: and the like: assume reachable


_LOCALHOST_SUBDOMAIN_RE = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.localhost")
_PORT_SUFFIX_RE = re.compile(r"(?::[0-9]{1,5})?")


def loopback_host_header(value: str) -> bool:
    """True when an HTTP Host header names this computer: localhost, *.localhost
    or a loopback IP address, with an optional :port. Anything else (including
    0.0.0.0) is refused in local no-login mode."""
    host = value.strip().lower()
    if host.startswith("["):
        name, bracket, rest = host[1:].partition("]")
        if not bracket:
            return False
    else:
        name, colon, port = host.partition(":")
        rest = colon + port
    if not _PORT_SUFFIX_RE.fullmatch(rest):
        return False
    if name == "localhost" or _LOCALHOST_SUBDOMAIN_RE.fullmatch(name):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


WRONG_HOST_MESSAGE = (
    "Dewey is running on this computer without login, so it only answers requests addressed to "
    "localhost or 127.0.0.1. Open it at http://127.0.0.1:<port>/ instead. (This stops web pages "
    "from reaching it through DNS rebinding.) Behind a reverse proxy, see docs/DEPLOYMENT.md.\n"
)


class RequestGuard:
    """ASGI middleware in front of Gradio's routes.

    - Always: 404 for /monitoring and /monitoring/*. Gradio 5.44.1 serves
      /monitoring/summary (request counts and timings per function) with no login
      check, even with enable_monitoring=False.
    - loopback_only (local, no login): 400 unless the Host header is a loopback
      name. Without it, a web page the person visits can point its own host name
      at 127.0.0.1 (DNS rebinding) and use Dewey as if it were same-origin.
    - Over HTTPS: drops Gradio's non-Secure copy of the login cookie.
    """

    def __init__(self, app, loopback_only: bool = False):
        self.app = app
        self.loopback_only = loopback_only

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path == "/monitoring" or path.startswith("/monitoring/"):
                await self._refuse(scope, send, 404, "Not found.\n")
                return
            if self.loopback_only:
                host = next((v for k, v in scope.get("headers") or [] if k == b"host"), None)
                if host is None or not loopback_host_header(host.decode("latin-1")):
                    await self._refuse(scope, send, 400, WRONG_HOST_MESSAGE)
                    return
            if scope["type"] == "http" and _is_https(scope):
                send = _without_plaintext_session_cookie(send)
        await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(scope, send, status: int, text: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = text.encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(body)).encode("ascii"))],
        })
        await send({"type": "http.response.body", "body": body})


def _is_https(scope) -> bool:
    """The browser used HTTPS: TLS here, or a proxy says so in X-Forwarded-Proto.
    A client can fake the header, but faking it only drops its own cookie copy."""
    if scope.get("scheme") == "https":
        return True
    proto = next((v for k, v in scope.get("headers") or [] if k == b"x-forwarded-proto"), b"")
    return proto.split(b",")[0].strip().lower() == b"https"


_PLAINTEXT_COOKIE = b"access-token-unsecure-"


def _without_plaintext_session_cookie(send):
    """Gradio 5.44.1 sets the login token twice: access-token-<id> (Secure) and
    access-token-unsecure-<id> (no Secure flag, for plain-HTTP use). Over HTTPS
    the second copy would be sent in clear text on any visit to the http://
    address, so drop it; Gradio reads the Secure one first."""
    async def wrapped(message):
        if message["type"] == "http.response.start":
            headers = [(k, v) for k, v in message.get("headers", [])
                       if not (k.lower() == b"set-cookie" and v.lstrip().startswith(_PLAINTEXT_COOKIE))]
            message = {**message, "headers": headers}
        await send(message)
    return wrapped


def request_guard_middleware(loopback_only: bool):
    from starlette.middleware import Middleware  # installed with Gradio; imported lazily like gradio

    return Middleware(RequestGuard, loopback_only=loopback_only)


def check_exposure(host: str, user_count: int, allow_no_auth: bool) -> None:
    """Refuse to serve the network with no login, unless explicitly allowed."""
    if user_count or allow_no_auth or is_loopback(host):
        return
    raise StartupError(
        f"Refusing to start: GRADIO_SERVER_NAME={host} lets other machines reach Dewey, but no login "
        "accounts are set up, so anyone who can reach it could use it (and your API keys).\n"
        "Do one of these:\n"
        "  - add an account:  python app/manage_users.py add NAME   (then start Dewey again)\n"
        "  - listen on this computer only: unset GRADIO_SERVER_NAME (the default is 127.0.0.1)\n"
        "  - only if a single-sign-on proxy in front of Dewey already checks who people are:\n"
        "    set DEWEY_ALLOW_NO_AUTH=true\n"
        "See docs/DEPLOYMENT.md."
    )


def build_launch_plan(env: Mapping[str, str], repo_root: Path = REPO_ROOT) -> LaunchPlan:
    """Work out Gradio's launch()/queue() settings from the environment, without
    starting anything. Raises StartupError for settings Dewey must not run with."""
    host, port, warnings = server_address(env)
    allow_no_auth = env_flag(env, "DEWEY_ALLOW_NO_AUTH")
    concurrency = env_int(env, "DEWEY_CONCURRENCY", 1, MAX_CONCURRENCY) or DEFAULT_CONCURRENCY
    try:
        store = auth.load_user_store(env, repo_root=repo_root, warn=warnings.append)
    except auth.UsersConfigError as e:
        raise StartupError(str(e)) from None
    user_count = len(store) if store is not None else 0
    check_exposure(host, user_count, allow_no_auth)

    login: Optional[Callable[[str, str], bool]] = store.verify if store is not None and user_count else None
    # Local with no login: only answer requests addressed to localhost (DNS rebinding).
    # Not with DEWEY_ALLOW_NO_AUTH, where an SSO proxy may forward its own Host header.
    loopback_only = login is None and is_loopback(host) and not allow_no_auth
    launch_kwargs: Dict[str, Any] = {
        "server_name": host,
        "server_port": port,
        "auth": login,
        "auth_message": env.get("DEWEY_AUTH_MESSAGE", "").strip() or None,
        "share": False,
        "show_api": False,
        "enable_monitoring": False,
        "mcp_server": False,
        "max_file_size": MAX_UPLOAD,
        "app_kwargs": {"middleware": [request_guard_middleware(loopback_only)]},
    }
    queue_kwargs: Dict[str, Any] = {"api_open": False, "default_concurrency_limit": concurrency}

    where = f"{host}:{port}" if port is not None else f"{host}, first free port from 7860"
    reach = "this computer only" if is_loopback(host) else "reachable from other machines"
    summary = [f"Listening on {where} ({reach})."]
    if login is not None:
        summary.append(f"Login required: {user_count} account(s) from {store.source}.")
        if allow_no_auth:
            summary.append("DEWEY_ALLOW_NO_AUTH is ignored because accounts are set up.")
    elif is_loopback(host):
        empty = f" ({store.source} has no accounts)" if store is not None else ""
        summary.append(f"Login: off{empty}. Only this computer can reach Dewey.")
        if loopback_only:
            summary.append("Requests must be addressed to localhost or 127.0.0.1 (blocks DNS rebinding).")
    else:
        summary.append(
            "Login: OFF (DEWEY_ALLOW_NO_AUTH=true). Anyone who can reach this address can use Dewey. "
            "Run this way only behind a single-sign-on proxy, with this port closed to everything else."
        )
    summary.append(f"Answers up to {concurrency} question(s) at once (DEWEY_CONCURRENCY).")
    summary.append("Telemetry, API page, /monitoring, MCP and share links: off.")
    return LaunchPlan(launch_kwargs, queue_kwargs, summary, warnings)


def make_chat_handler(dewey) -> Callable:
    def chat_with_dewey(message, history, session_state):
        # Initialize session if needed
        if session_state["session_id"] is None:
            session_state["session_id"] = str(uuid.uuid4())
            session_state["user_data"]["message_count"] = 0

        # Track message count
        session_state["user_data"]["message_count"] += 1

        # Add user message to history and show it immediately
        history.append({"role": "user", "content": message})
        yield history, session_state

        # Stream the response from Dewey
        history_length = len(history)
        for response, steps in dewey.process(message, history[:-1]):
            history = history[:history_length]
            for step in steps:
                history.append({
                    "role": "assistant",
                    "content": f"{step.get('content', '')}",
                    "metadata": {
                        "title": step["title"],
                        "status": step["status"]
                    }
                })
            history.append({
                "role": "assistant",
                "content": response
            })

            yield history, session_state

    return chat_with_dewey


def build_ui(dewey):
    import gradio as gr  # imported here so tests of the launch settings don't need it

    chat_with_dewey = make_chat_handler(dewey)
    with gr.Blocks(title="Dewey", analytics_enabled=False) as demo:
        gr.Markdown("# Dewey")
        gr.Markdown("A newsroom archive assistant.")

        # Session state
        session_state = gr.State({"session_id": None, "user_data": {}})

        chatbot = gr.Chatbot(type="messages", label="Dewey")
        msg = gr.Textbox(label="Message", placeholder="Ask your question here...")
        clear = gr.Button("Clear")

        msg.submit(chat_with_dewey, [msg, chatbot, session_state], [chatbot, session_state])
        msg.submit(lambda: "", None, [msg])
        clear.click(lambda: [], None, [chatbot])
    return demo


def main() -> int:
    from dotenv import load_dotenv

    # interpolate=False: values are literal, so a password containing "${" is not expanded.
    load_dotenv(REPO_ROOT / ".env", interpolate=False)
    # Gradio parses GRADIO_SERVER_PORT when it is imported; a blank value would crash it.
    for name in ("GRADIO_SERVER_NAME", "GRADIO_SERVER_PORT"):
        if name in os.environ and not os.environ[name].strip():
            del os.environ[name]

    try:
        plan = build_launch_plan(os.environ)
    except StartupError as e:
        print(f"Dewey: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    for warning in plan.warnings:
        print(f"Dewey: warning: {warning}", file=sys.stderr)
    for line in plan.summary:
        print(f"Dewey: {line}")

    from dewey import Dewey
    from models import NewsroomConfig
    from providers import StackConfigError, build_stack

    # DEWEY_STACK=azure (default), anthropic or open; see providers/__init__.py
    try:
        llm, retriever = build_stack(NewsroomConfig.from_env())
    except StackConfigError as e:
        print(f"Dewey: {e}", file=sys.stderr)
        return 1
    demo = build_ui(Dewey(llm, retriever))
    demo.queue(**plan.queue_kwargs)
    demo.launch(**plan.launch_kwargs)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Dewey's demo chat page (Gradio), for trying Dewey on your own computer.

    python app/main.py

Dewey's main interface is its MCP server (app/mcp_server.py, docs/MCP.md): agent
tools and chat apps such as Open WebUI and LibreChat connect to that. This page is
a localhost demo with no login, so it listens on 127.0.0.1 (this computer only)
and refuses any other address unless one of these says it is safe:

  DEWEY_DEMO_BEHIND_AUTH_PROXY=true      a proxy in front of the page makes people
                                         sign in (single sign-on) and nothing else
                                         can reach this port
  DEWEY_DEMO_PUBLISHED_ON_LOOPBACK=true  in a container whose port Docker publishes
                                         on the host's 127.0.0.1 only. The "demo"
                                         service in docker-compose.yml sets it; the
                                         refusal message doesn't offer it, because
                                         set on a bare host it exposes the page

Settings (environment or .env):
  GRADIO_SERVER_NAME   address to listen on (default 127.0.0.1)
  GRADIO_SERVER_PORT   port (default: PORT if set, else the first free port from 7860)
  PORT                 used when GRADIO_SERVER_PORT is unset; Render and similar platforms set it
  DEWEY_CONCURRENCY    how many questions are answered at the same time (default 4)

Telemetry, the API docs page, the /monitoring pages, Gradio's MCP server, share
links and file uploads over 1 MB are always off, and the page may run scripts only
from Dewey itself (Gradio's page template also loads one from cdnjs.cloudflare.com).
Unless DEWEY_DEMO_BEHIND_AUTH_PROXY is set, only requests addressed to localhost/
127.0.0.1 that come from the demo page itself (or from no web page at all) are
answered (DNS rebinding, other web pages; see RequestGuard).
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
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 100
MAX_UPLOAD = "1mb"  # Dewey has no upload fields; this caps Gradio's generic /upload route
EXIT_CONFIG_ERROR = 2

BEHIND_PROXY_ENV = "DEWEY_DEMO_BEHIND_AUTH_PROXY"
PUBLISHED_ON_LOOPBACK_ENV = "DEWEY_DEMO_PUBLISHED_ON_LOOPBACK"

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
    0.0.0.0) is refused unless the demo is behind an access-controlling proxy."""
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


def same_origin(origin: Optional[str], host: str) -> bool:
    """True when a request's Origin header is absent or is the demo page's own origin,
    http(s)://<the request's Host>. Browsers send Origin on every cross-origin request
    and every POST, so this refuses requests made by script on other web pages.

    Gradio's CORS middleware only refuses other web pages when the Host is exactly
    localhost or 127.0.0.1; for any other loopback name (x.localhost, which browsers
    resolve to 127.0.0.1 themselves, [::1], 127.0.0.2) it lets every origin read the
    responses. "null" (sandboxed frames, file: pages) is refused too."""
    if origin is None:
        return True
    try:
        o = urlsplit(origin.strip().lower())
        h = urlsplit(f"{o.scheme}://{host.strip().lower()}")
        default_port = {"http": 80, "https": 443}[o.scheme]
        if o.username or o.password or o.path or o.query or o.fragment or not o.hostname:
            return False
        return (o.hostname, o.port or default_port) == (h.hostname, h.port or default_port)
    except (KeyError, ValueError):  # another scheme, "null", a bad port
        return False


# Scripts only from Dewey's own address. Gradio's page template (checked in 6.16.0) loads
# iframe-resizer from cdnjs.cloudflare.com (no integrity hash), which would run next to
# the conversation; the demo is never embedded in another page, so it isn't needed.
# 'unsafe-inline' is for the template's own inline scripts (the page config).
CONTENT_SECURITY_POLICY = b"script-src 'self' 'unsafe-inline'"

WRONG_HOST_MESSAGE = (
    "Dewey's demo page has no login, so it only answers requests addressed to localhost or "
    "127.0.0.1. Open it at http://127.0.0.1:<port>/ instead. (This stops web pages from reaching "
    f"it through DNS rebinding.) Behind a sign-in proxy, see {BEHIND_PROXY_ENV} in docs/DEPLOYMENT.md.\n"
)

OTHER_ORIGIN_MESSAGE = (
    "Dewey's demo page has no login, so it only answers requests from its own page. This request "
    "came from another web page (its Origin header names a different site), so it was refused. "
    "Open the demo at http://127.0.0.1:<port>/ and ask there.\n"
)


class RequestGuard:
    """ASGI middleware in front of Gradio's routes.

    - Always: 404 for /monitoring and /monitoring/*. Gradio (checked in 6.16.0) serves
      /monitoring/summary (request counts and timings per function) even with
      enable_monitoring=False. Every response carries CONTENT_SECURITY_POLICY.
    - loopback_only (everything except DEWEY_DEMO_BEHIND_AUTH_PROXY): 400 unless
      the Host header is a loopback name. Without it, a web page the person visits
      can point its own host name at 127.0.0.1 (DNS rebinding) and use the demo as
      if it were same-origin. Then 403 unless the Origin header is absent or the
      page's own (same_origin): otherwise script on any web page could ask questions
      at http://x.localhost:<port>/ and read the answers, because Gradio's CORS
      check only covers the exact names localhost and 127.0.0.1.
    """

    def __init__(self, app, loopback_only: bool = True):
        self.app = app
        self.loopback_only = loopback_only

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path == "/monitoring" or path.startswith("/monitoring/"):
                await self._refuse(scope, send, 404, "Not found.\n")
                return
            if self.loopback_only:
                headers = scope.get("headers") or []
                host = next((v for k, v in headers if k == b"host"), None)
                if host is None or not loopback_host_header(host.decode("latin-1")):
                    await self._refuse(scope, send, 400, WRONG_HOST_MESSAGE)
                    return
                origins = [v.decode("latin-1") for k, v in headers if k == b"origin"]
                if len(origins) > 1 or not same_origin(origins[0] if origins else None, host.decode("latin-1")):
                    await self._refuse(scope, send, 403, OTHER_ORIGIN_MESSAGE)
                    return
        if scope["type"] == "http":
            send = self._with_csp(send)
        await self.app(scope, receive, send)

    @staticmethod
    def _with_csp(send):
        async def send_with_csp(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"content-security-policy"]
                message = {**message, "headers": headers + [(b"content-security-policy", CONTENT_SECURITY_POLICY)]}
            await send(message)

        return send_with_csp

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
                        (b"content-length", str(len(body)).encode("ascii")),
                        (b"content-security-policy", CONTENT_SECURITY_POLICY)],
        })
        await send({"type": "http.response.body", "body": body})


def request_guard_middleware(loopback_only: bool):
    from starlette.middleware import Middleware  # installed with Gradio; imported lazily like gradio

    return Middleware(RequestGuard, loopback_only=loopback_only)


def check_exposure(host: str, behind_proxy: bool, published_on_loopback: bool) -> None:
    """The demo has no login: refuse a network address unless a setting says access is controlled."""
    if is_loopback(host) or behind_proxy or published_on_loopback:
        return
    # PUBLISHED_ON_LOOPBACK_ENV is deliberately not offered below: the Compose demo service sets
    # it. Set by hand on a machine's own network address, it would put the page on the network:
    # the Host check only stops browsers (DNS rebinding), not a direct request.
    raise StartupError(
        f"Refusing to start: GRADIO_SERVER_NAME={host} lets other machines reach the demo page. It has "
        "no login, so anyone who can reach it could search the archive and ask questions on your API keys.\n"
        "Do one of these:\n"
        "  - listen on this computer only: unset GRADIO_SERVER_NAME (the default is 127.0.0.1)\n"
        "  - for colleagues and AI tools, run the MCP server instead (python app/mcp_server.py --http,\n"
        "    which requires a token; see docs/MCP.md)\n"
        "  - only if a proxy in front of the demo makes people sign in (single sign-on) and nothing\n"
        f"    else can reach this port: set {BEHIND_PROXY_ENV}=true\n"
        "  - in Docker, use docker compose --profile demo up, which publishes the page on the host's\n"
        "    127.0.0.1 only and sets what it needs itself\n"
        "See docs/DEPLOYMENT.md."
    )


def build_launch_plan(env: Mapping[str, str]) -> LaunchPlan:
    """Work out Gradio's launch()/queue() settings from the environment, without
    starting anything. Raises StartupError for settings Dewey must not run with."""
    host, port, warnings = server_address(env)
    behind_proxy = env_flag(env, BEHIND_PROXY_ENV)
    published_on_loopback = env_flag(env, PUBLISHED_ON_LOOPBACK_ENV)
    concurrency = env_int(env, "DEWEY_CONCURRENCY", 1, MAX_CONCURRENCY) or DEFAULT_CONCURRENCY
    check_exposure(host, behind_proxy, published_on_loopback)

    # Only answer requests addressed to localhost (DNS rebinding), except behind a
    # sign-in proxy, which may forward its own public Host header.
    loopback_only = not behind_proxy
    launch_kwargs: Dict[str, Any] = {
        "server_name": host,
        "server_port": port,
        "share": False,
        # No "Use via API" link (Gradio 6 replaced show_api with footer_links and per-event
        # api_visibility; build_ui marks every event private).
        "footer_links": ["gradio", "settings"],
        "enable_monitoring": False,
        "mcp_server": False,  # Gradio's own MCP server; Dewey's is app/mcp_server.py
        "max_file_size": MAX_UPLOAD,
        "app_kwargs": {"middleware": [request_guard_middleware(loopback_only)]},
    }
    queue_kwargs: Dict[str, Any] = {"api_open": False, "default_concurrency_limit": concurrency}

    where = f"{host}:{port}" if port is not None else f"{host}, first free port from 7860"
    reach = "this computer only" if is_loopback(host) else "reachable from other machines"
    summary = [f"Listening on {where} ({reach})."]
    if behind_proxy:
        summary.append(
            f"Demo page, no login ({BEHIND_PROXY_ENV}=true): the proxy in front must make people sign "
            "in, and nothing else may be able to reach this port."
        )
    elif not is_loopback(host):
        summary.append(
            f"Demo page, no login ({PUBLISHED_ON_LOOPBACK_ENV}=true): Docker must publish this port on "
            "the host's 127.0.0.1 only."
        )
    else:
        summary.append("Demo page, no login. Only this computer can reach it.")
    if loopback_only:
        summary.append("Requests must be addressed to localhost or 127.0.0.1 (blocks DNS rebinding) and "
                       "come from the demo page itself (blocks other web pages).")
    summary.append(f"Answers up to {concurrency} question(s) at once (DEWEY_CONCURRENCY).")
    summary.append("Telemetry, API page, /monitoring and Gradio's MCP server: off.")
    summary.append("For colleagues and AI tools, use Dewey's MCP server instead: docs/MCP.md.")
    # These two are Dewey's last lines on stdout, so they sit just above Gradio's own lines
    # (a warning from build_stack(), on stderr, can still come between them). launch() is not quiet,
    # because Gradio's "Running on local URL" line is the only place the port actually used is
    # shown; unless it is quiet, Gradio also prints a tip to set share=True, which would
    # contradict "share links: off" (tests/deploy/test_main_launch.py pins both lines).
    summary.append("Share links: off, always. The page has no login, so Dewey never makes a public link.")
    summary.append("Gradio, which serves the page, prints a tip about share=True when it starts. Ignore it.")
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

        # Gradio 6 always uses the messages format (no `type`). `buttons=[]` and
        # `allow_tags=False` keep Gradio 5's defaults: no share/copy buttons, and no
        # custom HTML tags kept in rendered messages.
        chatbot = gr.Chatbot(label="Dewey", buttons=[], allow_tags=False)
        msg = gr.Textbox(label="Message", placeholder="Ask your question here...")
        clear = gr.Button("Clear")

        # api_visibility="private": left out of the API docs, so Gradio's client libraries don't
        # offer them. This hides them; it doesn't lock them. RequestGuard and api_open=False do that.
        msg.submit(chat_with_dewey, [msg, chatbot, session_state], [chatbot, session_state],
                   api_visibility="private")
        msg.submit(lambda: "", None, [msg], api_visibility="private")
        clear.click(lambda: [], None, [chatbot], api_visibility="private")
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
    from models import NewsroomConfig, NewsroomConfigError
    from providers import StackConfigError, build_stack

    # DEWEY_STACK=azure (default), anthropic or open; see providers/__init__.py
    try:
        llm, retriever = build_stack(NewsroomConfig.from_env())
    except (StackConfigError, NewsroomConfigError) as e:
        print(f"Dewey: {e}", file=sys.stderr)
        return 1
    demo = build_ui(Dewey(llm, retriever))
    demo.queue(**plan.queue_kwargs)
    demo.launch(**plan.launch_kwargs)
    return 0


if __name__ == "__main__":
    sys.exit(main())

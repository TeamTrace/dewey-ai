"""Shared helpers for the MCP server tests: run app/mcp_server.py as a subprocess, offline."""
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER = REPO_ROOT / "app" / "mcp_server.py"
PYTHON = sys.executable

TEST_TOKEN = "test-token-" + "x" * 40  # 51 characters, valid b64token


def server_env(fake_stack: bool = True, **overrides: Optional[str]) -> Dict[str, str]:
    """
    Environment for the server subprocess. Variables are set explicitly (possibly to "") because
    the server loads the repo's .env, which never overrides a variable that is already present;
    that keeps a developer's real .env out of the tests.
    """
    env = dict(os.environ)
    env.pop("DEWEY_MCP_TEST_STACK", None)
    if fake_stack:
        env["DEWEY_MCP_TEST_STACK"] = "1"
    env["DEWEY_MCP_TOKEN"] = ""
    env["NEWSROOM_NAME"] = "The Example Times"  # required: the server has no default newsroom
    env["ARCHIVE_START_DATE"] = env["DEFAULT_REGION"] = ""
    env["PYTHONUNBUFFERED"] = "1"
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def run_server(args: List[str], env: Dict[str, str], timeout: float = 60) -> subprocess.CompletedProcess:
    """Run the server to completion (for invocations that should refuse to start)."""
    return subprocess.run(
        [PYTHON, str(SERVER), *args], env=env, cwd=REPO_ROOT, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout,
    )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HttpServer:
    """Start the server in HTTP mode on a free loopback port; stop it on exit."""

    def __init__(self, extra_args: List[str], env: Dict[str, str]):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.args = ["--http", "--port", str(self.port), *extra_args]
        self.env = env
        self.proc: Optional[subprocess.Popen] = None

    def __enter__(self) -> "HttpServer":
        self._stderr = tempfile.TemporaryFile("w+")
        self.proc = subprocess.Popen(
            [PYTHON, str(SERVER), *self.args], env=self.env, cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self._stderr, text=True,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited with {self.proc.returncode}:\n{self.stderr()}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return self
            except OSError:
                time.sleep(0.1)
        self.__exit__(None, None, None)
        raise RuntimeError(f"server did not start listening:\n{self.stderr()}")

    def stderr(self) -> str:
        self._stderr.seek(0)
        return self._stderr.read()

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self._stderr.close()

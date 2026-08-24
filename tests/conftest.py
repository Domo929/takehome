"""Shared test fixtures.

The fake Vertex server runs as a real uvicorn process on an ephemeral port rather
than an in-process ASGI transport, so tests exercise the genuine HTTP path: real
sockets, the real connection pool, real JSON serialization. Those are the layers
where async LLM clients actually fail, and an ASGI shortcut would skip all of them.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn

from mock.fake_vertex import Behavior, create_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ServerHandle:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def configure(self, **kwargs) -> None:
        """Re-tune the server's failure profile mid-test."""
        httpx.post(f"{self.base_url}/__configure", json=kwargs, timeout=5.0).raise_for_status()

    def reset(self) -> None:
        httpx.post(f"{self.base_url}/__reset", timeout=5.0).raise_for_status()

    def stats(self) -> dict:
        return httpx.get(f"{self.base_url}/__stats", timeout=5.0).json()


@pytest.fixture(scope="session")
def fake_vertex() -> Iterator[ServerHandle]:
    port = _free_port()
    behavior = Behavior(
        base_latency_s=0.01,
        latency_sigma=0.05,
        per_output_token_s=0.0,
        seed=1234,
    )
    config = uvicorn.Config(
        create_app(behavior), host="127.0.0.1", port=port, log_level="error"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            httpx.get(f"{base_url}/__stats", timeout=1.0).raise_for_status()
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("fake vertex server did not start")

    yield ServerHandle(base_url)

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _fake_adc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub Application Default Credentials.

    Vertex mode always builds a credentialed client. Tests must never touch real
    credentials or the network, so ADC resolution is replaced wholesale.
    """
    import google.auth
    import google.auth.credentials as gac

    class _Creds(gac.Credentials):
        def refresh(self, request) -> None:
            self.token = "fake-token"

        @property
        def valid(self) -> bool:
            return True

        @property
        def expired(self) -> bool:
            return False

    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (_Creds(), "fake-project"))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop ambient GEMINI_*/GOOGLE_* env from leaking into test configuration."""
    for key in (
        "GEMINI_MAX_OUTPUT_TOKENS", "GEMINI_THINKING_BUDGET", "GEMINI_MAX_CONNECTIONS",
        "GEMINI_PARALLELISM", "GEMINI_MAX_ATTEMPTS", "GEMINI_ATTEMPT_TIMEOUT_S",
        "GEMINI_TOTAL_DEADLINE_S", "GEMINI_MODEL", "GEMINI_BACKEND", "GEMINI_BASE_URL",
        "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()

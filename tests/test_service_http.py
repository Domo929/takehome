"""The service over real HTTP, not by inspecting its state.

`test_service_wiring.py` checks configuration invariants by reading attributes. That
misses everything the web layer does: request validation, status codes, the shape of
the JSON a caller actually receives, and whether shedding and the budget breaker fire
where they are supposed to. Those are the behaviours the load tests depend on, so they
are worth pinning through the same path a caller takes.

The provider is stubbed. The point here is the HTTP lifecycle, and a real provider
would only add network flakiness to a test about status codes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from llm.errors import LLMContentBlockedError, LLMRateLimitError
from llm.llm import LLM, FinishReason
from service import app as service_app


def _response(**kw: Any) -> LLM.SimpleResponse:
    base = dict(
        answer="iRobot, Roborock, and Eufy.",
        input_tokens=35, output_tokens=110, thinking_tokens=0,
        finish_reason=FinishReason.STOP, cost_usd=0.00029,
        latency_ms=120.0, upstream_total_ms=120.0, attempts=1,
    )
    base.update(kw)
    return LLM.SimpleResponse(**base)


class StubProvider:
    """Minimal stand-in. `describe()` exists because startup logs it."""

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result, self.error = result or _response(), error
        self.limiter = None
        self.calls: list[dict] = []

    def parallelism(self) -> int:
        return 4

    def describe(self) -> dict[str, Any]:
        return {"provider": "stub", "model": "stub-model"}

    async def ask_generic_question(self, system, question, temperature, *, grounded=None):
        self.calls.append({"temperature": temperature, "grounded": grounded})
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """A started app with a stubbed provider, driven over HTTP."""

    def _build(provider: StubProvider, **env: str):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(service_app, "_build_provider", lambda: provider)
        return TestClient(service_app.app)

    return _build


def test_health_reports_readiness_and_config(client):
    with client(StubProvider()) as c:
        r = c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["capacity"] == 4
        assert body["draining"] is False


def test_ask_returns_the_decomposition_a_caller_needs(client):
    with client(StubProvider()) as c:
        r = c.post("/ask", json={"question": "Which robot vacuums?"})
        assert r.status_code == 200
        body = r.json()
        assert body["answer"]
        assert body["finish_reason"] == "STOP"
        # The latency split is the reason this service exists rather than a thin
        # proxy: without it "our overhead" cannot be separated from vendor time.
        for key in ("upstream_ms", "overhead_ms", "queue_wait_ms", "retry_backoff_ms"):
            assert key in body, f"{key} missing from /ask response"
        assert body["grounded"] is False


def test_ask_rejects_a_malformed_body(client):
    with client(StubProvider()) as c:
        assert c.post("/ask", json={}).status_code == 422
        assert c.post("/ask", json={"question": "x" * 9000}).status_code == 422


def test_temperature_and_grounding_reach_the_provider(client):
    """Both are per-request on the contract, so they must survive the HTTP hop."""
    provider = StubProvider(_response(grounded=True, grounding_sources=["https://x"]))
    with client(provider) as c:
        r = c.post("/ask", json={"question": "q", "temperature": 0.2, "grounded": True})
        assert r.status_code == 200
        assert provider.calls[-1] == {"temperature": 0.2, "grounded": True}
        assert r.json()["grounded"] is True
        assert r.json()["grounding_sources"] == ["https://x"]


def test_unusable_answers_are_422_not_200(client):
    """A blocked answer is not a successful one, and must not look like it."""
    provider = StubProvider(error=LLMContentBlockedError("blocked: SAFETY", provider="stub"))
    with client(provider) as c:
        r = c.post("/ask", json={"question": "q"})
        assert r.status_code == 422
        assert "error" in r.json()


def test_vendor_rate_limit_is_surfaced_as_429_with_retry_after(client):
    """Passing the vendor's backpressure through is what lets a caller back off."""
    err = LLMRateLimitError("quota", provider="stub", retry_after_s=7)
    with client(StubProvider(error=err)) as c:
        r = c.post("/ask", json={"question": "q"})
        assert r.status_code == 429
        assert r.headers.get("Retry-After") == "7"


def test_metrics_endpoint_exposes_prometheus_text(client):
    with client(StubProvider()) as c:
        c.post("/ask", json={"question": "q"})
        r = c.get("/metrics")
        assert r.status_code == 200
        assert "llm_pool_saturation_ratio" in r.text


def test_budget_ceiling_sheds_with_a_reset_hint(client):
    """The breaker must tell a caller when to return, not just refuse.

    A spend ceiling with no window is an outage: the process refuses every request
    until someone restarts it. The window makes it backpressure instead.
    """
    provider = StubProvider(_response(cost_usd=1.0))
    with client(provider, SERVICE_BUDGET_USD="1.5", SERVICE_BUDGET_WINDOW_S="3600") as c:
        assert c.post("/ask", json={"question": "q"}).status_code == 200
        assert c.post("/ask", json={"question": "q"}).status_code == 200
        r = c.post("/ask", json={"question": "q"})
        assert r.status_code == 503
        assert r.json()["error"] == "spend ceiling reached"
        assert int(r.headers["Retry-After"]) > 0
        assert r.json()["window_resets_in_s"] > 0


def test_budget_window_rolls_over(client):
    """Without this the service bricks itself permanently on a long-lived process."""
    provider = StubProvider(_response(cost_usd=1.0))
    with client(provider, SERVICE_BUDGET_USD="0.5", SERVICE_BUDGET_WINDOW_S="3600") as c:
        assert c.post("/ask", json={"question": "q"}).status_code == 200
        assert c.post("/ask", json={"question": "q"}).status_code == 503
        # Expire the window rather than sleeping an hour.
        service_app.state.window_started -= 3601
        assert c.post("/ask", json={"question": "q"}).status_code == 200


def test_draining_service_refuses_new_work(client):
    """Shutdown must stop accepting rather than drop requests already paid for."""
    with client(StubProvider()) as c:
        service_app.state.draining = True
        try:
            r = c.post("/ask", json={"question": "q"})
            assert r.status_code == 503
            assert r.json()["error"] == "shutting down"
            assert c.get("/health").json()["ok"] is False
        finally:
            service_app.state.draining = False


def test_saturation_sheds_instead_of_queueing(client):
    """At capacity the service returns 503 rather than growing an invisible backlog.

    Capacity is forced to 1 and the provider made slow, so the second concurrent
    request has nowhere to go. Unbounded queueing would turn a throughput problem into
    a latency problem and then a memory problem.
    """
    provider = StubProvider()
    released = asyncio.Event()

    async def slow(system, question, temperature, *, grounded=None):
        await released.wait()
        return _response()

    provider.ask_generic_question = slow
    with client(provider, SERVICE_CAPACITY="1") as c:
        assert service_app.state.capacity == 1
        # Hold the only slot, then prove the next caller is shed rather than queued.
        service_app.state.gate = asyncio.Semaphore(0)
        r = c.post("/ask", json={"question": "q"})
        released.set()
        assert r.status_code == 503
        assert r.json()["error"] == "at capacity"

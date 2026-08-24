"""The integration, as a service.

Why this exists
---------------
The brief asks for evidence that the integration "will not fall over when we point
real traffic at it". In production traffic arrives *at our code*, and our code then
calls Vertex. So the thing that has to survive load is this process: its connection
handling, its admission control, its event loop, its outbound pool.

An in-process asyncio driver calling the provider directly (``harness/run.py``)
exercises none of that. It is the right shape for a batch job, but it is both the
load generator and the system under test, so it cannot answer "what happens when 200
requests per second arrive from outside?"

So k6 points here, and the same k6 script can point straight at Vertex as a baseline.
The difference between those two runs is the cost of our integration:

    overhead = (latency through this service) - (latency calling Vertex directly)

That is decomposed per request so a regression can be attributed rather than guessed:

    total = queue_wait + upstream + framework/serialization

Admission control
-----------------
Requests are gated by a semaphore sized to ``provider.parallelism()``. Past that the
service returns **503 with Retry-After** instead of queueing without bound. Unbounded
queueing turns a throughput problem into a latency problem and then into a memory
problem; shedding early keeps the failure legible and lets callers back off. Wait time
at the gate is measured, because a quietly growing queue is the earliest saturation
signal.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from llm.errors import (
    LLMAuthenticationError,
    LLMContentBlockedError,
    LLMEmptyResponseError,
    LLMError,
    LLMRateLimitError,
)
from llm.gemini import Gemini
from llm.metrics import (
    REGISTRY,
    EventLoopLagMonitor,
    budget_remaining_usd,
    service_admission_rejected_total,
    service_inflight,
    service_overhead_seconds,
    service_queue_wait_seconds,
    service_request_duration_seconds,
    service_requests_total,
)


class AskRequest(BaseModel):
    question: str = Field(..., max_length=8000)
    system_prompt: str = "You are a market research assistant. Answer concisely."
    temperature: float = 0.7


class AskResponse(BaseModel):
    answer: str
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    finish_reason: str
    cost_usd: float
    upstream_ms: float
    overhead_ms: float
    queue_wait_ms: float
    attempts: int


class ServiceState:
    def __init__(self) -> None:
        self.provider: Gemini | None = None
        self.gate: asyncio.Semaphore | None = None
        self.lag = EventLoopLagMonitor()
        self.inflight = 0
        self.capacity = 0
        # Optional spend ceiling. A long-running service pointed at a metered API
        # should be able to stop itself; without this a runaway loop bills until
        # someone notices. Unset means no ceiling.
        self.budget_usd = float(os.getenv("SERVICE_BUDGET_USD", "0") or 0)
        self.spent_usd = 0.0


state = ServiceState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One provider per process, so every request shares one connection pool.
    # Constructing per request would defeat pooling and hide the pool ceiling, which
    # is precisely the bug this exercise exists to catch.
    state.provider = Gemini()
    state.capacity = int(os.getenv("SERVICE_CAPACITY", state.provider.parallelism()))
    state.gate = asyncio.Semaphore(state.capacity)
    if state.budget_usd > 0:
        budget_remaining_usd.set(state.budget_usd)
    state.lag.start()
    yield
    await state.lag.stop()


app = FastAPI(title="gemini-integration", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    provider = state.provider
    return {
        "ok": provider is not None,
        "capacity": state.capacity,
        "inflight": state.inflight,
        "provider": provider.describe() if provider else None,
    }


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.post("/ask")
async def ask(payload: AskRequest) -> JSONResponse:
    provider, gate = state.provider, state.gate
    if provider is None or gate is None:
        return JSONResponse({"error": "service not ready"}, status_code=503)

    started = time.perf_counter()

    if state.budget_usd > 0 and state.spent_usd >= state.budget_usd:
        service_requests_total.labels(outcome="over_budget", finish_reason="").inc()
        return JSONResponse(
            {
                "error": "spend ceiling reached",
                "spent_usd": round(state.spent_usd, 6),
                "budget_usd": state.budget_usd,
            },
            status_code=503,
        )

    # Shed rather than queue without bound. Checked before awaiting so a saturated
    # service rejects immediately instead of growing an invisible backlog.
    if gate.locked():
        service_admission_rejected_total.inc()
        service_requests_total.labels(outcome="rejected", finish_reason="").inc()
        return JSONResponse(
            {"error": "at capacity", "capacity": state.capacity},
            status_code=503,
            headers={"Retry-After": "1"},
        )

    async with gate:
        queue_wait = time.perf_counter() - started
        service_queue_wait_seconds.observe(queue_wait)
        state.inflight += 1
        service_inflight.set(state.inflight)
        try:
            result = await provider.ask_generic_question(
                payload.system_prompt, payload.question, payload.temperature
            )
        except (LLMEmptyResponseError, LLMContentBlockedError) as exc:
            service_request_duration_seconds.labels(outcome="unusable").observe(
                time.perf_counter() - started
            )
            service_requests_total.labels(
                outcome="unusable", finish_reason=exc.error_class
            ).inc()
            return JSONResponse(
                {"error": exc.error_class, "detail": str(exc)}, status_code=422
            )
        except LLMRateLimitError as exc:
            service_request_duration_seconds.labels(outcome="rate_limited").observe(
                time.perf_counter() - started
            )
            service_requests_total.labels(
                outcome="rate_limited", finish_reason=exc.error_class
            ).inc()
            return JSONResponse(
                {"error": exc.error_class, "detail": str(exc)},
                status_code=429,
                headers={"Retry-After": str(int(exc.retry_after_s or 1))},
            )
        except LLMAuthenticationError as exc:
            return JSONResponse(
                {"error": exc.error_class, "detail": str(exc)}, status_code=401
            )
        except LLMError as exc:
            service_request_duration_seconds.labels(outcome="error").observe(
                time.perf_counter() - started
            )
            service_requests_total.labels(
                outcome="error", finish_reason=exc.error_class
            ).inc()
            return JSONResponse(
                {"error": exc.error_class, "detail": str(exc)}, status_code=502
            )
        finally:
            state.inflight -= 1
            service_inflight.set(state.inflight)

    total = time.perf_counter() - started
    upstream_s = (result.latency_ms or 0.0) / 1000.0

    if state.budget_usd > 0:
        state.spent_usd += result.cost_usd or 0.0
        budget_remaining_usd.set(max(0.0, state.budget_usd - state.spent_usd))

    # The headline number: everything that was not waiting on Vertex. Framework,
    # validation, JSON, event-loop scheduling, admission queueing.
    overhead = max(0.0, total - upstream_s)
    service_request_duration_seconds.labels(outcome="success").observe(total)
    service_overhead_seconds.observe(overhead)
    service_requests_total.labels(
        outcome="success", finish_reason=result.finish_reason.value
    ).inc()

    return JSONResponse(
        AskResponse(
            answer=result.answer or "",
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            thinking_tokens=result.thinking_tokens,
            finish_reason=result.finish_reason.value,
            cost_usd=result.cost_usd or 0.0,
            upstream_ms=round(upstream_s * 1000, 2),
            overhead_ms=round(overhead * 1000, 2),
            queue_wait_ms=round(queue_wait * 1000, 2),
            attempts=result.attempts,
        ).model_dump()
    )


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the Gemini integration service.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Uvicorn worker processes. One process is GIL-bound; more workers is the "
            "standard way past that, at the cost of one connection pool each."
        ),
    )
    args = parser.parse_args()
    if args.workers > 1:
        uvicorn.run(
            "service.app:app",
            host=args.host,
            port=args.port,
            workers=args.workers,
            log_level="warning",
        )
    else:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

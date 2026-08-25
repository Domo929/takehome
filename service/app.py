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
import logging
import os
import signal
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
from llm.logging_setup import configure as configure_logging
from llm.logging_setup import get_logger, log_failure
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
    service_retry_backoff_seconds,
    service_upstream_seconds,
)


class AskRequest(BaseModel):
    question: str = Field(..., max_length=8000)
    system_prompt: str = "You are a market research assistant. Answer concisely."
    # Measured optimum and the model's own default (FINDINGS 0e). Set explicitly
    # rather than left unset: the effective default lives server-side.
    temperature: float = 1.0
    # Per request, because the two measurement conditions run over the same prompts
    # and should share one connection pool. None means "use the service default".
    grounded: bool | None = None


class AskResponse(BaseModel):
    answer: str
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    finish_reason: str
    cost_usd: float
    upstream_ms: float
    retry_backoff_ms: float
    # What actually happened, which is not necessarily what was asked for.
    grounded: bool = False
    grounding_sources: list[str] = []
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
        # Set on SIGTERM/SIGINT. New work is refused while in-flight work finishes;
        # for a batch worker, dropping requests mid-flight means paying for tokens
        # whose answers are thrown away.
        self.draining = False


state = ServiceState()
logger = get_logger("service")


@asynccontextmanager
async def _admission(limiter, gate):
    """Hold an admission permit from whichever gate is in use.

    The adaptive limiter grants its permit in ``try_acquire`` above, so here it only
    needs releasing; the fixed semaphore is acquired and released normally.
    """
    if limiter is not None:
        try:
            yield
        finally:
            limiter.release()
    else:
        async with gate:
            yield


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    # One provider per process, so every request shares one connection pool.
    # Constructing per request would defeat pooling and hide the pool ceiling, which
    # is precisely the bug this exercise exists to catch.
    state.provider = Gemini()
    state.capacity = int(os.getenv("SERVICE_CAPACITY", state.provider.parallelism()))
    # A fixed semaphore cannot represent a limit that moves, so when adaptive limiting
    # is enabled the controller owns admission instead.
    state.gate = None if state.provider.limiter else asyncio.Semaphore(state.capacity)
    if state.budget_usd > 0:
        budget_remaining_usd.set(state.budget_usd)
    state.lag.start()

    loop = asyncio.get_running_loop()

    def _drain(sig: int) -> None:
        # Flip the flag and let in-flight requests finish. uvicorn's own handler then
        # stops the server once connections close.
        state.draining = True
        logger.warning(
            "draining on signal",
            signal=signal.Signals(sig).name,
            inflight=state.inflight,
        )

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _drain, sig)
        except (NotImplementedError, RuntimeError):
            # Not available on every platform, and uvicorn may already own it.
            pass

    logger.info(
        "service ready",
        capacity=state.capacity,
        adaptive=state.provider.limiter is not None,
        **state.provider.describe(),
    )

    yield

    deadline = time.monotonic() + float(os.getenv("SERVICE_DRAIN_TIMEOUT_S", "30"))
    while state.inflight > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    if state.inflight:
        logger.warning("drain timed out", abandoned=state.inflight)
    else:
        logger.info("drained cleanly", spent_usd=round(state.spent_usd, 6))
    await state.lag.stop()


app = FastAPI(title="gemini-integration", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    provider = state.provider
    limiter = provider.limiter if provider else None
    return {
        "ok": provider is not None and not state.draining,
        "draining": state.draining,
        "capacity": limiter.limit if limiter else state.capacity,
        "adaptive": limiter.snapshot() if limiter else None,
        "inflight": state.inflight,
        "provider": provider.describe() if provider else None,
    }


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.post("/ask")
async def ask(payload: AskRequest) -> JSONResponse:
    provider = state.provider
    if provider is None:
        return JSONResponse({"error": "service not ready"}, status_code=503)
    limiter = provider.limiter
    gate = state.gate

    if state.draining:
        return JSONResponse(
            {"error": "shutting down"}, status_code=503, headers={"Retry-After": "5"}
        )

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
    admitted = limiter.try_acquire() if limiter is not None else not gate.locked()
    if not admitted:
        service_admission_rejected_total.inc()
        service_requests_total.labels(outcome="rejected", finish_reason="").inc()
        return JSONResponse(
            {
                "error": "at capacity",
                "capacity": limiter.limit if limiter else state.capacity,
            },
            status_code=503,
            headers={"Retry-After": "1"},
        )

    async with _admission(limiter, gate):
        queue_wait = time.perf_counter() - started
        service_queue_wait_seconds.observe(queue_wait)
        state.inflight += 1
        service_inflight.set(state.inflight)
        try:
            result = await provider.ask_generic_question(
                payload.system_prompt,
                payload.question,
                payload.temperature,
                grounded=payload.grounded,
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
            log_failure(
                logger,
                "upstream failure returned to caller",
                error=exc,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                question_chars=len(payload.question),
                inflight=state.inflight,
            )
            return JSONResponse(
                {"error": exc.error_class, "detail": str(exc)}, status_code=502
            )
        finally:
            state.inflight -= 1
            service_inflight.set(state.inflight)

    total = time.perf_counter() - started

    # Vendor time across ALL attempts, not just the last one. Using only the final
    # attempt made a retried request look as though our layer had stalled for the
    # duration of the failed attempts plus their backoff, which produced p99
    # "overhead" in the seconds on a path whose real cost is a fraction of a
    # millisecond.
    upstream_s = (result.upstream_total_ms or result.latency_ms or 0.0) / 1000.0
    backoff_s = (result.retry_backoff_ms or 0.0) / 1000.0

    if state.budget_usd > 0:
        state.spent_usd += result.cost_usd or 0.0
        budget_remaining_usd.set(max(0.0, state.budget_usd - state.spent_usd))

    # Everything that is genuinely ours: framework, validation, JSON, event-loop
    # scheduling, admission queueing. Vendor time and deliberate retry sleep are both
    # excluded because neither is a cost our code can reduce.
    overhead = max(0.0, total - upstream_s - backoff_s)
    service_request_duration_seconds.labels(outcome="success").observe(total)
    service_overhead_seconds.observe(overhead)
    service_upstream_seconds.observe(upstream_s)
    service_retry_backoff_seconds.observe(backoff_s)
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
            retry_backoff_ms=round(backoff_s * 1000, 2),
            overhead_ms=round(overhead * 1000, 2),
            queue_wait_ms=round(queue_wait * 1000, 2),
            attempts=result.attempts,
            grounded=result.grounded,
            grounding_sources=result.grounding_sources,
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

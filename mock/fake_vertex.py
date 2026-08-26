"""A fake Vertex / Gemini endpoint.

Why this exists
---------------
An in-process mock of the ``LLM`` class validates harness logic but exercises none of
the machinery that actually breaks under load: no HTTP, no connection pool, no TLS,
no JSON parsing, no SDK. Those are exactly the layers where an async client silently
caps out. A real HTTP server on localhost exercises the full stack for $0, and it can
serve the k6 control harness too, which an in-process mock cannot do at all.

What it emulates
----------------
The ``:generateContent`` contract, plus the failure modes that matter:

* latency drawn from a lognormal distribution, so p99 is meaningfully worse than p50
* a configurable concurrency knee, past which latency inflates rather than erroring,
  which is how Vertex actually degrades
* 429 with ``Retry-After`` once past a saturation threshold
* ``MAX_TOKENS`` truncation when the thinking budget swallows the output allowance
* safety blocks returning HTTP 200 with no candidate text
* empty 200 responses with no text at all
* configurable 5xx

Every behavior is tunable so a test can force a specific failure deterministically.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


@dataclass
class Behavior:
    """Tunable failure/latency profile."""

    base_latency_s: float = 0.45
    latency_sigma: float = 0.35
    per_output_token_s: float = 0.0035

    # Past this many concurrent requests, latency inflates instead of failing.
    knee_concurrency: int = 64
    # Past this, 429s begin.
    saturation_concurrency: int = 220
    inflation_factor: float = 2.5

    rate_limit_probability: float = 0.0
    server_error_probability: float = 0.0
    empty_probability: float = 0.0
    safety_probability: float = 0.0
    truncate_probability: float = 0.0

    retry_after_s: float = 1.0
    seed: int | None = None

    # Grounding: calibrated against 20 paired real Vertex requests (see FINDINGS 2).
    # An earlier version of this mock assumed retrieved passages were billed as prompt
    # tokens (6x inflation). Measurement falsified that: input tokens are *identical*
    # grounded or not, so retrieval is priced entirely in the per-prompt SKU. What does
    # change is the answer: grounded responses ran 1.9x longer, which is what drives
    # the truncation risk.
    # Fraction of grounded requests that come back 200 OK with no groundingMetadata:
    # the model declined to search, or retrieval failed. Real and untestable against
    # the vendor on demand, which is exactly why the mock has to be able to produce it.
    grounding_failure_rate: float = 0.0
    grounding_latency_s: float = 2.4
    grounding_output_token_multiplier: float = 1.9

    # Token generation model
    prompt_tokens_per_char: float = 0.27
    output_tokens_mean: int = 180
    output_tokens_sigma: int = 60
    # How much the model *wants* to think, independent of answer length. Thinking
    # demand tracks question difficulty, not response size, which is why a generous
    # thinking budget can consume an entire output allowance and leave no answer.
    thinking_demand_mean: int = 600
    thinking_demand_sigma: int = 200

    _rng: random.Random = field(default_factory=random.Random, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)


class FakeVertexState:
    def __init__(self, behavior: Behavior) -> None:
        self.behavior = behavior
        self.inflight = 0
        self.peak_inflight = 0
        self.total_requests = 0
        self.responses_by_kind: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def enter(self) -> int:
        async with self._lock:
            self.inflight += 1
            self.total_requests += 1
            self.peak_inflight = max(self.peak_inflight, self.inflight)
            return self.inflight

    async def leave(self) -> None:
        async with self._lock:
            self.inflight -= 1

    def note(self, kind: str) -> None:
        self.responses_by_kind[kind] = self.responses_by_kind.get(kind, 0) + 1


LOREM = (
    "Based on current reviews and specifications, the leading options are the Roborock "
    "S8 Pro Ultra, the iRobot Roomba j9+, the Dreame L20 Ultra, and the Eufy X10 Pro "
    "Omni. Each balances suction, mapping accuracy, and self-emptying differently. "
)


def _make_text(n_tokens: int) -> str:
    """Roughly ``n_tokens`` worth of text, at ~0.75 words per token."""
    words = LOREM.split()
    if not words:
        return ""
    target_words = max(1, int(n_tokens * 0.75))
    return " ".join(words[i % len(words)] for i in range(target_words))


def create_app(behavior: Behavior | None = None) -> FastAPI:
    state = FakeVertexState(behavior or Behavior())
    app = FastAPI(title="fake-vertex")
    app.state.fake = state

    @app.get("/__stats")
    async def stats() -> dict:
        return {
            "inflight": state.inflight,
            "peak_inflight": state.peak_inflight,
            "total_requests": state.total_requests,
            "responses_by_kind": state.responses_by_kind,
        }

    @app.post("/__configure")
    async def configure(request: Request) -> dict:
        """Re-tune behavior at runtime so one server can drive many scenarios."""
        payload = await request.json()
        for key, value in payload.items():
            if hasattr(state.behavior, key) and not key.startswith("_"):
                setattr(state.behavior, key, value)
        if "seed" in payload:
            state.behavior._rng = random.Random(payload["seed"])
        return {"ok": True}

    @app.post("/__reset")
    async def reset() -> dict:
        state.inflight = 0
        state.peak_inflight = 0
        state.total_requests = 0
        state.responses_by_kind = {}
        # Injected failure modes reset too. Without this a caller that configures a
        # failure and then dies leaves it set for everyone after it.
        state.behavior = replace(behavior)
        return {"ok": True}

    # Vertex and the developer API use different path shapes; accept both so one
    # server backs either backend setting.
    @app.post("/{full_path:path}")
    async def generate(full_path: str, request: Request) -> JSONResponse:
        if ":generatecontent" not in full_path.lower():
            return JSONResponse({"error": {"message": "not found", "code": 404}}, status_code=404)

        b = state.behavior
        rng = b._rng
        body = await request.json()

        inflight = await state.enter()
        try:
            if inflight > b.saturation_concurrency or rng.random() < b.rate_limit_probability:
                state.note("429")
                return JSONResponse(
                    {
                        "error": {
                            "code": 429,
                            "status": "RESOURCE_EXHAUSTED",
                            "message": (
                                "Quota exceeded for aiplatform.googleapis.com/"
                                "generate_content_requests_per_minute_per_project_per_base_model."
                            ),
                        }
                    },
                    status_code=429,
                    headers={"Retry-After": str(b.retry_after_s)},
                )

            if rng.random() < b.server_error_probability:
                state.note("503")
                return JSONResponse(
                    {
                        "error": {
                            "code": 503,
                            "status": "UNAVAILABLE",
                            "message": "The service is currently unavailable.",
                        }
                    },
                    status_code=503,
                )

            contents = body.get("contents") or []
            text_in = ""
            for item in contents:
                for part in item.get("parts") or []:
                    text_in += part.get("text") or ""
            # Grounding is requested via tools, not generationConfig.
            grounded = any(
                "googleSearch" in t or "google_search" in t
                for t in (body.get("tools") or [])
            )

            config = body.get("generationConfig") or {}
            max_output = int(config.get("maxOutputTokens") or config.get("max_output_tokens") or 1024)
            thinking_cfg = config.get("thinkingConfig") or config.get("thinking_config") or {}
            # Vertex accepts either spelling, but they are the same protobuf oneof
            # field, so supplying both is rejected. Mirrored here: a mock more
            # permissive than production hides exactly this class of bug, which is
            # how a both-spellings payload survived local testing before failing
            # against the real endpoint.
            has_camel = "thinkingBudget" in thinking_cfg
            has_snake = "thinking_budget" in thinking_cfg
            if has_camel and has_snake:
                state.note("400_oneof")
                return JSONResponse(
                    {
                        "error": {
                            "code": 400,
                            "status": "INVALID_ARGUMENT",
                            "message": (
                                "Invalid value at 'generation_config.thinking_config' "
                                "(oneof), oneof field '_thinking_budget' is already set."
                            ),
                        }
                    },
                    status_code=400,
                )
            thinking_budget = thinking_cfg.get("thinkingBudget")
            if thinking_budget is None:
                thinking_budget = thinking_cfg.get("thinking_budget")
            if thinking_budget is None:
                thinking_budget = -1

            prompt_tokens = max(1, int(len(text_in) * b.prompt_tokens_per_char))

            wanted = max(1, int(rng.gauss(b.output_tokens_mean, b.output_tokens_sigma)))

            # The headline quirk: thinking and visible output share one allowance. A
            # budget at or above the cap leaves nothing for the answer, and every
            # token is billed regardless.
            demand = max(0, int(rng.gauss(b.thinking_demand_mean, b.thinking_demand_sigma)))
            if thinking_budget == -1:
                # Dynamic thinking: the model spends whatever it wants. This is the
                # SDK default, and it is the footgun.
                thinking = demand
            elif thinking_budget > 0:
                thinking = min(thinking_budget, demand)
            else:
                thinking = 0

            truncated = False
            if thinking >= max_output:
                thinking = max_output
                visible = 0
                truncated = True
            elif thinking + wanted > max_output:
                visible = max(0, max_output - thinking)
                truncated = True
            else:
                visible = wanted

            if rng.random() < b.truncate_probability:
                visible = max(0, min(visible, 20))
                truncated = True

            # Latency: base + per-token cost, inflated past the knee, lognormal jitter.
            latency = b.base_latency_s + (visible + thinking) * b.per_output_token_s
            if inflight > b.knee_concurrency:
                overload = (inflight - b.knee_concurrency) / max(1, b.knee_concurrency)
                latency *= 1.0 + overload * (b.inflation_factor - 1.0)
            if grounded:
                latency += b.grounding_latency_s
            latency *= rng.lognormvariate(0.0, b.latency_sigma)
            await asyncio.sleep(max(0.0, latency))

            usage = {
                "promptTokenCount": prompt_tokens,
                "candidatesTokenCount": visible,
                "totalTokenCount": prompt_tokens + visible + thinking,
                "trafficType": "ON_DEMAND",
            }
            if thinking:
                usage["thoughtsTokenCount"] = thinking

            response_id = str(uuid.uuid4())

            if rng.random() < b.safety_probability:
                state.note("safety")
                return JSONResponse(
                    {
                        "candidates": [
                            {"finishReason": "SAFETY", "index": 0, "safetyRatings": []}
                        ],
                        "usageMetadata": usage,
                        "modelVersion": "gemini-2.5-flash",
                        "responseId": response_id,
                    }
                )

            if rng.random() < b.empty_probability:
                state.note("empty")
                return JSONResponse(
                    {
                        "candidates": [
                            {
                                "finishReason": "STOP",
                                "index": 0,
                                "content": {"role": "model", "parts": []},
                            }
                        ],
                        "usageMetadata": usage,
                        "modelVersion": "gemini-2.5-flash",
                        "responseId": response_id,
                    }
                )

            if truncated and visible == 0:
                state.note("max_tokens_empty")
                return JSONResponse(
                    {
                        "candidates": [
                            {
                                "finishReason": "MAX_TOKENS",
                                "index": 0,
                                "content": {"role": "model", "parts": []},
                            }
                        ],
                        "usageMetadata": usage,
                        "modelVersion": "gemini-2.5-flash",
                        "responseId": response_id,
                    }
                )

            state.note("grounded" if grounded else ("truncated" if truncated else "ok"))

            candidate: dict[str, Any] = {
                "finishReason": "MAX_TOKENS" if truncated else "STOP",
                "index": 0,
                "content": {"role": "model", "parts": [{"text": _make_text(visible)}]},
            }
            grounding_ran = grounded and rng.random() >= b.grounding_failure_rate
            if grounding_ran:
                candidate["groundingMetadata"] = {
                    "webSearchQueries": ["best robot vacuum brands 2026"],
                    "groundingChunks": [
                        {"web": {"uri": "https://example.com/reviews/robot-vacuums",
                                 "title": "Best robot vacuums"}},
                        {"web": {"uri": "https://example.com/buying-guide",
                                 "title": "Buying guide"}},
                    ],
                }
            return JSONResponse(
                {
                    "candidates": [candidate],
                    "usageMetadata": usage,
                    "modelVersion": "gemini-2.5-flash",
                    "responseId": response_id,
                }
            )
        finally:
            await state.leave()

    return app


def build_behavior_from_env() -> Behavior:
    b = Behavior()
    float_fields = (
        "grounding_failure_rate",
        "grounding_latency_s", "grounding_output_token_multiplier",
        "base_latency_s", "latency_sigma", "per_output_token_s", "inflation_factor",
        "rate_limit_probability", "server_error_probability", "empty_probability",
        "safety_probability", "truncate_probability", "retry_after_s",
    )
    for name in float_fields:
        raw = os.getenv(f"FAKE_{name.upper()}")
        if raw:
            setattr(b, name, float(raw))
    int_fields = (
        "knee_concurrency", "saturation_concurrency", "output_tokens_mean",
        "output_tokens_sigma", "thinking_demand_mean", "thinking_demand_sigma",
    )
    for name in int_fields:
        raw = os.getenv(f"FAKE_{name.upper()}")
        if raw:
            setattr(b, name, int(raw))
    seed = os.getenv("FAKE_SEED")
    if seed:
        b.seed = int(seed)
        b._rng = random.Random(b.seed)
    return b


app = create_app(build_behavior_from_env())


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Fake Vertex AI endpoint for $0 load testing.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--knee", type=int, default=64)
    parser.add_argument("--saturation", type=int, default=220)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    behavior = build_behavior_from_env()
    behavior.knee_concurrency = args.knee
    behavior.saturation_concurrency = args.saturation
    if args.seed is not None:
        behavior.seed = args.seed
        behavior._rng = random.Random(args.seed)

    uvicorn.run(create_app(behavior), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

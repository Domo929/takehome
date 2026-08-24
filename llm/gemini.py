"""Gemini 2.5 Flash provider.

Backend selection
-----------------
``google-genai`` speaks to two different services with one client surface: Vertex AI
(ADC credentials, project + location) and the Gemini Developer API (an API key).
Vertex is the production target; the developer API is useful for functional iteration
when Vertex credentials are not available. They are *not* interchangeable for
capacity work — different quota pools, different endpoints, different scaling
behavior. Anything measured against the developer API is a smoke test, not evidence.

Notable deviations from the Together provider
---------------------------------------------
*System prompt is not a message.* Gemini takes it as ``system_instruction`` on the
config, not as a role in the message list.

*Thinking is disabled by default.* ``thinking_budget`` and ``max_output_tokens`` draw
on one shared allowance, so a model left to think freely can spend the entire output
budget reasoning and return an empty or fragmentary answer that still bills in full.
Off by default; turn it on deliberately.

*The connection pool is sized explicitly.* The default pool is a hard throughput
ceiling that no vendor response will ever tell you about.

*SDK-level retries stay off.* They would hide 429s beneath our instrumentation.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

# The SDK logs an automatic-function-calling advisory on every generate_content call.
# We pass no tools, so it is pure noise that would drown a load-test log.
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

from .adaptive import AdaptiveConfig, AdaptiveLimiter, Outcome
from .errors import (
    LLMAuthenticationError,
    LLMContentBlockedError,
    LLMEmptyResponseError,
    LLMError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMServerError,
)
from .llm import LLM
from .response import FinishReason, GeminiResponse
from .metrics import (
    adaptive_baseline_rtt,
    adaptive_drops_total,
    adaptive_gradient,
    adaptive_limit,
    adaptive_short_rtt,
    empty_responses_total,
    inflight_requests,
    pool_saturation_ratio,
    pool_size,
    request_duration,
    requests_total,
    retry_attempts_total,
    retry_budget_tokens,
    spend_usd_total,
    tokens_total,
)
from .pricing import cost_usd
from .retry import RetryBudget, RetryOutcome, RetryPolicy, parse_retry_after, with_retries

_PROVIDER = "gemini"

_BLOCKED_REASONS = {
    FinishReason.SAFETY,
    FinishReason.RECITATION,
    FinishReason.BLOCKLIST,
    FinishReason.PROHIBITED_CONTENT,
    FinishReason.SPII,
}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _coerce_finish_reason(raw: Any) -> FinishReason:
    if raw is None:
        return FinishReason.UNKNOWN
    name = getattr(raw, "name", None) or str(raw)
    name = name.rsplit(".", 1)[-1].upper()
    try:
        return FinishReason(name)
    except ValueError:
        return FinishReason.OTHER


class Gemini(LLM):
    def __init__(
        self,
        *,
        backend: str | None = None,
        project: str | None = None,
        location: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        thinking_budget: int | None = None,
        max_connections: int | None = None,
        parallelism_limit: int | None = None,
        retry_policy: RetryPolicy | None = None,
        base_url: str | None = None,
        adaptive: bool | None = None,
        adaptive_config: AdaptiveConfig | None = None,
    ) -> None:
        # "vertex" | "developer". Explicit beats inferred: silently falling back to a
        # different backend than intended would invalidate every number we collect.
        self._backend = (backend or os.getenv("GEMINI_BACKEND") or "vertex").lower()
        if self._backend not in {"vertex", "developer"}:
            raise ValueError(f"Unknown GEMINI_BACKEND {self._backend!r}")

        self._model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self._max_output_tokens = max_output_tokens or _env_int("GEMINI_MAX_OUTPUT_TOKENS", 1024)
        self._thinking_budget = (
            thinking_budget if thinking_budget is not None else _env_int("GEMINI_THINKING_BUDGET", 0)
        )

        # Pool sizing drives the achievable ceiling, so parallelism is derived from it
        # rather than chosen independently. Headroom above in-flight count absorbs
        # connection churn without letting the pool become the silent constraint.
        self._max_connections = max_connections or _env_int("GEMINI_MAX_CONNECTIONS", 256)
        self._parallelism = parallelism_limit or _env_int(
            "GEMINI_PARALLELISM", max(1, int(self._max_connections / 2.5))
        )

        self._retry_policy = retry_policy or RetryPolicy(
            max_attempts=_env_int("GEMINI_MAX_ATTEMPTS", 4),
            attempt_timeout_s=_env_float("GEMINI_ATTEMPT_TIMEOUT_S", 60.0),
            total_deadline_s=_env_float("GEMINI_TOTAL_DEADLINE_S", 180.0),
            budget=RetryBudget(
                capacity=_env_float("GEMINI_RETRY_BUDGET_CAPACITY", 100.0),
                refill_per_attempt=_env_float("GEMINI_RETRY_BUDGET_REFILL", 0.1),
            ),
        )

        limits = httpx.Limits(
            max_connections=self._max_connections,
            max_keepalive_connections=self._max_connections,
        )
        http_options = types.HttpOptions(
            # Pin the stable surface; "v1beta1" drifts under us.
            api_version=None if self._backend == "developer" else "v1",
            base_url=base_url or os.getenv("GEMINI_BASE_URL") or None,
            async_client_args={"limits": limits},
            # retry_options intentionally unset: retries belong above, where they
            # are visible to metrics.
        )

        if self._backend == "vertex":
            resolved_project = project or os.getenv("GOOGLE_CLOUD_PROJECT")
            resolved_location = location or os.getenv("GOOGLE_CLOUD_LOCATION", "global")
            if not resolved_project:
                raise ValueError(
                    "Vertex backend requires a project: set GOOGLE_CLOUD_PROJECT or pass project=."
                )
            self._client = genai.Client(
                vertexai=True,
                project=resolved_project,
                location=resolved_location,
                http_options=http_options,
            )
            self._location = resolved_location
        else:
            resolved_key = api_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
            if not resolved_key:
                raise ValueError(
                    "Developer backend requires an API key: set GOOGLE_API_KEY or pass api_key=."
                )
            self._client = genai.Client(api_key=resolved_key, http_options=http_options)
            self._location = "developer-api"

        pool_size.labels(provider=_PROVIDER).set(self._max_connections)
        self._inflight = 0

        # Adaptive limiting is opt-in. A fixed limit is easier to reason about when
        # the backend's capacity really is fixed; this earns its place against shared
        # quota, where the ceiling moves.
        if adaptive is None:
            adaptive = os.getenv("GEMINI_ADAPTIVE", "").lower() in ("1", "true", "yes")
        self._limiter: AdaptiveLimiter | None = None
        if adaptive:
            config = adaptive_config or AdaptiveConfig(
                initial_limit=float(self._parallelism),
                min_limit=float(_env_int("GEMINI_ADAPTIVE_MIN", 1)),
                # Never exceed the connection pool: permits beyond it would queue on
                # sockets instead of on the gate, which is exactly the invisible
                # queueing the limiter exists to prevent.
                max_limit=float(min(self._max_connections, _env_int("GEMINI_ADAPTIVE_MAX", 512))),
            )
            self._limiter = AdaptiveLimiter(config)
            self._publish_limiter()

    def _publish_limiter(self) -> None:
        if self._limiter is None:
            return
        st = self._limiter.state
        adaptive_limit.labels(provider=_PROVIDER).set(st.limit)
        adaptive_gradient.labels(provider=_PROVIDER).set(st.gradient)
        adaptive_baseline_rtt.labels(provider=_PROVIDER).set(st.baseline_rtt_s or 0.0)
        adaptive_short_rtt.labels(provider=_PROVIDER).set(st.short_rtt_s or 0.0)

    @property
    def limiter(self) -> AdaptiveLimiter | None:
        return self._limiter

    # -- introspection -------------------------------------------------------

    @property
    def model(self) -> str:
        return self._model

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def location(self) -> str:
        return self._location

    @property
    def max_connections(self) -> int:
        return self._max_connections

    def parallelism(self) -> int:
        # With adaptive limiting on, this is a live measurement rather than config.
        if self._limiter is not None:
            return self._limiter.limit
        return self._parallelism

    def describe(self) -> dict[str, Any]:
        """Configuration snapshot, recorded in run manifests for reproducibility."""
        return {
            "provider": _PROVIDER,
            "backend": self._backend,
            "model": self._model,
            "location": self._location,
            "max_output_tokens": self._max_output_tokens,
            "thinking_budget": self._thinking_budget,
            "max_connections": self._max_connections,
            "parallelism": self._parallelism,
            "max_attempts": self._retry_policy.max_attempts,
            "attempt_timeout_s": self._retry_policy.attempt_timeout_s,
            "total_deadline_s": self._retry_policy.total_deadline_s,
        }

    # -- error translation ---------------------------------------------------

    def _translate(self, exc: Exception) -> LLMError:
        """Map SDK exceptions onto the provider-neutral taxonomy."""
        if isinstance(exc, genai_errors.APIError):
            status = getattr(exc, "code", None)
            message = getattr(exc, "message", None) or str(exc)
            retry_after = None
            response = getattr(exc, "response", None)
            headers = getattr(response, "headers", None)
            if headers:
                retry_after = parse_retry_after(
                    headers.get("Retry-After") or headers.get("retry-after")
                )
                if retry_after is None:
                    ms = headers.get("Retry-After-ms") or headers.get("retry-after-ms")
                    if ms:
                        try:
                            retry_after = float(ms) / 1000.0
                        except ValueError:
                            retry_after = None

            kwargs = {
                "provider": _PROVIDER,
                "status_code": status,
                "retry_after_s": retry_after,
            }
            if status == 429:
                return LLMRateLimitError(message, **kwargs)
            if status in (401, 403):
                return LLMAuthenticationError(message, **kwargs)
            if status in (408, 499):
                # 499 is client-cancelled; upstream shed us, so it is worth another try.
                return LLMServerError(message, **kwargs)
            if status is not None and 500 <= status < 600:
                return LLMServerError(message, **kwargs)
            if status is not None and 400 <= status < 500:
                return LLMInvalidRequestError(message, **kwargs)
            return LLMServerError(message, **kwargs)

        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
            return LLMServerError(f"transport: {exc}", provider=_PROVIDER)

        return LLMServerError(f"unexpected: {exc!r}", provider=_PROVIDER)

    # -- response parsing ----------------------------------------------------

    def _parse(self, response: Any, latency_ms: float, attempts: int) -> GeminiResponse:
        usage = getattr(response, "usage_metadata", None)
        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        visible = int(getattr(usage, "candidates_token_count", 0) or 0)
        thinking = int(getattr(usage, "thoughts_token_count", 0) or 0)

        # Billed output is visible + thinking. Falling back to total-minus-prompt keeps
        # accounting honest if the SDK omits a component, but the explicit sum avoids
        # folding in tool-use prompt tokens, which price as input.
        output_tokens = visible + thinking
        if output_tokens == 0 and usage is not None:
            total = int(getattr(usage, "total_token_count", 0) or 0)
            output_tokens = max(0, total - input_tokens)

        candidates = getattr(response, "candidates", None) or []
        finish_reason = (
            _coerce_finish_reason(getattr(candidates[0], "finish_reason", None))
            if candidates
            else FinishReason.UNKNOWN
        )

        # `.text` raises on some blocked/empty payloads rather than returning None.
        # Normalized to "" rather than None so the inherited `answer: str` contract is
        # never violated. The provider raises before returning an empty answer, so
        # callers only ever see a populated string.
        try:
            answer = response.text or ""
        except Exception:
            answer = ""
        if not answer.strip():
            answer = ""

        traffic_type = getattr(usage, "traffic_type", None)
        metadata: dict[str, Any] = {"backend": self._backend, "location": self._location}
        if traffic_type is not None:
            # Reveals whether the request drew on on-demand or provisioned quota.
            metadata["traffic_type"] = getattr(traffic_type, "name", str(traffic_type))

        cost = cost_usd(self._model, input_tokens, output_tokens)

        return GeminiResponse(
            answer=answer,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            thinking_tokens=thinking,
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            attempts=attempts,
            model=self._model,
            provider=_PROVIDER,
            cost_usd=cost,
            metadata=metadata,
        )

    def _record(self, parsed: GeminiResponse, outcome: str) -> None:
        labels = {"provider": _PROVIDER, "model": self._model}
        request_duration.labels(
            **labels, outcome=outcome, finish_reason=parsed.finish_reason.value
        ).observe((parsed.latency_ms or 0.0) / 1000.0)
        requests_total.labels(
            **labels,
            outcome=outcome,
            finish_reason=parsed.finish_reason.value,
            error_class="",
        ).inc()
        tokens_total.labels(**labels, kind="input").inc(parsed.input_tokens)
        tokens_total.labels(**labels, kind="output").inc(parsed.output_tokens)
        tokens_total.labels(**labels, kind="thinking").inc(parsed.thinking_tokens)
        spend_usd_total.labels(**labels).inc(parsed.cost_usd or 0.0)

    def _observe(self, error: LLMError | None, rtt_s: float) -> None:
        """Classify one attempt for the controller.

        Only backpressure counts as a capacity signal. A safety block or a malformed
        request says nothing about how much load the vendor can take, and treating
        those as congestion would throttle us for reasons unrelated to capacity.
        """
        if self._limiter is None:
            return
        if error is None:
            outcome = Outcome.SUCCESS
        elif isinstance(error, (LLMRateLimitError, LLMServerError)):
            outcome = Outcome.DROP
            adaptive_drops_total.labels(provider=_PROVIDER).inc()
        elif type(error).__name__ == "LLMTimeoutError":
            outcome = Outcome.DROP
            adaptive_drops_total.labels(provider=_PROVIDER).inc()
        else:
            outcome = Outcome.IGNORE
        self._limiter.observe(
            outcome=outcome, rtt_s=rtt_s, inflight=self._limiter.state.inflight or self._inflight
        )
        self._publish_limiter()

    # -- main entrypoint -----------------------------------------------------

    async def ask_generic_question(
        self, system_prompt: str, question: str, temperature: float
    ) -> GeminiResponse:
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            max_output_tokens=self._max_output_tokens,
            thinking_config=types.ThinkingConfig(thinking_budget=self._thinking_budget),
        )

        outcome_tracker = RetryOutcome()

        def _on_retry(err: LLMError, delay: float, attempt: int) -> None:
            retry_attempts_total.labels(provider=_PROVIDER, reason=err.error_class).inc()

        async def _attempt() -> GeminiResponse:
            self._inflight += 1
            inflight_requests.labels(provider=_PROVIDER).set(self._inflight)
            pool_saturation_ratio.labels(provider=_PROVIDER).set(
                self._inflight / self._max_connections
            )
            started = time.perf_counter()
            try:
                raw = await self._client.aio.models.generate_content(
                    model=self._model, contents=question, config=config
                )
            except Exception as exc:
                # Count the failed attempt's wall time as vendor time. It was spent
                # waiting on them, and omitting it would silently reappear as our
                # overhead in the caller's decomposition.
                elapsed = time.perf_counter() - started
                outcome_tracker.upstream_s += elapsed
                translated = self._translate(exc)
                self._observe(translated, elapsed)
                raise translated from exc
            finally:
                self._inflight -= 1
                inflight_requests.labels(provider=_PROVIDER).set(self._inflight)
                pool_saturation_ratio.labels(provider=_PROVIDER).set(
                    self._inflight / self._max_connections
                )

            latency_ms = (time.perf_counter() - started) * 1000.0
            outcome_tracker.upstream_s += latency_ms / 1000.0
            self._observe(None, latency_ms / 1000.0)
            parsed = self._parse(raw, latency_ms, outcome_tracker.attempts)

            if not parsed.answer:
                empty_responses_total.labels(
                    provider=_PROVIDER,
                    model=self._model,
                    finish_reason=parsed.finish_reason.value,
                ).inc()
                # Tokens were still billed even though we got nothing usable, so the
                # spend is recorded before deciding whether to retry.
                self._record(parsed, outcome="empty")

                if parsed.finish_reason in _BLOCKED_REASONS:
                    raise LLMContentBlockedError(
                        f"blocked: {parsed.finish_reason.value}",
                        provider=_PROVIDER,
                    )
                # An empty 200 is invisible to transport-level retry; only response
                # validation can catch it, so it is raised here to re-enter backoff.
                raise LLMEmptyResponseError(
                    f"empty response (finish_reason={parsed.finish_reason.value})",
                    provider=_PROVIDER,
                )

            self._record(parsed, outcome="success")
            return parsed

        try:
            result = await with_retries(
                _attempt, self._retry_policy, outcome=outcome_tracker, on_retry=_on_retry
            )
        except LLMError as err:
            requests_total.labels(
                provider=_PROVIDER,
                model=self._model,
                outcome="error",
                finish_reason="",
                error_class=err.error_class,
            ).inc()
            raise
        finally:
            if self._retry_policy.budget is not None:
                retry_budget_tokens.labels(provider=_PROVIDER).set(
                    self._retry_policy.budget.tokens
                )

        result.attempts = outcome_tracker.attempts
        result.upstream_total_ms = outcome_tracker.upstream_s * 1000.0
        result.retry_backoff_ms = outcome_tracker.backoff_s * 1000.0
        return result

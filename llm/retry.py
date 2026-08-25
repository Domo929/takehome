"""Retry engine.

Three deliberate choices:

*Retries are hand-rolled, never delegated to the SDK.* ``google-genai`` can retry
internally, but those attempts happen below our instrumentation, so a load test would
report a clean 0% error rate while quietly absorbing 429s. If we cannot see a retry we
cannot reason about capacity.

*There is a retry budget.* Unbounded per-request retries turn a partial vendor outage
into a self-inflicted traffic multiplier. A token bucket caps retries as a fraction of
overall traffic, so pressure sheds instead of amplifying.

*There are two clocks.* A per-attempt timeout bounds a single call; a separate
end-to-end deadline bounds the whole retry sequence. Without the second, three
retries of a 120s timeout is a six-minute stall.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import TypeVar

from .errors import LLMError, LLMTimeoutError

T = TypeVar("T")


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header. Accepts delta-seconds or an HTTP-date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    delta = when.timestamp() - time.time()
    return max(0.0, delta)


class RetryBudget:
    """Token bucket limiting retries as a fraction of total traffic.

    Every attempt (success or failure) refills slightly; every retry spends a token.
    Under steady healthy traffic the bucket stays full and retries are always
    available. Under a broad outage it drains, and we stop retrying rather than
    multiplying load against an already-struggling backend.
    """

    def __init__(self, capacity: float = 100.0, refill_per_attempt: float = 0.1) -> None:
        self._capacity = capacity
        self._refill = refill_per_attempt
        self._tokens = capacity
        self._lock = asyncio.Lock()

    async def record_attempt(self) -> None:
        async with self._lock:
            self._tokens = min(self._capacity, self._tokens + self._refill)

    async def try_consume(self) -> bool:
        async with self._lock:
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    @property
    def tokens(self) -> float:
        return self._tokens


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay_s: float = 0.5
    max_delay_s: float = 30.0
    attempt_timeout_s: float = 60.0
    total_deadline_s: float = 180.0
    budget: RetryBudget | None = None

    def backoff(self, attempt: int, retry_after: float | None = None) -> float:
        """Full-jitter exponential backoff, floored at Retry-After when the server sends one.

        Full jitter (uniform over [0, computed]) rather than equal jitter: clients
        retrying in lockstep is what turns one 429 into a thundering herd.
        """
        capped = min(self.max_delay_s, self.base_delay_s * (2**attempt))
        delay = random.uniform(0.0, capped)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return min(delay, self.max_delay_s)


class RetryOutcome:
    """Per-call record of what the retry engine did, for instrumentation."""

    def __init__(self) -> None:
        self.attempts = 0
        self.retries_by_reason: dict[str, int] = {}
        self.budget_exhausted = False
        # Wall time spent sleeping between attempts. Tracked separately because it is
        # neither our processing cost nor the vendor's response time: it is a
        # deliberate wait we chose. Folding it into either one misattributes it.
        self.backoff_s = 0.0
        # Upper-bound cost of attempts that failed after the vendor may already have
        # billed. Failed attempts carry no usage metadata, so this cannot be exact;
        # it errs high because a spend breaker that under-counts is not a breaker.
        self.unbilled_cost_usd = 0.0
        # Wall time spent inside vendor calls, summed across every attempt. The last
        # attempt's latency alone understates a retried request.
        self.upstream_s = 0.0

    def note_retry(self, reason: str) -> None:
        self.retries_by_reason[reason] = self.retries_by_reason.get(reason, 0) + 1


async def with_retries(
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    outcome: RetryOutcome | None = None,
    on_retry: Callable[[LLMError, float, int], None] | None = None,
) -> T:
    """Run ``fn`` under the retry policy.

    Raises the last error if every attempt fails, the budget is exhausted, or the
    end-to-end deadline passes.
    """
    outcome = outcome or RetryOutcome()
    started = time.monotonic()
    last: LLMError | None = None

    for attempt in range(policy.max_attempts):
        remaining = policy.total_deadline_s - (time.monotonic() - started)
        if remaining <= 0:
            raise last or LLMTimeoutError("Deadline exceeded before any attempt completed")

        outcome.attempts = attempt + 1
        if policy.budget is not None:
            await policy.budget.record_attempt()

        try:
            # asyncio.wait_for is a backstop, not a belt-and-braces nicety: the SDK's
            # own timeout does not reliably fire on a stalled stream, and a hung
            # request with no ceiling will park a worker indefinitely.
            return await asyncio.wait_for(
                fn(), timeout=min(policy.attempt_timeout_s, remaining)
            )
        except asyncio.TimeoutError as exc:
            last = LLMTimeoutError(
                f"Attempt {attempt + 1} exceeded {policy.attempt_timeout_s}s"
            )
            last.__cause__ = exc
        except LLMError as exc:
            last = exc
            if not exc.retryable:
                raise

        if attempt == policy.max_attempts - 1:
            break

        if policy.budget is not None and not await policy.budget.try_consume():
            outcome.budget_exhausted = True
            break

        delay = policy.backoff(attempt, getattr(last, "retry_after_s", None))
        if (time.monotonic() - started) + delay >= policy.total_deadline_s:
            break

        outcome.note_retry(last.error_class)
        if on_retry is not None:
            on_retry(last, delay, attempt + 1)
        slept_at = time.monotonic()
        await asyncio.sleep(delay)
        outcome.backoff_s += time.monotonic() - slept_at

    assert last is not None
    raise last

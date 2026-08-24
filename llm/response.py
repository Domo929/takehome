"""Gemini-specific response type.

``LLM.SimpleResponse`` is treated as a fixed contract: it is what every existing
caller already depends on, and the take-home explicitly frames the base class as
given. So rather than widen it, this module extends it.

``GeminiResponse`` is a strict extension. Every added field carries a default, no
inherited field changes type or meaning, and ``isinstance(r, LLM.SimpleResponse)``
holds. Code written against the base contract keeps working untouched; code that
knows it is talking to Gemini can reach the extra metadata.

Two things Gemini needs that the base contract has no room for:

``finish_reason`` — Gemini returns HTTP 200 with a truncated fragment when it stops
for ``MAX_TOKENS``. Without the reason, a fragment is indistinguishable from a
complete answer, and for brand-mention counting a fragment is worse than an error
because it looks like success and quietly skews the counts.

``thinking_tokens`` — reported separately by the API but billed at the output rate.
Note the split: ``output_tokens`` (inherited) is the *total billed* output and
already includes thinking, so the base contract stays correct on its own terms for
any caller that ignores this subclass. ``thinking_tokens`` is purely additive detail.

If I owned the interface I would propose promoting ``finish_reason`` into
``SimpleResponse``, since truncation is not Gemini-specific — Together exposes the
same concept as ``choices[0].finish_reason``. That is a change for whoever owns the
contract to make, not one to take unilaterally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .llm import LLM


class FinishReason(StrEnum):
    """Why generation stopped, normalized so callers never branch on vendor strings."""

    STOP = "STOP"
    MAX_TOKENS = "MAX_TOKENS"
    SAFETY = "SAFETY"
    RECITATION = "RECITATION"
    BLOCKLIST = "BLOCKLIST"
    PROHIBITED_CONTENT = "PROHIBITED_CONTENT"
    SPII = "SPII"
    MALFORMED_FUNCTION_CALL = "MALFORMED_FUNCTION_CALL"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"

    @property
    def is_usable(self) -> bool:
        return self is FinishReason.STOP


@dataclass
class GeminiResponse(LLM.SimpleResponse):
    """``LLM.SimpleResponse`` plus the metadata Gemini needs to be used safely.

    ``answer`` keeps its inherited ``str`` type. The provider raises
    ``LLMEmptyResponseError`` or ``LLMContentBlockedError`` rather than returning a
    response with no text, so a returned ``GeminiResponse`` always carries an answer
    and the base contract is never violated.
    """

    thinking_tokens: int = 0
    # True when the answer was produced with live web search enabled. This is a
    # different axis from thinking: thinking reasons harder over training data,
    # grounding injects current search results. For brand tracking the two answer
    # different questions -- what the model believes, versus what it can find today --
    # so which condition produced a sample must travel with the sample.
    grounded: bool = False

    # What we asked for, kept separate from what we got. Asking for grounding does not
    # guarantee it happens: the model may decline to search, or retrieval may fail. If
    # those two ever diverge, an ungrounded answer is about to be filed as a grounded
    # measurement, which for brand tracking is silent corruption of the primary signal.
    grounding_requested: bool = False

    @property
    def grounding_degraded(self) -> bool:
        """We asked for live search and did not get it."""
        return self.grounding_requested and not self.grounded
    # Queries the model actually issued, and the sources it cited. Recorded because a
    # grounded answer without its sources is not reproducible: the web moves, and
    # without the citations there is no way to tell later why an answer changed.
    search_queries: list[str] = field(default_factory=list)
    grounding_sources: list[str] = field(default_factory=list)
    finish_reason: FinishReason = FinishReason.UNKNOWN
    latency_ms: float | None = None
    # Vendor time summed across every attempt. Equals latency_ms for a single-attempt
    # request; larger when retries occurred.
    upstream_total_ms: float | None = None
    # Time deliberately spent sleeping between attempts.
    retry_backoff_ms: float = 0.0

    # Worst-case cost of attempts that failed after the vendor had already done
    # billable work. We cannot see usage metadata on a failed attempt, so this is an
    # upper bound, and it is deliberately an upper bound: a spend breaker that
    # under-counts is not a breaker. Negligible ungrounded, material when a grounded
    # request at ~88x the token cost is retried up to four times.
    unbilled_attempt_cost_usd: float = 0.0
    attempts: int = 1
    model: str | None = None
    provider: str | None = None
    cost_usd: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def visible_output_tokens(self) -> int:
        """Billed output tokens that produced user-visible text."""
        return max(0, self.output_tokens - self.thinking_tokens)

    @property
    def is_usable(self) -> bool:
        """True when there is text *and* generation completed normally."""
        return bool(self.answer) and self.finish_reason.is_usable

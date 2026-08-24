from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FinishReason(StrEnum):
    """Why generation stopped, normalized so callers never branch on vendor strings.

    Every vendor spells these differently. Normalizing here means a caller comparing
    two providers is comparing outcomes rather than string formats, and a provider
    that invents a new reason degrades to UNKNOWN instead of being mistaken for
    success.
    """

    STOP = "STOP"
    MAX_TOKENS = "MAX_TOKENS"
    SAFETY = "SAFETY"
    RECITATION = "RECITATION"
    BLOCKLIST = "BLOCKLIST"
    PROHIBITED_CONTENT = "PROHIBITED_CONTENT"
    SPII = "SPII"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"

    @property
    def is_usable(self) -> bool:
        return self is FinishReason.STOP


class LLM:
    @dataclass
    class SimpleResponse:
        answer: str
        input_tokens: int
        output_tokens: int

        # --- everything below is additive, and every field has a default, so the
        # three fields above can still be passed positionally by existing callers.

        # Billed at the output rate but never shown to the user. Counting only
        # visible tokens understates cost; counting them as visible output
        # overstates how much answer was produced.
        thinking_tokens: int = 0

        # Whether live web search actually ran for this answer.
        #
        # This reports what happened, not what was asked for. Requesting grounding
        # does not guarantee it occurs: the model may decline to search, or retrieval
        # may fail, and in both cases the request still returns a plausible answer
        # with no error. A caller that assumed its request was honoured would file an
        # ungrounded answer as a grounded measurement.
        grounded: bool = False
        grounding_requested: bool = False
        # What the model searched for, and the sources it cited. A grounded answer
        # without its citations cannot be reproduced: the web moves, so when an
        # answer changes there is otherwise no way to tell whether the model changed
        # or the world did.
        search_queries: list[str] = field(default_factory=list)
        grounding_sources: list[str] = field(default_factory=list)

        finish_reason: FinishReason = FinishReason.UNKNOWN

        latency_ms: float | None = None
        # Vendor time summed across every attempt. Equals latency_ms for a
        # single-attempt request; larger when retries occurred.
        upstream_total_ms: float | None = None
        # Time deliberately spent sleeping between attempts. Neither our processing
        # cost nor the vendor's response time, so folding it into either misattributes
        # it.
        retry_backoff_ms: float = 0.0
        attempts: int = 1

        model: str | None = None
        provider: str | None = None
        # None means "not computed", which is not the same as free. A provider that
        # cannot price itself must not report 0.0 into a spend ledger.
        cost_usd: float | None = None
        # Worst-case cost of attempts that failed after the vendor had already done
        # billable work. Failed attempts carry no usage metadata, so this is an upper
        # bound on purpose: a spend breaker that under-counts is not a breaker.
        unbilled_attempt_cost_usd: float = 0.0

        metadata: dict[str, Any] = field(default_factory=dict)

        @property
        def grounding_degraded(self) -> bool:
            """We asked for live search and did not get it."""
            return self.grounding_requested and not self.grounded

        @property
        def visible_output_tokens(self) -> int:
            """Billed output tokens that produced user-visible text."""
            return max(0, self.output_tokens - self.thinking_tokens)

        @property
        def is_usable(self) -> bool:
            """True when there is text *and* generation completed normally.

            A truncated answer is not a short answer. It reads as complete prose and
            is billed in full, so callers that only check for text will treat it as a
            good sample.
            """
            return bool(self.answer) and self.finish_reason.is_usable

    def supports_grounding(self) -> bool:
        """Whether this provider can answer with live web search.

        Defaults to False so a provider that cannot ground says so, rather than
        silently returning an ungrounded answer to a grounded request.
        """
        return False

    async def ask_generic_question(self, system_prompt: str, question: str, temperature: float, *, grounded: bool = False) -> SimpleResponse:
        raise NotImplementedError()

    def parallelism(self):
        raise NotImplementedError()

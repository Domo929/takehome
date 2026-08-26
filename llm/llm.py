from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FinishReason(StrEnum):
    """Why generation stopped, normalized so callers never branch on vendor strings.

    An unrecognised reason degrades to UNKNOWN rather than being mistaken for success.
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

        # Everything below is additive and defaulted, so the three fields above can
        # still be passed positionally by existing callers.

        # Included in output_tokens, since both bill at the output rate.
        thinking_tokens: int = 0

        # What happened, not what was asked for. A model can decline to search and
        # still return a plausible answer with no error, so a caller that assumed its
        # request was honoured would file an ungrounded answer as a grounded one.
        grounded: bool = False
        grounding_requested: bool = False
        # A grounded answer without its citations cannot be reproduced: the web moves.
        search_queries: list[str] = field(default_factory=list)
        grounding_sources: list[str] = field(default_factory=list)

        finish_reason: FinishReason = FinishReason.UNKNOWN

        # Wall time of the final attempt.
        latency_ms: float | None = None
        # Vendor time summed across every attempt.
        upstream_total_ms: float | None = None
        # Deliberate sleep between attempts: neither our cost nor the vendor's.
        retry_backoff_ms: float = 0.0
        attempts: int = 1

        model: str | None = None
        provider: str | None = None
        # None means "not computed", which is not the same as free.
        cost_usd: float | None = None
        # Upper bound on attempts that failed after the vendor may already have
        # billed. A spend breaker that under-counts is not a breaker.
        unbilled_attempt_cost_usd: float = 0.0

        metadata: dict[str, Any] = field(default_factory=dict)

        @property
        def grounding_degraded(self) -> bool:
            """We asked for live search and did not get it."""
            return self.grounding_requested and not self.grounded

        @property
        def visible_output_tokens(self) -> int:
            return max(0, self.output_tokens - self.thinking_tokens)

        @property
        def is_usable(self) -> bool:
            """Text *and* a clean finish.

            A truncated answer reads as complete prose and is billed in full, so
            checking for text alone treats it as a good sample.
            """
            return bool(self.answer) and self.finish_reason.is_usable

    def supports_grounding(self) -> bool:
        """False by default, so a provider that cannot ground says so rather than
        silently answering ungrounded."""
        return False

    async def ask_generic_question(self, system_prompt: str, question: str, temperature: float, *, grounded: bool = False) -> SimpleResponse:
        raise NotImplementedError()

    def parallelism(self):
        raise NotImplementedError()

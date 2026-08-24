"""Core LLM provider abstraction.

The ``ask_generic_question`` signature is deliberately unchanged so existing callers
keep working. What changed is the *response* model, because the original one cannot
represent things Gemini actually does. See ``SimpleResponse``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


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


class LLM:
    @dataclass
    class SimpleResponse:
        """A single completion.

        Three fields differ from the original definition, each for a concrete reason:

        ``answer`` is ``str | None``. Gemini returns HTTP 200 with no text at all when
        it stops for ``MAX_TOKENS`` (thinking consumed the whole budget) or when a
        safety filter fires. Typing this as ``str`` pushes a ``None`` into downstream
        code that believes it holds text.

        ``output_tokens`` counts *every billed output token, including thinking
        tokens*. Gemini reports thinking separately but bills it at the output rate, so
        a provider reporting only ``candidates_token_count`` under-reports spend. Use
        ``visible_output_tokens`` for just the text a user sees.

        ``finish_reason`` distinguishes "the model answered" from "the model stopped
        early and this is a fragment". Without it the two are indistinguishable.
        """

        answer: str | None
        input_tokens: int
        output_tokens: int

        thinking_tokens: int = 0
        finish_reason: FinishReason = FinishReason.UNKNOWN
        latency_ms: float | None = None
        attempts: int = 1
        model: str | None = None
        provider: str | None = None
        cost_usd: float | None = None
        metadata: dict[str, Any] = field(default_factory=dict)

        @property
        def visible_output_tokens(self) -> int:
            return max(0, self.output_tokens - self.thinking_tokens)

        @property
        def is_usable(self) -> bool:
            """True when there is text *and* generation completed normally.

            A truncated answer counts as unusable rather than partial: for
            brand-mention extraction a fragment silently skews counts, which is worse
            than an outright failure because it looks like success.
            """
            return bool(self.answer) and self.finish_reason.is_usable

    async def ask_generic_question(
        self, system_prompt: str, question: str, temperature: float
    ) -> SimpleResponse:
        raise NotImplementedError()

    def parallelism(self) -> int:
        """Concurrent in-flight requests this provider can sustain.

        Kept as an int for interface compatibility, but treat it as a measured
        operating point, not a vendor guarantee. Vertex governs Gemini through Dynamic
        Shared Quota, which publishes no fixed per-project ceiling, so the honest value
        is whatever load testing showed to be the knee of the throughput curve.
        ``harness/`` exists to produce that number.
        """
        raise NotImplementedError()
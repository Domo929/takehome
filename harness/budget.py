"""Cost governor.

Spending real money against someone else's cloud project deserves a hard stop, not a
comment in a README. Four mechanisms, in order of when they fire:

1. **Pre-flight estimate.** Before any request, project the cost from the planned
   request count and expected token usage. Print it, and refuse to proceed without an
   explicit ``--confirm``. Dry run is the default.
2. **Hard ceiling check.** Before each dispatch, compare *actual* accumulated spend
   against the budget. Over the line, the run stops and drains.
3. **Actual accounting.** Spend is derived from reported ``usage_metadata``, never
   estimated, so an unexpectedly chatty model trips the breaker on real dollars.
4. **Post-run reconciliation.** Compare the estimate against actual and report the
   error, because a consistently wrong estimator is itself a finding.

The breaker is intentionally conservative: it stops *before* dispatching the request
that would exceed the budget, so the ceiling is never crossed rather than merely
detected after the fact.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from llm.metrics import budget_remaining_usd
from llm.pricing import pricing_for


class BudgetExceeded(RuntimeError):
    """Raised when a dispatch would cross the configured ceiling."""


@dataclass
class CostEstimate:
    requests: int
    input_tokens_each: int
    output_tokens_each: int
    model: str

    @property
    def total_usd(self) -> float:
        pricing = pricing_for(self.model)
        return self.requests * pricing.cost(self.input_tokens_each, self.output_tokens_each)

    def render(self) -> str:
        pricing = pricing_for(self.model)
        return (
            f"  model               {self.model}\n"
            f"  requests            {self.requests:,}\n"
            f"  est. input tokens   {self.input_tokens_each:,} each "
            f"({self.requests * self.input_tokens_each:,} total)\n"
            f"  est. output tokens  {self.output_tokens_each:,} each "
            f"({self.requests * self.output_tokens_each:,} total)\n"
            f"  rates               ${pricing.input_per_1m:.2f}/1M in, "
            f"${pricing.output_per_1m:.2f}/1M out\n"
            f"  ESTIMATED COST      ${self.total_usd:.4f}\n"
        )


@dataclass
class CostGovernor:
    """Tracks spend and refuses dispatches that would exceed the budget."""

    budget_usd: float
    model: str = "gemini-2.5-flash"
    # Used only for the pre-dispatch reservation check, since actual cost is unknown
    # until the response arrives.
    expected_cost_per_request: float = 0.0

    spent_usd: float = 0.0
    requests_completed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    tripped: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def __post_init__(self) -> None:
        budget_remaining_usd.set(self.budget_usd)

    async def reserve(self) -> None:
        """Check before dispatching. Raises :class:`BudgetExceeded` when out of room.

        The check uses actual spend so far plus the expected cost of one more request,
        so the breaker trips before the ceiling is crossed rather than after.
        """
        async with self._lock:
            if self.tripped:
                raise BudgetExceeded(
                    f"budget ${self.budget_usd:.4f} exhausted "
                    f"(spent ${self.spent_usd:.4f} over {self.requests_completed} requests)"
                )
            projected = self.spent_usd + self.expected_cost_per_request
            if projected > self.budget_usd:
                self.tripped = True
                raise BudgetExceeded(
                    f"next request would reach ${projected:.4f}, over the "
                    f"${self.budget_usd:.4f} ceiling "
                    f"(spent ${self.spent_usd:.4f} over {self.requests_completed} requests)"
                )

    async def record(
        self,
        *,
        cost_usd: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        thinking_tokens: int = 0,
    ) -> None:
        """Record real spend from a response.

        Called for failed and empty responses too: Gemini bills for tokens even when
        it returns nothing usable, so ignoring those would under-count the invoice.
        """
        async with self._lock:
            self.spent_usd += cost_usd
            self.requests_completed += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.thinking_tokens += thinking_tokens
            budget_remaining_usd.set(max(0.0, self.budget_usd - self.spent_usd))
            if self.spent_usd >= self.budget_usd:
                self.tripped = True

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.budget_usd - self.spent_usd)

    def summary(self, estimate: CostEstimate | None = None) -> str:
        lines = [
            f"  requests completed  {self.requests_completed:,}",
            f"  input tokens        {self.input_tokens:,}",
            f"  output tokens       {self.output_tokens:,} "
            f"(of which thinking: {self.thinking_tokens:,})",
            f"  ACTUAL COST         ${self.spent_usd:.4f}",
            f"  budget remaining    ${self.remaining_usd:.4f}",
        ]
        if estimate is not None and estimate.total_usd > 0:
            error = (self.spent_usd - estimate.total_usd) / estimate.total_usd * 100.0
            lines.append(
                f"  estimate error      {error:+.1f}% "
                f"(estimated ${estimate.total_usd:.4f})"
            )
        if self.tripped:
            lines.append("  STATUS              budget breaker TRIPPED")
        return "\n".join(lines)


def confirm_or_exit(estimate: CostEstimate, *, confirmed: bool, budget_usd: float) -> None:
    """Gate on an explicit confirmation flag. Dry run is the default."""
    print("\nCost pre-flight")
    print(estimate.render())
    print(f"  hard budget ceiling ${budget_usd:.4f}")

    if estimate.total_usd > budget_usd:
        raise SystemExit(
            f"\nRefusing to start: estimate ${estimate.total_usd:.4f} already exceeds "
            f"the ${budget_usd:.4f} ceiling. Lower the request count or raise --budget-usd."
        )

    if not confirmed:
        raise SystemExit(
            "\nDry run only. No requests were sent and nothing was spent.\n"
            "Re-run with --confirm to actually spend money."
        )

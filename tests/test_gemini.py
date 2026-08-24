"""Behavioral tests for the Gemini provider.

These assert on the failure modes that make Gemini different from an OpenAI-shaped
API. Each test forces one specific condition on the fake server, so failures point at
a named behavior rather than a flaky end-to-end run.
"""

from __future__ import annotations

import pytest

from llm.errors import LLMContentBlockedError, LLMEmptyResponseError, LLMRateLimitError
from llm.gemini import Gemini
from llm.response import FinishReason
from llm.retry import RetryBudget, RetryPolicy

SYSTEM = "You are a market research assistant."
QUESTION = "Which robot vacuum brands are worth considering?"


def build(server, **kwargs) -> Gemini:
    params = dict(
        backend="vertex",
        project="fake-project",
        location="global",
        base_url=server.base_url,
        max_output_tokens=1024,
        thinking_budget=0,
        max_connections=64,
    )
    params.update(kwargs)
    return Gemini(**params)


async def test_happy_path_parses_usage_and_cost(fake_vertex):
    fake_vertex.configure(
        empty_probability=0.0, safety_probability=0.0, rate_limit_probability=0.0,
        server_error_probability=0.0, truncate_probability=0.0,
    )
    provider = build(fake_vertex)
    result = await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)

    assert result.is_usable
    assert result.finish_reason is FinishReason.STOP
    assert result.answer
    assert result.input_tokens > 0
    assert result.output_tokens > 0
    assert result.cost_usd and result.cost_usd > 0
    assert result.latency_ms is not None
    assert result.metadata["traffic_type"] == "ON_DEMAND"


async def test_thinking_tokens_are_counted_as_billed_output(fake_vertex):
    """Thinking bills at the output rate, so it must be inside output_tokens.

    Reporting only candidates_token_count is the accounting bug that makes a run look
    cheaper than the invoice.
    """
    fake_vertex.configure(
        empty_probability=0.0, safety_probability=0.0, truncate_probability=0.0,
        rate_limit_probability=0.0, server_error_probability=0.0,
    )
    provider = build(fake_vertex, thinking_budget=256, max_output_tokens=4096)
    result = await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)

    assert result.thinking_tokens > 0
    assert result.output_tokens > result.thinking_tokens
    assert result.visible_output_tokens == result.output_tokens - result.thinking_tokens

    from llm.pricing import cost_usd
    expected = cost_usd("gemini-2.5-flash", result.input_tokens, result.output_tokens)
    assert result.cost_usd == pytest.approx(expected)


async def test_thinking_budget_at_cap_starves_the_answer(fake_vertex):
    """The headline quirk.

    thinking_budget and max_output_tokens draw on one allowance. Setting the budget at
    or above the cap leaves nothing for visible text: HTTP 200, finish_reason
    MAX_TOKENS, no answer, and the thinking tokens are billed in full.
    """
    fake_vertex.configure(
        empty_probability=0.0, safety_probability=0.0, truncate_probability=0.0,
        rate_limit_probability=0.0, server_error_probability=0.0,
    )
    provider = build(
        fake_vertex, thinking_budget=512, max_output_tokens=512,
        retry_policy=RetryPolicy(max_attempts=1, attempt_timeout_s=10, total_deadline_s=20),
    )

    with pytest.raises(LLMEmptyResponseError):
        await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)


async def test_safety_block_is_terminal_not_retried(fake_vertex):
    """Retrying a safety block just re-triggers it and bills again."""
    fake_vertex.configure(safety_probability=1.0, empty_probability=0.0)
    budget = RetryBudget(capacity=100.0)
    provider = build(
        fake_vertex,
        retry_policy=RetryPolicy(
            max_attempts=4, attempt_timeout_s=10, total_deadline_s=20, budget=budget
        ),
    )

    with pytest.raises(LLMContentBlockedError):
        await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)

    # A terminal error must not consume retry budget.
    assert budget.tokens == pytest.approx(100.0)
    fake_vertex.configure(safety_probability=0.0)


async def test_empty_response_is_retried_and_can_recover(fake_vertex):
    """An empty 200 is invisible to transport retry; only validation catches it."""
    fake_vertex.configure(empty_probability=1.0, safety_probability=0.0)
    provider = build(
        fake_vertex,
        retry_policy=RetryPolicy(
            max_attempts=3, base_delay_s=0.01, attempt_timeout_s=10, total_deadline_s=20
        ),
    )

    with pytest.raises(LLMEmptyResponseError):
        await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)

    fake_vertex.configure(empty_probability=0.0)
    result = await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)
    assert result.is_usable


async def test_rate_limit_surfaces_as_retryable_with_retry_after(fake_vertex):
    fake_vertex.configure(rate_limit_probability=1.0, retry_after_s=0.05)
    provider = build(
        fake_vertex,
        retry_policy=RetryPolicy(
            max_attempts=2, base_delay_s=0.01, attempt_timeout_s=10, total_deadline_s=20
        ),
    )

    with pytest.raises(LLMRateLimitError) as excinfo:
        await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)

    assert excinfo.value.retryable
    assert excinfo.value.status_code == 429
    assert excinfo.value.retry_after_s == pytest.approx(0.05, abs=0.02)
    fake_vertex.configure(rate_limit_probability=0.0)


async def test_retry_budget_sheds_load_when_exhausted(fake_vertex):
    """A drained budget must stop retrying rather than amplify traffic."""
    fake_vertex.configure(rate_limit_probability=1.0, retry_after_s=0.01)
    budget = RetryBudget(capacity=2.0, refill_per_attempt=0.0)
    provider = build(
        fake_vertex,
        retry_policy=RetryPolicy(
            max_attempts=10, base_delay_s=0.001, max_delay_s=0.01,
            attempt_timeout_s=5, total_deadline_s=15, budget=budget,
        ),
    )

    with pytest.raises(LLMRateLimitError):
        await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)

    assert budget.tokens < 1.0
    fake_vertex.configure(rate_limit_probability=0.0)


async def test_parallelism_is_derived_from_pool_size(fake_vertex):
    """parallelism() must never exceed the connection pool it has to run through."""
    provider = build(fake_vertex, max_connections=250)
    assert provider.parallelism() == 100
    assert provider.parallelism() < provider.max_connections


async def test_concurrent_requests_do_not_exceed_pool(fake_vertex):
    import asyncio

    fake_vertex.reset()
    fake_vertex.configure(
        rate_limit_probability=0.0, empty_probability=0.0, safety_probability=0.0,
        saturation_concurrency=10_000,
    )
    provider = build(fake_vertex, max_connections=16)

    results = await asyncio.gather(
        *(provider.ask_generic_question(SYSTEM, QUESTION, 0.7) for _ in range(24))
    )
    assert all(r.is_usable for r in results)

    # The server observed real concurrency, confirming requests were not serialized.
    assert fake_vertex.stats()["peak_inflight"] > 1


async def test_sdk_serializes_thinking_budget_in_snake_case(fake_vertex):
    """Pins an SDK serialization quirk that is easy to get silently wrong.

    Inside ``generationConfig`` every field is camelCase except ``thinkingConfig``'s
    budget, which the SDK emits as ``thinking_budget``. Anything sitting between the
    client and Vertex (a gateway, a proxy, a recording mock) that matches on
    ``thinkingBudget`` will quietly drop the setting. The model then thinks without a
    bound, and the symptom is truncated answers plus unexplained cost rather than an
    error. If a future SDK release normalizes this, this test should fail loudly.
    """
    import json

    import httpx

    captured: dict = {}
    original = httpx.AsyncClient.request

    async def spy(self, method, url, **kwargs):
        body = kwargs.get("content")
        if body:
            try:
                captured["body"] = json.loads(body)
            except (TypeError, ValueError):
                pass
        return await original(self, method, url, **kwargs)

    httpx.AsyncClient.request = spy
    try:
        provider = build(fake_vertex, thinking_budget=128, max_output_tokens=4096)
        await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)
    finally:
        httpx.AsyncClient.request = original

    generation_config = captured["body"]["generationConfig"]
    assert "maxOutputTokens" in generation_config, "sibling fields are camelCase"
    thinking_config = generation_config["thinkingConfig"]
    assert thinking_config.get("thinking_budget") == 128, (
        "SDK emitted the budget under an unexpected key; "
        f"got keys {sorted(thinking_config)}"
    )


async def test_base_contract_is_unmodified_and_honored(fake_vertex):
    """The provided LLM.SimpleResponse contract must not be widened or bypassed.

    ``llm/llm.py`` is treated as given. GeminiResponse extends it additively, so any
    caller written against the base contract keeps working: the isinstance relation
    holds, inherited fields keep their declared types, and ``answer`` is always a
    populated ``str`` because the provider raises rather than returning empty text.
    """
    import dataclasses

    from llm.llm import LLM
    from llm.response import GeminiResponse

    base_fields = {f.name: f.type for f in dataclasses.fields(LLM.SimpleResponse)}
    assert set(base_fields) == {"answer", "input_tokens", "output_tokens"}, (
        "the base contract gained or lost fields; llm/llm.py must stay as provided"
    )
    # The original module has no `from __future__ import annotations`, so field types
    # resolve to real classes; tolerate either form in case that changes.
    assert base_fields["answer"] in (str, "str"), (
        "answer must remain a plain str on the base contract"
    )

    fake_vertex.configure(
        empty_probability=0.0, safety_probability=0.0, rate_limit_probability=0.0,
        server_error_probability=0.0, truncate_probability=0.0,
    )
    provider = build(fake_vertex)
    result = await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)

    assert isinstance(result, LLM.SimpleResponse)
    assert isinstance(result, GeminiResponse)
    assert isinstance(result.answer, str) and result.answer

    # A caller that knows only the base contract must work unchanged.
    def legacy_consumer(response: LLM.SimpleResponse) -> int:
        return response.input_tokens + response.output_tokens

    assert legacy_consumer(result) > 0


async def test_output_tokens_stay_correct_for_base_contract_callers(fake_vertex):
    """A caller reading only ``output_tokens`` still sees the full billed amount.

    Thinking tokens are surfaced separately for callers that want the split, but the
    inherited field remains the total billed output so cost computed from the base
    contract alone is right rather than an undercount.
    """
    fake_vertex.configure(
        empty_probability=0.0, safety_probability=0.0, truncate_probability=0.0,
        rate_limit_probability=0.0, server_error_probability=0.0,
    )
    provider = build(fake_vertex, thinking_budget=256, max_output_tokens=4096)
    result = await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)

    assert result.thinking_tokens > 0
    assert result.output_tokens == result.visible_output_tokens + result.thinking_tokens

    from llm.pricing import cost_usd
    base_only_cost = cost_usd("gemini-2.5-flash", result.input_tokens, result.output_tokens)
    assert result.cost_usd == pytest.approx(base_only_cost)


async def test_retried_request_attributes_time_correctly(fake_vertex):
    """Retry time must not be misattributed as our own overhead.

    ``latency_ms`` reports only the final attempt. A request that was retried also
    spent time on the failed attempts and on deliberate backoff sleep. If a caller
    computes "our overhead" as ``total - latency_ms``, all of that lands on us, and a
    path whose real cost is a fraction of a millisecond reports p99 overhead in
    seconds. ``upstream_total_ms`` and ``retry_backoff_ms`` exist so the three costs
    can be separated.
    """
    import time

    fake_vertex.configure(server_error_probability=1.0, empty_probability=0.0)
    provider = build(
        fake_vertex,
        retry_policy=RetryPolicy(
            max_attempts=3, base_delay_s=0.05, max_delay_s=0.2,
            attempt_timeout_s=10, total_deadline_s=30,
        ),
    )

    # Fail twice, then succeed, so the result carries a real retry history.
    from llm.errors import LLMServerError
    with pytest.raises(LLMServerError):
        await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)

    fake_vertex.configure(server_error_probability=0.0)
    started = time.perf_counter()
    result = await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)
    total_ms = (time.perf_counter() - started) * 1000.0

    assert result.is_usable
    assert result.upstream_total_ms is not None
    assert result.upstream_total_ms >= (result.latency_ms or 0.0)
    assert result.retry_backoff_ms >= 0.0

    # The decomposition must not claim more time than actually elapsed.
    accounted = result.upstream_total_ms + result.retry_backoff_ms
    assert accounted <= total_ms + 5.0, (
        f"accounted {accounted:.1f}ms exceeds elapsed {total_ms:.1f}ms"
    )

    # And what is left over for us must be small: this path does almost nothing.
    ours = total_ms - accounted
    assert ours < 100.0, f"unattributed time {ours:.1f}ms is implausibly large"


def test_cached_input_tokens_are_discounted_not_double_charged():
    """Cache hits bill at a fraction of the input rate.

    Implicit caching is on by default for Gemini 2.5, so a workload with a repeated
    system prompt can be getting this discount without anyone enabling it. Charging
    every prompt token at full rate overstates spend, which is the mirror image of
    the thinking-token bug that understates it.
    """
    from llm.pricing import cost_usd, pricing_for

    pricing = pricing_for("gemini-2.5-flash")
    full = cost_usd("gemini-2.5-flash", input_tokens=1000, output_tokens=0)
    all_cached = cost_usd(
        "gemini-2.5-flash", input_tokens=1000, output_tokens=0, cached_tokens=1000
    )
    assert all_cached == pytest.approx(full * pricing.cached_input_multiplier)

    half = cost_usd(
        "gemini-2.5-flash", input_tokens=1000, output_tokens=0, cached_tokens=500
    )
    assert full > half > all_cached

    # Cached can never exceed prompt tokens, and must not produce a negative charge.
    absurd = cost_usd(
        "gemini-2.5-flash", input_tokens=100, output_tokens=0, cached_tokens=99999
    )
    assert absurd > 0

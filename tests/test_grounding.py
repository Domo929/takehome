"""Grounding is a different axis from thinking, and the code must not conflate them.

Thinking reasons harder over training data. Grounding injects live web results. They
have different API surfaces, different billing, and answer different product
questions — "what does the model believe about this brand" versus "what can it find
about this brand today". A brand-tracking system that ran one and reported the other
would be measuring the wrong thing.
"""

from __future__ import annotations

import pytest

from llm.gemini import Gemini
from llm.pricing import GROUNDING_USD_PER_1K_PROMPTS, cost_usd

SYSTEM = "You are a market research assistant."
QUESTION = "Which robot vacuum brands are worth considering?"


def build(server, **kw) -> Gemini:
    params = dict(
        backend="vertex", project="fake-project", location="us-central1",
        base_url=server.base_url, max_output_tokens=512, thinking_budget=0,
        max_connections=64,
    )
    params.update(kw)
    return Gemini(**params)


async def test_grounding_is_off_unless_requested(fake_vertex):
    """Grounding costs ~88x per request, so it must never be a silent default."""
    provider = build(fake_vertex)
    assert provider.describe()["grounded"] is False

    result = await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)
    assert result.grounded is False
    assert result.search_queries == []
    assert result.grounding_sources == []


async def test_grounded_request_carries_its_evidence(fake_vertex):
    """A grounded answer without its sources is not reproducible.

    The web moves. If a brand's measured share shifts next week, the queries and
    citations are the only way to tell whether the model changed or the world did.
    """
    provider = build(fake_vertex, grounded=True)
    result = await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)

    assert result.grounded is True
    assert result.search_queries, "grounded response carried no search queries"
    assert result.grounding_sources, "grounded response carried no sources"
    assert all(s.startswith("http") for s in result.grounding_sources)


async def test_grounded_request_is_billed_for_the_search_sku(fake_vertex):
    """Token cost alone understates a grounded workload by roughly 88x.

    Grounding bills per prompt on a separate SKU. A cost model that counts only
    tokens would make a grounded sweep look affordable when it is the dominant line
    item.
    """
    grounded = build(fake_vertex, grounded=True)
    plain = build(fake_vertex)

    g = await grounded.ask_generic_question(SYSTEM, QUESTION, 0.7)
    p = await plain.ask_generic_question(SYSTEM, QUESTION, 0.7)

    assert g.cost_usd > p.cost_usd
    # The difference should be dominated by the flat per-prompt SKU fee.
    sku = GROUNDING_USD_PER_1K_PROMPTS / 1000.0
    assert g.cost_usd >= sku
    assert (g.cost_usd - p.cost_usd) == pytest.approx(sku, rel=0.5)


def test_pricing_separates_the_grounding_sku_from_tokens():
    tokens_only = cost_usd("gemini-2.5-flash", 35, 111)
    with_search = cost_usd("gemini-2.5-flash", 35, 111, grounded=True)
    assert with_search - tokens_only == pytest.approx(GROUNDING_USD_PER_1K_PROMPTS / 1000.0)
    # At these rates the SKU dwarfs the tokens, which is the whole point.
    assert with_search / tokens_only > 50


async def test_grounding_and_thinking_are_independent(fake_vertex):
    """Two separate axes, so all four combinations must be expressible."""
    for grounded in (False, True):
        for budget in (0, -1):
            provider = build(fake_vertex, grounded=grounded, thinking_budget=budget)
            d = provider.describe()
            assert d["grounded"] is grounded
            assert d["thinking_budget"] == budget


async def test_grounding_can_fail_silently_and_we_notice(fake_vertex):
    """The dangerous grounding failure is the one that returns HTTP 200.

    If retrieval fails or the model declines to search, the request still succeeds and
    still returns a plausible answer. Nothing raises. For a product whose entire signal
    is the difference between the grounded and ungrounded conditions, quietly filing an
    ungrounded answer as a grounded one corrupts the measurement in the direction that
    is hardest to detect later.

    So `grounded` must report what came back, not what was asked for.
    """
    fake_vertex.configure(grounding_failure_rate=1.0)
    try:
        provider = build(fake_vertex, grounded=True)
        result = await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)

        assert result.answer, "request should still succeed - that is the problem"
        assert result.grounding_requested is True
        assert result.grounded is False, "grounded must reflect the response"
        assert result.grounding_degraded is True
        assert result.grounding_sources == []
    finally:
        fake_vertex.configure(grounding_failure_rate=0.0)


async def test_degraded_grounding_is_not_billed_for_search(fake_vertex):
    """No search ran, so no search fee. Billing the request would inflate the ledger."""
    fake_vertex.configure(grounding_failure_rate=1.0)
    try:
        provider = build(fake_vertex, grounded=True)
        result = await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)
        assert result.cost_usd < GROUNDING_USD_PER_1K_PROMPTS / 1000.0
    finally:
        fake_vertex.configure(grounding_failure_rate=0.0)


async def test_failed_grounded_attempts_are_counted_against_the_budget(fake_vertex):
    """A grounded attempt that fails may still have run - and been billed for - a search.

    Failed attempts carry no usage metadata, so the cost of a retried grounded request
    is invisible if only the successful attempt is counted. Ungrounded that is a
    rounding error. Grounded, at ~88x the token cost and up to four attempts, it is the
    difference between a spend breaker that works and one that reports a number it
    would like to be true.
    """
    fake_vertex.configure(server_error_probability=1.0)
    try:
        provider = build(fake_vertex, grounded=True)
        with pytest.raises(Exception):
            await provider.ask_generic_question(SYSTEM, QUESTION, 0.7)
    finally:
        fake_vertex.configure(server_error_probability=0.0)

    from llm.metrics import REGISTRY

    charged = REGISTRY.get_sample_value(
        "llm_unbilled_attempt_cost_usd_total",
        {"provider": "gemini", "model": "gemini-2.5-flash"},
    )
    assert charged is not None and charged > 0, (
        "failed grounded attempts must reach the spend accounting"
    )

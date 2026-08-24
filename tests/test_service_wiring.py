"""The service must inherit its admission capacity from the provider.

This seam is easy to get wrong and invisible when it is: the load harness takes
concurrency as a CLI flag and never calls ``parallelism()``, so a soak can validate an
operating point while the service quietly runs at a different one. These tests assert
the number the soak validated is the number the service actually admits.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from llm.gemini import Gemini
from service import app as service_app


@pytest.fixture
def _dev_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_BACKEND", "developer")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-not-used")
    monkeypatch.delenv("SERVICE_CAPACITY", raising=False)
    monkeypatch.delenv("GEMINI_PARALLELISM", raising=False)
    monkeypatch.delenv("GEMINI_ADAPTIVE", raising=False)


async def test_service_capacity_matches_provider_parallelism(_dev_env):
    """No silent divergence between what was load tested and what is served."""
    async with service_app.lifespan(service_app.app):
        assert service_app.state.provider is not None
        assert service_app.state.capacity == service_app.state.provider.parallelism()
        # 128 is the top of the measured range; see FINDINGS 6g.
        assert service_app.state.capacity == 128


async def test_service_capacity_never_exceeds_the_connection_pool(_dev_env, monkeypatch):
    """A gate wider than the pool queues on sockets instead of at the gate.

    That is the invisible queueing the pool-saturation metric exists to catch, so the
    admission limit must stay at or below the pool at every pool size.
    """
    for conns in (40, 100, 160, 256):
        monkeypatch.setenv("GEMINI_MAX_CONNECTIONS", str(conns))
        provider = Gemini()
        assert provider.parallelism() <= provider.max_connections, (
            f"pool={conns} gave parallelism={provider.parallelism()}"
        )
        # And never above the value we actually validated.
        assert provider.parallelism() <= 128


async def test_explicit_capacity_override_wins(_dev_env, monkeypatch):
    """Operators must be able to override without editing code."""
    monkeypatch.setenv("SERVICE_CAPACITY", "7")
    async with service_app.lifespan(service_app.app):
        assert service_app.state.capacity == 7


async def test_adaptive_limiter_is_off_by_default(_dev_env):
    """Its justification — that quota moves — is not evidenced (FINDINGS 6b).

    Shipping it enabled would mean running unproven machinery in the request path.
    """
    async with service_app.lifespan(service_app.app):
        assert service_app.state.provider.limiter is None
        assert service_app.state.gate is not None, "fixed semaphore expected when adaptive is off"

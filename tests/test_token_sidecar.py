"""Tests for the k6 token sidecar's cache.

The sidecar exists so N k6 VUs produce one upstream token refresh per VU rather than
one per request. That property is load-bearing: at 550 requests per second a broken
cache turns a Vertex capacity test into an unintentional load test of Google's OAuth
endpoint, and the resulting throughput number would be measuring the wrong service
entirely.

The bug these tests pin was exactly that, and it was invisible. Expiry was computed by
calling ``.timestamp()`` on google-auth's naive UTC datetime, which Python interprets
as local time. In a UTC-7 zone the computed expiry landed seven hours in the past, so
every single call decided a refresh was due. Nothing errored. Tokens worked. The only
symptom was a refresh count nobody was checking.
"""

from __future__ import annotations

import datetime
import importlib.util
import pathlib
import sys

import pytest

_SIDECAR = (
    pathlib.Path(__file__).resolve().parent.parent
    / "loadtest" / "k6" / "token-refresher" / "sidecar.py"
)


def _load_sidecar():
    spec = importlib.util.spec_from_file_location("k6_token_sidecar", _SIDECAR)
    module = importlib.util.module_from_spec(spec)
    sys.modules["k6_token_sidecar"] = module
    spec.loader.exec_module(module)
    return module


sidecar = _load_sidecar()


class FakeCredentials:
    """Stands in for google.auth credentials, mimicking the naive-UTC expiry."""

    def __init__(self, lifetime_s: float = 3600.0) -> None:
        self.lifetime_s = lifetime_s
        self.refresh_calls = 0
        self.token = "initial-token"
        self._set_expiry()

    def _set_expiry(self) -> None:
        # google-auth stores a NAIVE datetime that is really UTC. Reproducing that
        # exactly is the whole point: a timezone-aware stub would hide the bug.
        self.expiry = datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        ) + datetime.timedelta(seconds=self.lifetime_s)

    def refresh(self, _request) -> None:
        self.refresh_calls += 1
        self.token = f"token-{self.refresh_calls}"
        self._set_expiry()


@pytest.fixture
def cache(monkeypatch):
    c = sidecar.TokenCache()
    creds = FakeCredentials()
    c._credentials = creds
    monkeypatch.setattr(
        sidecar.google.auth.transport.requests, "Request", lambda: object()
    )
    return c, creds


def test_a_fresh_token_is_served_from_cache_not_refetched(cache):
    """The property the whole sidecar exists for.

    700 VUs asking for a token must not produce 700 refreshes. If this fails, a load
    test is measuring OAuth rather than Vertex.
    """
    c, creds = cache
    creds.refresh_calls = 0

    for _ in range(700):
        token, expires_in = c.get()

    assert creds.refresh_calls == 0, (
        f"cache refetched {creds.refresh_calls} times for 700 reads; "
        "at 550 rps this would hammer Google's token endpoint"
    )
    assert token
    assert c.served_count == 700


def test_expiry_is_read_as_utc_not_local_time(cache):
    """The specific bug, pinned.

    google-auth's expiry is naive but means UTC. Treating it as local time shifts it
    by the UTC offset, and anywhere west of Greenwich that puts it in the past, which
    makes the cache refresh on every call. Asserting on the remaining lifetime catches
    it in any timezone, whereas asserting "did it refresh" would pass in UTC.
    """
    c, creds = cache
    _, expires_in = c.get()

    assert expires_in > 3000, (
        f"token reported {expires_in:.0f}s of life remaining, expected ~3600. "
        "A near-zero value means expiry was interpreted in local time."
    )
    assert expires_in <= creds.lifetime_s


def test_a_token_near_expiry_is_refreshed_early(cache):
    """The cache must not serve a token that expires mid-flight.

    A request carrying a token with two seconds left fails somewhere over the network
    rather than at the client, which reads as a vendor error.
    """
    c, creds = cache
    creds.lifetime_s = sidecar.REFRESH_MARGIN_S / 2
    creds._set_expiry()
    creds.refresh_calls = 0

    c.get()

    assert creds.refresh_calls == 1


def test_refresh_count_is_observable(cache):
    """The count is the only symptom of a broken cache, so it has to be exposed.

    This bug shipped precisely because nothing surfaced the number. vertex.js now
    derives it as http_reqs minus iterations and reports it in every summary.
    """
    c, creds = cache
    creds.lifetime_s = 0.0
    creds._set_expiry()
    c.get()
    c.get()

    assert c.refresh_count >= 2
    assert c.served_count == 2

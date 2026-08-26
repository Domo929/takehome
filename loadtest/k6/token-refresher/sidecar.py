"""ADC token sidecar for the k6 control harness.

k6 has no Google credential chain, so it needs a bearer token from somewhere. This
mints one via Application Default Credentials and serves it over localhost.

There is a second reason to make this a separate, observable component. ADC access
tokens live about an hour, and a fleet of clients that all refresh on the same
schedule produces a synchronized stall against the token endpoint, a real production
thundering-herd risk that stays invisible while the refresh is buried inside an SDK.
Exposing it here makes the refresh boundary something a soak test can actually cross
and measure.

Refresh is proactive (a minute before expiry) and serialized behind a lock, so N
concurrent k6 VUs asking at once produce one upstream refresh rather than N.
"""

from __future__ import annotations

import argparse
import threading
import time

import google.auth
import google.auth.transport.requests
from fastapi import FastAPI
from fastapi.responses import JSONResponse

SCOPE = "https://www.googleapis.com/auth/cloud-platform"
REFRESH_MARGIN_S = 60.0


class TokenCache:
    def __init__(self) -> None:
        self._credentials = None
        self._lock = threading.Lock()
        self.refresh_count = 0
        self.served_count = 0

    def _expiry_epoch(self) -> float:
        expiry = getattr(self._credentials, "expiry", None)
        if expiry is None:
            return time.time() + 3600.0
        # google-auth stores expiry as a naive UTC datetime.
        return expiry.replace(tzinfo=None).timestamp() - time.timezone

    def get(self) -> tuple[str, float]:
        with self._lock:
            now = time.time()
            needs_refresh = (
                self._credentials is None
                or not getattr(self._credentials, "token", None)
                or self._expiry_epoch() - now <= REFRESH_MARGIN_S
            )
            if needs_refresh:
                if self._credentials is None:
                    self._credentials, _ = google.auth.default(scopes=[SCOPE])
                self._credentials.refresh(google.auth.transport.requests.Request())
                self.refresh_count += 1
            self.served_count += 1
            return self._credentials.token, max(0.0, self._expiry_epoch() - now)


def create_app() -> FastAPI:
    cache = TokenCache()
    app = FastAPI(title="adc-token-sidecar")

    @app.get("/token")
    async def token() -> JSONResponse:
        try:
            value, expires_in = cache.get()
        except Exception as exc:
            return JSONResponse(
                {"error": f"could not obtain ADC token: {exc}"}, status_code=500
            )
        return JSONResponse({"token": value, "expires_in_s": round(expires_in, 1)})

    @app.get("/__stats")
    async def stats() -> dict:
        """Refreshes vs served shows the cache is actually coalescing requests."""
        return {"refreshes": cache.refresh_count, "served": cache.served_count}

    return app


app = create_app()


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Serve ADC access tokens to k6.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

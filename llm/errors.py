"""Provider-neutral error taxonomy.

Callers should never import a vendor SDK to decide whether something is retryable.
Each provider translates its own failures into these types; the retry engine and the
load harness both branch only on this hierarchy.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base for every provider failure."""

    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retry_after_s = retry_after_s
        # Money already spent on the attempts that led here. A failed request is not
        # a free request: an empty-but-billed 200 costs full tokens, and a grounded
        # attempt costs $0.035 whether or not it returns anything. Without this the
        # spend breaker is blind in exactly the failure modes where cost runs away
        # fastest, and it disagrees with the Prometheus ledger, which does count them.
        self.cost_usd: float = 0.0

    @property
    def error_class(self) -> str:
        """Short stable label used as a Prometheus metric label."""
        return type(self).__name__


class LLMRateLimitError(LLMError):
    """429 / RESOURCE_EXHAUSTED. May carry a server-supplied Retry-After."""

    retryable = True


class LLMServerError(LLMError):
    """5xx and other transient vendor faults."""

    retryable = True


class LLMTimeoutError(LLMError):
    """Request exceeded our own deadline.

    Distinct from a vendor 504 because the cause is usually ours (pool starvation,
    event-loop lag) rather than theirs.
    """

    retryable = True


class LLMEmptyResponseError(LLMError):
    """HTTP 200 with no candidate text.

    Worth its own type because it is invisible to transport-level retry: the request
    succeeded as far as HTTP is concerned, so only response validation can catch it.
    """

    retryable = True


class LLMContentBlockedError(LLMError):
    """Safety filter, recitation, or blocklist. Terminal, because retrying re-triggers it."""

    retryable = False


class LLMInvalidRequestError(LLMError):
    """Malformed 4xx. Terminal."""

    retryable = False


class LLMAuthenticationError(LLMError):
    """Credential or permission failure. Terminal, and usually fatal for the whole run."""

    retryable = False


class LLMInternalError(LLMError):
    """An exception we did not recognise. Almost certainly a bug on our side.

    Terminal on purpose. The tempting default is to treat anything unknown as a
    transient vendor fault, but a TypeError or a response shape the SDK changed
    underneath us does not get better on the fourth attempt. It just costs four times
    as much and then lands in the metrics as vendor unreliability, which is where a
    client-side defect goes to hide.
    """

    retryable = False

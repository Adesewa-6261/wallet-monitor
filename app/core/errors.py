"""
lib/errors.py

Every failure in this API has one of two shapes:

    Whole request failed   → HTTP error status + {"error": {...}}
    One wallet failed      → HTTP 200, that wallet carries {"error": {...}}

The second is the important one. A portfolio with four wallets where one
provider is down should still return the other three and a usable total.
Failing the whole request because one node was unreachable would blank the
user's screen for no good reason.
"""

import logging
from typing import Literal, Optional, TypedDict

logger = logging.getLogger("walletnest")

ErrorCode = Literal[
    # Request-level
    "INVALID_REQUEST",
    "RATE_LIMITED",
    "UPSTREAM_UNAVAILABLE",
    "INTERNAL",
    # Wallet-level
    "INVALID_CHECKSUM",
    "UNSUPPORTED_INPUT",
    "REJECTED_SECRET",
    "SCAN_INCOMPLETE",
    "PRICE_UNAVAILABLE",
]

STATUS: dict[str, int] = {
    "INVALID_REQUEST": 400,
    "RATE_LIMITED": 429,
    "UPSTREAM_UNAVAILABLE": 503,
    "INTERNAL": 500,
    "INVALID_CHECKSUM": 400,
    "UNSUPPORTED_INPUT": 400,
    "REJECTED_SECRET": 400,
    "SCAN_INCOMPLETE": 200,
    "PRICE_UNAVAILABLE": 200,
}


class ApiError(TypedDict, total=False):
    code: str
    message: str
    retryable: bool
    retryAfter: int


class RequestError(Exception):
    """
    Raised when the whole request cannot be fulfilled.

    Wallet-level problems should NOT use this — attach a wallet_error() to the
    wallet instead and keep going.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after

    @property
    def status(self) -> int:
        return STATUS.get(self.code, 500)

    def to_dict(self) -> dict:
        error: ApiError = {"code": self.code, "message": self.message}
        if self.retry_after is not None:
            error["retryAfter"] = self.retry_after
        return {"error": error}


def wallet_error(code: ErrorCode, message: str, retryable: bool = True) -> ApiError:
    """Build a wallet-level error object. Never raises."""
    return {"code": code, "message": message, "retryable": retryable}


def to_error_response(err: Exception) -> tuple[int, dict]:
    """
    Turn any exception into a safe JSON response.

    Deliberately does not leak internal messages for unexpected errors — an
    upstream provider's error text can contain our API key from the request
    URL. RequestError messages are ones we wrote, so those are safe.
    """
    if isinstance(err, RequestError):
        return err.status, err.to_dict()

    logger.exception("Unhandled error")

    return 500, {
        "error": {
            "code": "INTERNAL",
            "message": "Something went wrong on our side. Please try again.",
        }
    }

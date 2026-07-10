"""LLM call error hierarchy, classification, and retry-with-backoff utilities."""
from __future__ import annotations
import random
import time

from openai import (
    APIStatusError,
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    AuthenticationError,
    PermissionDeniedError,
)


class PipelineCallError(Exception):
    """Base for pipeline LLM call failures."""

    def __init__(self, error_type: str, message: str, cause: Exception = None):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.cause = cause


class RetryableError(PipelineCallError):
    """Transient error; caller should retry with backoff."""

    def __init__(self, message: str, cause: Exception = None, retry_after: float = None):
        super().__init__("retryable", message, cause)
        self.retry_after = retry_after  # seconds, or None for exponential backoff


class NonRetryableError(PipelineCallError):
    """Permanent error; caller must not retry."""

    def __init__(self, error_type: str, message: str = None, cause: Exception = None):
        if message is None:
            super().__init__("non_retryable", error_type, cause)
        else:
            super().__init__(error_type, message, cause)



class AuthError(NonRetryableError):
    def __init__(self, message: str, cause: Exception = None):
        super().__init__("auth", message, cause)


class CreditsExhaustedError(NonRetryableError):
    def __init__(self, message: str, cause: Exception = None):
        super().__init__("credits", message, cause)


class BlankResponseError(RetryableError):
    """LLM returned empty/blank output."""
    pass


class MalformedOutputError(RetryableError):
    """LLM output could not be parsed into valid JSON/expected structure."""

    def __init__(self, message: str, cause: Exception = None):
        super().__init__(message, cause)
        self.error_type = "malformed_output"


def _parse_retry_after(response) -> float | None:
    """Parse Retry-After header (seconds) from an HTTP response, capped at max_delay."""
    if response is None:
        return None
    val = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def classify_api_error(exc: Exception) -> PipelineCallError:
    """Classify an OpenAI SDK exception into a PipelineCallError."""
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return RetryableError(f"Connection/timeout error: {exc}", cause=exc)
    elif isinstance(exc, RateLimitError):
        retry_after = _parse_retry_after(exc.response)
        return RetryableError(f"Rate limited: {exc}", cause=exc, retry_after=retry_after)
    elif isinstance(exc, AuthenticationError):
        return AuthError(f"Authentication failed: {exc}", cause=exc)
    elif isinstance(exc, PermissionDeniedError):
        return AuthError(f"Permission denied: {exc}", cause=exc)
    elif isinstance(exc, APIStatusError):
        sc = exc.status_code
        if 500 <= sc < 600:
            return RetryableError(f"Server error ({sc}): {exc}", cause=exc)
        elif sc == 429:
            retry_after = _parse_retry_after(exc.response)
            return RetryableError(f"Rate limited ({sc}): {exc}", cause=exc, retry_after=retry_after)
        elif sc == 402:
            return CreditsExhaustedError(f"Insufficient credits: {exc}", cause=exc)
        else:
            return NonRetryableError(f"API error ({sc}): {exc}", cause=exc)
    else:
        return NonRetryableError(f"Unexpected error: {exc}", cause=exc)


def retry_with_backoff(
    fn: callable,
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: float = 0.5,
):
    """Execute fn() with exponential backoff on RetryableError.

    max_attempts=3 means at most 3 total attempts (initial + 2 retries).
    Non-retryable errors propagate immediately. Retry-After is respected if present.
    """
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except RetryableError as e:
            last_exc = e
            if attempt == max_attempts - 1:
                raise
            if e.retry_after is not None:
                delay = min(e.retry_after, max_delay)
            else:
                delay = min(base_delay * (2 ** attempt), max_delay)
                delay += random.random() * jitter
            time.sleep(delay)
        except NonRetryableError as e:
            raise
        except Exception as e:
            classified = classify_api_error(e)
            if isinstance(classified, RetryableError):
                last_exc = classified
                if attempt == max_attempts - 1:
                    raise classified from e
                if classified.retry_after is not None:
                    delay = min(classified.retry_after, max_delay)
                else:
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    delay += random.random() * jitter
                time.sleep(delay)
            else:
                raise classified from e
    # Should be unreachable; last_exc is set if we exhausted retries.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry_with_backoff exhausted with no exception captured (max_attempts=0?)")


def is_blank_fragment(fragment: str) -> bool:
    """Return True if the fragment carries no meaningful content."""
    if not fragment or not fragment.strip():
        return True
    return False

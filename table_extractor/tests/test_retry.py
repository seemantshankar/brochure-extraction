import time
from unittest.mock import MagicMock

import pytest
from openai import AuthenticationError, APIStatusError

from table_extractor.retry import (
    PipelineCallError,
    RetryableError,
    NonRetryableError,
    AuthError,
    CreditsExhaustedError,
    BlankResponseError,
    MalformedOutputError,
    classify_api_error,
    retry_with_backoff,
    is_blank_fragment,
)


def test_is_blank_fragment_variants():
    assert is_blank_fragment("") is True
    assert is_blank_fragment("   \n  ") is True
    assert is_blank_fragment("<p>hi</p>") is False


def test_retry_exhausts_after_max_attempts():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        raise RetryableError("boom")

    with pytest.raises(RetryableError):
        retry_with_backoff(flaky, max_attempts=3, base_delay=0.001, max_delay=0.01, jitter=0.0)
    assert calls["n"] == 3  # exactly 3 total attempts


def test_retry_succeeds_on_second_attempt():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetryableError("boom")
        return "ok"

    result = retry_with_backoff(flaky, max_attempts=3, base_delay=0.001, max_delay=0.01, jitter=0.0)
    assert result == "ok"
    assert calls["n"] == 2


def test_non_retryable_propagates_immediately():
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise AuthError("bad key")

    with pytest.raises(AuthError):
        retry_with_backoff(boom, max_attempts=3)
    assert calls["n"] == 1  # never retried


def test_classify_auth_and_credits():
    resp = MagicMock()
    resp.status_code = 401
    resp.headers = {}
    assert isinstance(classify_api_error(AuthenticationError("x", response=resp, body=None)), AuthError)

    resp402 = MagicMock()
    resp402.status_code = 402
    resp402.headers = {}
    assert isinstance(classify_api_error(APIStatusError("x", response=resp402, body=None)), CreditsExhaustedError)


def test_classify_5xx_is_retryable():
    resp = MagicMock()
    resp.status_code = 503
    resp.headers = {}
    err = classify_api_error(APIStatusError("x", response=resp, body=None))
    assert isinstance(err, RetryableError)


def test_blank_response_error_is_retryable():
    e = BlankResponseError("empty")
    assert isinstance(e, RetryableError)
    assert e.error_type == "retryable"


def test_malformed_output_error_type():
    e = MalformedOutputError("bad json")
    assert e.error_type == "malformed_output"
    assert isinstance(e, RetryableError)


def test_retry_with_backoff_respects_retry_after_on_generic_error():
    calls = {"n": 0, "delays": []}

    def sleep_spy(sec):
        calls["delays"].append(sec)

    # Use a custom APIStatusError mock that classify_api_error classifies as RetryableError with retry_after
    from openai import APIStatusError
    resp = MagicMock()
    resp.status_code = 429
    resp.headers = {"retry-after": "5"}
    api_err = APIStatusError("too many requests", response=resp, body=None)

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise api_err
        return "success"

    import unittest.mock as mock
    with mock.patch("time.sleep", sleep_spy):
        res = retry_with_backoff(flaky, max_attempts=3, base_delay=0.001)

    assert res == "success"
    assert calls["n"] == 2
    assert calls["delays"] == [5.0]


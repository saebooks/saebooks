"""Unit tests for saebooks.services.bank_feeds.errors.

These cover the CDR ``ResponseErrorList`` parser + the status → subclass
dispatch. No HTTP involved.
"""
from __future__ import annotations

import pytest

from saebooks.services.bank_feeds.errors import (
    SissAuthError,
    SissError,
    SissRateLimitError,
    SissScopeError,
    SissServerError,
    SissValidationError,
)


def test_from_payload_401_returns_auth_error() -> None:
    err = SissError.from_payload(
        http_status=401,
        payload={"errors": [{"code": "AUTH01", "title": "Invalid token"}]},
        interaction_id="abc",
    )
    assert isinstance(err, SissAuthError)
    assert err.http_status == 401
    assert err.errors[0].code == "AUTH01"
    assert err.interaction_id == "abc"


def test_from_payload_403_returns_scope_error() -> None:
    err = SissError.from_payload(http_status=403, payload=None, interaction_id=None)
    assert isinstance(err, SissScopeError)


def test_from_payload_429_returns_rate_limit_error_default_retry_none() -> None:
    err = SissError.from_payload(http_status=429, payload=None, interaction_id=None)
    assert isinstance(err, SissRateLimitError)
    # from_payload alone doesn't set retry_after; the client does on the
    # 429 path with the actual Retry-After header.
    assert err.retry_after_seconds is None


def test_from_payload_422_returns_validation_error() -> None:
    payload = {
        "errors": [
            {"code": "0001", "title": "Invalid field", "detail": "accountId required"},
            {"code": "0002", "title": "Invalid field", "detail": "bsb required"},
        ]
    }
    err = SissError.from_payload(http_status=422, payload=payload, interaction_id=None)
    assert isinstance(err, SissValidationError)
    assert len(err.errors) == 2
    assert "+1 more" in str(err)


def test_from_payload_500_returns_server_error() -> None:
    err = SissError.from_payload(http_status=502, payload=None, interaction_id=None)
    assert isinstance(err, SissServerError)


def test_from_payload_tolerates_malformed_errors_list() -> None:
    err = SissError.from_payload(
        http_status=400,
        payload={"errors": "not a list"},
        interaction_id=None,
    )
    assert err.errors == []


def test_from_payload_tolerates_non_dict_entries() -> None:
    err = SissError.from_payload(
        http_status=400,
        payload={"errors": [{"code": "a"}, "junk", 42]},
        interaction_id=None,
    )
    assert len(err.errors) == 1
    assert err.errors[0].code == "a"


def test_rate_limit_error_carries_retry_after_when_set_manually() -> None:
    err = SissRateLimitError(
        "rate limited",
        http_status=429,
        retry_after_seconds=5.0,
    )
    assert err.retry_after_seconds == 5.0


@pytest.mark.parametrize(
    ("status", "subclass"),
    [
        (400, SissValidationError),
        (401, SissAuthError),
        (403, SissScopeError),
        (404, SissValidationError),
        (422, SissValidationError),
        (429, SissRateLimitError),
        (500, SissServerError),
        (503, SissServerError),
    ],
)
def test_status_to_subclass_mapping(status: int, subclass: type[SissError]) -> None:
    err = SissError.from_payload(http_status=status, payload=None, interaction_id=None)
    assert isinstance(err, subclass)

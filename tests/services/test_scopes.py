"""Unit tests for the API-token scope decision logic (A2).

Pure function tests — no DB, no app. Pins the method->scope mapping
and the backward-compatible full-access marker rules that
``require_bearer`` relies on.
"""
from __future__ import annotations

import pytest

from saebooks.services.scopes import (
    SCOPE_READ,
    SCOPE_WRITE,
    is_full_access,
    method_requires_scope,
    token_allows,
)


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "get", "head"])
def test_safe_methods_require_read(method):
    assert method_requires_scope(method) == SCOPE_READ


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post"])
def test_mutating_methods_require_write(method):
    assert method_requires_scope(method) == SCOPE_WRITE


@pytest.mark.parametrize(
    "scopes",
    [None, [], ["*"], ["full"], ["read", "write"], ["write", "read"],
     ["READ", "WRITE"], ["Full"]],
)
def test_full_access_markers(scopes):
    assert is_full_access(scopes) is True


@pytest.mark.parametrize("scopes", [["read"], ["write"], ["read", "invoices.x"]])
def test_restrictive_scopes_are_not_full(scopes):
    assert is_full_access(scopes) is False


def test_read_token_allows_get_denies_write():
    assert token_allows(["read"], "GET") is True
    assert token_allows(["read"], "HEAD") is True
    assert token_allows(["read"], "POST") is False
    assert token_allows(["read"], "DELETE") is False


def test_write_token_allows_write_and_read():
    # write implies the ability to mutate; a write-only token may still
    # read (mutations routinely read-then-write).
    assert token_allows(["write"], "POST") is True
    assert token_allows(["write"], "GET") is True


def test_full_access_allows_everything():
    for scopes in (None, [], ["*"], ["full"], ["read", "write"]):
        assert token_allows(scopes, "GET") is True
        assert token_allows(scopes, "POST") is True
        assert token_allows(scopes, "DELETE") is True

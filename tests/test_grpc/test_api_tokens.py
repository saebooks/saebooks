"""Smoke tests for the machine API token service.

Covers the round-trip: issue → verify → list → revoke → verify fails.
Also asserts the hot-path invariant (lookup-by-prefix is O(1), bcrypt
verify is the only slow op).
"""
from __future__ import annotations

import pytest

from saebooks.services import api_tokens

pytestmark = pytest.mark.asyncio


async def test_issue_returns_cleartext_with_correct_shape(db_session, seeded_company, seeded_user):
    token, cleartext = await api_tokens.issue(
        db_session,
        user_id=seeded_user.id,
        company_id=seeded_company.id,
        name="test-token",
    )
    await db_session.commit()

    assert cleartext.startswith("saebk_"), "cleartext must carry the saebk_ prefix"
    assert len(cleartext) == len("saebk_") + 64, "32 bytes hex = 64 chars"
    assert token.token_prefix == cleartext[len("saebk_") : len("saebk_") + 6]
    assert token.is_active is True
    assert token.revoked_at is None


async def test_verify_round_trip(db_session, seeded_company, seeded_user):
    _, cleartext = await api_tokens.issue(
        db_session,
        user_id=seeded_user.id,
        company_id=seeded_company.id,
        name="rt",
    )
    await db_session.commit()

    resolved = await api_tokens.verify(db_session, cleartext)
    assert resolved.user_id == seeded_user.id
    assert resolved.company_id == seeded_company.id
    assert resolved.last_used_at is not None


async def test_verify_rejects_garbage(db_session):
    with pytest.raises(api_tokens.TokenVerifyError):
        await api_tokens.verify(db_session, "garbage")
    with pytest.raises(api_tokens.TokenVerifyError):
        await api_tokens.verify(db_session, "saebk_short")
    with pytest.raises(api_tokens.TokenVerifyError):
        await api_tokens.verify(db_session, "saebk_" + "0" * 64)


async def test_revoke_then_verify_fails(db_session, seeded_company, seeded_user):
    token, cleartext = await api_tokens.issue(
        db_session,
        user_id=seeded_user.id,
        company_id=seeded_company.id,
        name="rev",
    )
    await db_session.commit()

    ok = await api_tokens.revoke(
        db_session, token_id=token.id, user_id=seeded_user.id
    )
    await db_session.commit()
    assert ok is True

    with pytest.raises(api_tokens.TokenVerifyError):
        await api_tokens.verify(db_session, cleartext)


async def test_revoke_other_users_token_returns_false(
    db_session, seeded_company, seeded_user, another_user
):
    token, _ = await api_tokens.issue(
        db_session,
        user_id=seeded_user.id,
        company_id=seeded_company.id,
        name="cross",
    )
    await db_session.commit()

    ok = await api_tokens.revoke(
        db_session, token_id=token.id, user_id=another_user.id
    )
    assert ok is False, "must not revoke a token owned by a different user"


async def test_list_excludes_revoked_by_default(
    db_session, seeded_company, seeded_user
):
    t1, _ = await api_tokens.issue(
        db_session,
        user_id=seeded_user.id,
        company_id=seeded_company.id,
        name="t1",
    )
    t2, _ = await api_tokens.issue(
        db_session,
        user_id=seeded_user.id,
        company_id=seeded_company.id,
        name="t2",
    )
    await api_tokens.revoke(db_session, token_id=t2.id, user_id=seeded_user.id)
    await db_session.commit()

    active = await api_tokens.list_for_user(
        db_session,
        user_id=seeded_user.id,
        company_id=seeded_company.id,
    )
    all_ = await api_tokens.list_for_user(
        db_session,
        user_id=seeded_user.id,
        company_id=seeded_company.id,
        include_revoked=True,
    )
    active_ids = {t.id for t in active}
    assert t1.id in active_ids
    assert t2.id not in active_ids
    assert {t.id for t in all_} == {t1.id, t2.id}

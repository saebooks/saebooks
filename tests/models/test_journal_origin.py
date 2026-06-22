"""JournalOrigin enum — bad-debt members exist and round-trip as strings.

The origin column is a plain ``String(32)`` (no DB enum), so adding members
needs no Alembic migration; this test pins that the two new bad-debt origins
are present and that their value round-trips through the StrEnum.
"""
from saebooks.models.journal import JournalOrigin


def test_bad_debt_writeoff_member_exists():
    assert JournalOrigin.BAD_DEBT_WRITEOFF.value == "BAD_DEBT_WRITEOFF"
    assert JournalOrigin("BAD_DEBT_WRITEOFF") is JournalOrigin.BAD_DEBT_WRITEOFF


def test_bad_debt_recovery_member_exists():
    assert JournalOrigin.BAD_DEBT_RECOVERY.value == "BAD_DEBT_RECOVERY"
    assert JournalOrigin("BAD_DEBT_RECOVERY") is JournalOrigin.BAD_DEBT_RECOVERY


def test_str_value_round_trips():
    for member in (
        JournalOrigin.BAD_DEBT_WRITEOFF,
        JournalOrigin.BAD_DEBT_RECOVERY,
    ):
        # StrEnum: str(member) is the value, and reconstructing from it
        # returns the same singleton.
        assert str(member) == member.value
        assert JournalOrigin(str(member)) is member

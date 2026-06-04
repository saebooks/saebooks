"""Regression guard for the 2026-05-25 DELETE-route void bug.

The bug: DELETE handlers for bills/invoices/credit_notes called the bare
``api_void()`` (archive only) for EVERY status, so a POSTED entity could be
DELETEd without reversing its GL journal entry — overstating AP/AR.

The fix routes POSTED voids through ``api_void_<entity>()`` (reverses the JE).
A DRAFT has no JE to reverse, so the DELETE handler legitimately archives it
via ``api_void()`` (op="archive", 204) — the handler branches on status. The
POST /{id}/void *action* stays strict (rejects DRAFT with 422).

This guard enforces the POSITIVE invariant rather than banning ``api_void``
outright (which would forbid the legitimate DRAFT-archive branch):

  1. Each route MUST wire the JE-reversing ``svc.api_void_<entity>(`` so
     POSTED voids reverse the journal entry.
  2. Any bare ``svc.api_void(`` callsite MUST sit inside a DRAFT-guarded
     branch (a ``.DRAFT`` status check within the preceding few lines) —
     never an unconditional archive of all statuses (the original bug).

The payments route is exempt: payments has a single canonical ``api_void``
(no sibling), so ``svc.api_void(`` there is correct.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# route file -> its JE-reversing void function
ROUTE_VOID = {
    "saebooks/api/v1/bills.py": "api_void_bill",
    "saebooks/api/v1/invoices.py": "api_void_invoice",
    "saebooks/api/v1/credit_notes.py": "api_void_credit_note",
}
# how many lines before a bare api_void( may carry its DRAFT guard
_DRAFT_LOOKBACK = 6


def test_bills_invoices_credit_notes_use_proper_void() -> None:
    problems: list[str] = []
    for rel, reversing_fn in ROUTE_VOID.items():
        src = (REPO_ROOT / rel).read_text()
        lines = src.splitlines()

        # (1) the JE-reversing void must be wired
        if f"svc.{reversing_fn}(" not in src:
            problems.append(
                f"{rel}: missing svc.{reversing_fn}( — POSTED voids must "
                "reverse the GL journal entry."
            )

        # (2) every bare svc.api_void( must be inside a DRAFT-guarded branch
        for i, line in enumerate(lines):
            if "svc.api_void(" in line:
                window = lines[max(0, i - _DRAFT_LOOKBACK):i + 1]
                if not any(".DRAFT" in w for w in window):
                    problems.append(
                        f"{rel}:{i + 1}: bare svc.api_void( not guarded by a "
                        ".DRAFT status check — POSTED entities must reverse the "
                        f"JE via svc.{reversing_fn}(, not be archived."
                    )

    assert not problems, (
        "DELETE-route void invariant violated:\n" + "\n".join(problems)
    )

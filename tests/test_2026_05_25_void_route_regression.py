"""Regression guard for the 2026-05-25 DELETE-route → bare-api_void bug.

bills/invoices/credit_notes each have TWO void functions in the service
layer: the proper ``api_void_<entity>()`` (checks paid + reverses JE) and
the older bare ``api_void()`` (skips both). DELETE handlers in the API
were wired to the bare one — a paid bill could be DELETEd and the GL
JE would stay live, overstating AP/AR. Fixed by routing the DELETE
handlers to ``api_void_<entity>`` and passing ``tenant_id=tenant_id``
for defense-in-depth.

This test fires if anyone reintroduces ``await svc.api_void(`` in a
bills/invoices/credit_notes route — that bare callsite is the bug.

The payments route is exempt: payments has a single canonical
``api_void`` (no sibling), so ``svc.api_void(`` there is correct.
"""
from __future__ import annotations

import re
from pathlib import Path


ROUTES = (
    "saebooks/api/v1/bills.py",
    "saebooks/api/v1/invoices.py",
    "saebooks/api/v1/credit_notes.py",
)
REPO_ROOT = Path(__file__).resolve().parents[1]
# Match bare `svc.api_void(` but NOT `svc.api_void_anything(`.
BAD_PATTERN = re.compile(r"svc\.api_void\(")


def test_bills_invoices_credit_notes_use_proper_void() -> None:
    offenders: list[str] = []
    for rel in ROUTES:
        path = REPO_ROOT / rel
        src = path.read_text()
        for lineno, line in enumerate(src.splitlines(), start=1):
            if BAD_PATTERN.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "DELETE-route regression: bills/invoices/credit_notes routes "
        "must call api_void_bill/api_void_invoice/api_void_credit_note "
        "(which reverse the JE on POSTED entities), NOT bare api_void.\n"
        + "\n".join(offenders)
    )

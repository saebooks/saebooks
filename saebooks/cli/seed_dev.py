"""Dev-install seed script.

Usage::

    python -m saebooks.cli.seed_dev

Seeds a fresh development install with:

1. Default company (UUID ``00000000-0000-0000-0000-000000000001``).
2. Default admin user (password-login capable).
3. AU chart of accounts (from the Odoo l10n_au CSV fixtures).
4. AU GST tax codes (the canonical six-code starter set).

All steps are idempotent — safe to re-run on every container start when
``SAEBOOKS_RUN_SEED=true`` is set.

Environment variables
---------------------
``SAEBOOKS_DEV_COMPANY_NAME``   Company name (default: "My Company").
``SAEBOOKS_DEV_ADMIN_EMAIL``    Admin user email (default: admin@example.com).
``SAEBOOKS_DEV_ADMIN_PASSWORD`` Admin plain-text password (default: changeme).
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.user import User, UserRole
from saebooks.seed.load_au_coa import main as load_au_coa
from saebooks.services.companies import ensure_seed_company
from saebooks.services.jwt_tokens import hash_password
from saebooks.services.tax_codes import ensure_au_seed as ensure_tax_codes

logger = logging.getLogger("saebooks.cli.seed_dev")

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _dev_company_name() -> str:
    return os.environ.get("SAEBOOKS_DEV_COMPANY_NAME", "My Company")


def _dev_admin_email() -> str:
    return os.environ.get("SAEBOOKS_DEV_ADMIN_EMAIL", "admin@example.com")


def _dev_admin_password() -> str:
    return os.environ.get("SAEBOOKS_DEV_ADMIN_PASSWORD", "changeme")


async def seed_company(session: AsyncSession) -> tuple[Company, bool]:
    """Ensure the default company exists.

    Returns ``(company, created)`` where ``created`` is False when the row
    was already present.
    """
    # ensure_seed_company reads SEED_COMPANY_NAME from settings; we also
    # want to honour the dev-specific env var here.  Mirror the check
    # directly so we can control the name without touching config.
    existing = await session.execute(
        select(Company).where(
            Company.id == _DEFAULT_TENANT_ID,
        )
    )
    row = existing.scalars().first()
    if row is not None:
        return row, False

    # Fall back to ensure_seed_company which respects SEED_COMPANY_NAME
    # and creates with the correct tenant_id.  If SEED_COMPANY_NAME is
    # unset we patch the env so it gets the dev default.
    original = os.environ.get("SEED_COMPANY_NAME")
    name = _dev_company_name()
    os.environ["SEED_COMPANY_NAME"] = name
    try:
        company = await ensure_seed_company(session)
    finally:
        if original is None:
            os.environ.pop("SEED_COMPANY_NAME", None)
        else:
            os.environ["SEED_COMPANY_NAME"] = original
    return company, True


async def seed_admin_user(session: AsyncSession) -> tuple[User, bool]:
    """Ensure the default admin user exists.

    Returns ``(user, created)``.  Skips if a user with the same email
    already exists (matched by email rather than username for robustness).
    """
    email = _dev_admin_email()
    existing = await session.execute(
        select(User).where(User.email == email)
    )
    row = existing.scalars().first()
    if row is not None:
        return row, False

    username = email.split("@")[0]
    user = User(
        tenant_id=_DEFAULT_TENANT_ID,
        username=username,
        display_name="Admin",
        email=email,
        role=UserRole.ADMIN.value,
        password_hash=hash_password(_dev_admin_password()),
        version=1,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user, True


async def seed_tax_codes(session: AsyncSession, company: Company) -> int:
    """Ensure AU GST tax codes exist for the company.

    Returns the number of new codes inserted.
    """
    return await ensure_tax_codes(session, company.id)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    # Step 1: company
    async with AsyncSessionLocal() as session:
        company, created = await seed_company(session)
        logger.info(
            "Company: %s (%s) — %s",
            company.name,
            company.id,
            "created" if created else "already exists",
        )

    # Step 2: admin user
    async with AsyncSessionLocal() as session:
        user, created = await seed_admin_user(session)
        logger.info(
            "Admin user: %s (%s) — %s",
            user.email,
            user.id,
            "created" if created else "already exists",
        )

    # Step 3 + 4: AU CoA and tax codes (load_au_coa already calls
    # ensure_seed_company + ensure_au_seed internally; just call it)
    await load_au_coa()


if __name__ == "__main__":
    asyncio.run(main())

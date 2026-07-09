"""Idempotent seed: ensure the default company exists.

Run: `docker compose exec app python -m saebooks.seed.seed_company`
"""
import asyncio
import logging

from saebooks.db import AsyncSessionLocal
from saebooks.services.companies import ensure_seed_company

logger = logging.getLogger("saebooks.seed")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    async with AsyncSessionLocal() as session:
        company = await ensure_seed_company(session)
        logger.info("Seed company: %s (%s)", company.name, company.id)


if __name__ == "__main__":
    asyncio.run(main())

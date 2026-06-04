import pytest

from saebooks.db import AsyncSessionLocal
from saebooks.services import settings as svc

pytestmark = pytest.mark.postgres_only


async def test_settings_round_trip() -> None:
    async with AsyncSessionLocal() as session:
        await svc.set(session, "test_key", {"nested": [1, 2, 3]})
        value = await svc.get(session, "test_key")
        assert value == {"nested": [1, 2, 3]}

        await svc.set(session, "test_key", "replaced")
        assert await svc.get(session, "test_key") == "replaced"


async def test_settings_defaults_present() -> None:
    async with AsyncSessionLocal() as session:
        assert await svc.get(session, "base_currency") == "AUD"
        assert await svc.get(session, "fin_year_start_month") == 7
        assert await svc.get(session, "audit_mode") == "immutable"

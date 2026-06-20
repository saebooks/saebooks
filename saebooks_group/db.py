"""Broker DB engine + declarative base (saebooks_group). No GL, ever."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from saebooks_group.config import settings


class Base(DeclarativeBase):
    """Declarative base for the broker's two tables (pair_registry, relay_log)."""


engine = create_async_engine(
    settings.database_url, echo=False, future=True, poolclass=NullPool
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

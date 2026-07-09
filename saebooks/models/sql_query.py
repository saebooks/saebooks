"""SQL query history — record of ad-hoc queries run through /admin/sql.

Every query execution (successful or not) is appended here. This gives us
a record of what's been run against the DB, even when the query produces
no results or errors out.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class SqlQuery(Base):
    __tablename__ = "sql_queries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sql: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="Rows returned (writes are rejected so this is always SELECT output)",
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    error: Mapped[str | None] = mapped_column(
        Text, comment="If the query failed, the error message",
    )
    performed_by: Mapped[str | None] = mapped_column(String(64))
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

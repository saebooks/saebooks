"""Pydantic 2.x request/response models for API v1.

Kept in one module for Phase 0 — once more entities land, split into
``schemas/<entity>.py`` files under ``api/v1/schemas/``.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from saebooks.models.contact import ContactType


class ContactBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str = Field(min_length=1, max_length=255)
    contact_type: ContactType
    email: str | None = None
    phone: str | None = None
    abn: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    postcode: str | None = None
    country: str | None = "Australia"
    notes: str | None = None
    default_account_id: uuid.UUID | None = None
    default_tax_code: str | None = None
    bank_bsb: str | None = None
    bank_account_number: str | None = None
    bank_account_title: str | None = None


class ContactCreate(ContactBase):
    """POST body."""


class ContactUpdate(BaseModel):
    """PATCH body — every field optional. ``None`` clears, missing leaves alone.

    Because "omit" vs "set to None" matters, callers iterate
    ``model_dump(exclude_unset=True)`` at the route layer.
    """

    model_config = ConfigDict(from_attributes=True)

    name: str | None = Field(default=None, min_length=1, max_length=255)
    contact_type: ContactType | None = None
    email: str | None = None
    phone: str | None = None
    abn: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    postcode: str | None = None
    country: str | None = None
    notes: str | None = None
    default_account_id: uuid.UUID | None = None
    default_tax_code: str | None = None


class ContactOut(ContactBase):
    id: uuid.UUID
    company_id: uuid.UUID
    version: int
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ContactListOut(BaseModel):
    items: list[ContactOut]
    total: int
    limit: int
    offset: int


class ConflictBody(BaseModel):
    """409 response body — includes the current server state so the client
    can show a three-way reconcile dialog."""

    detail: str
    current: ContactOut


class ChangeLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entity: str
    entity_id: uuid.UUID
    op: str
    actor: str
    at: datetime
    version: int
    payload: dict

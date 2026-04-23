"""Pydantic 2.x request/response models for API v1.

Kept in one module for Phase 0/1 — once more entities land, split into
``schemas/<entity>.py`` files under ``api/v1/schemas/``.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from saebooks.models.contact import ContactType
from saebooks.models.account import AccountType


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
    tenant_id: uuid.UUID
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


# ---------------------------------------------------------------------------
# Accounts (Chart of Accounts) — Phase 1 tier-1
# ---------------------------------------------------------------------------


class AccountBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    account_type: AccountType
    parent_id: uuid.UUID | None = None
    tax_code_default: str | None = None
    is_header: bool = False
    reconcile: bool = False


class AccountCreate(AccountBase):
    """POST body for creating a new account."""


class AccountUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    code: str | None = Field(default=None, min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    account_type: AccountType | None = None
    tax_code_default: str | None = None
    is_header: bool | None = None
    reconcile: bool | None = None


class AccountOut(AccountBase):
    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    version: int
    system_managed: bool
    bsb: str | None = None
    bank_account_number: str | None = None
    bank_account_title: str | None = None
    apca_user_id: str | None = None
    bank_abbreviation: str | None = None
    created_at: datetime
    archived_at: datetime | None = None


class AccountListOut(BaseModel):
    items: list[AccountOut]
    total: int
    limit: int
    offset: int


class AccountConflictBody(BaseModel):
    detail: str
    current: AccountOut


# ---------------------------------------------------------------------------
# Companies — Phase 1 tier-1 (FLAG_MULTI_COMPANY present in codebase)
# ---------------------------------------------------------------------------


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    legal_name: str | None = None
    trading_name: str | None = None
    abn: str | None = None
    acn: str | None = None
    base_currency: str
    fin_year_start_month: int
    audit_mode: str
    version: int
    created_at: datetime
    archived_at: datetime | None = None


class CompanyListOut(BaseModel):
    items: list[CompanyOut]
    total: int


class CompanyUpdate(BaseModel):
    """PATCH body for updating company metadata — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    name: str | None = Field(default=None, min_length=1, max_length=255)
    legal_name: str | None = None
    trading_name: str | None = None
    abn: str | None = None
    acn: str | None = None
    base_currency: str | None = Field(default=None, min_length=3, max_length=3)
    fin_year_start_month: int | None = Field(default=None, ge=1, le=12)
    audit_mode: str | None = None


class CompanyConflictBody(BaseModel):
    detail: str
    current: CompanyOut


# ---------------------------------------------------------------------------
# Tax Codes — Phase 1 tier-1 (cycle 3)
# ---------------------------------------------------------------------------


class TaxCodeBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str = Field(min_length=1, max_length=16)
    name: str = Field(min_length=1, max_length=255)
    rate: Decimal
    tax_system: str = Field(default="GST", min_length=1, max_length=16)
    reporting_type: str = Field(default="taxable", min_length=1, max_length=32)
    description: str | None = None


class TaxCodeCreate(TaxCodeBase):
    """POST body for creating a new tax code."""


class TaxCodeUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    code: str | None = Field(default=None, min_length=1, max_length=16)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    rate: Decimal | None = None
    tax_system: str | None = Field(default=None, min_length=1, max_length=16)
    reporting_type: str | None = Field(default=None, min_length=1, max_length=32)
    description: str | None = None


class TaxCodeOut(TaxCodeBase):
    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    version: int
    created_at: datetime
    archived_at: datetime | None = None


class TaxCodeListOut(BaseModel):
    items: list[TaxCodeOut]
    total: int
    limit: int
    offset: int


class TaxCodeConflictBody(BaseModel):
    detail: str
    current: TaxCodeOut


# ---------------------------------------------------------------------------
# Users — Phase 1 tier-2 (cycle 4)
# NOTE: password fields are deliberately absent from ALL user schemas.
# ---------------------------------------------------------------------------


class UserBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    username: str = Field(min_length=1, max_length=64)
    display_name: str | None = None
    email: str | None = None
    role: str = Field(default="readonly", min_length=1, max_length=16)
    preferred_theme: str | None = None


class UserCreate(UserBase):
    """POST body for creating a new user (admin only)."""


class UserUpdate(BaseModel):
    """PATCH body — every field optional. Never includes password."""

    model_config = ConfigDict(from_attributes=True)

    display_name: str | None = None
    email: str | None = None
    role: str | None = Field(default=None, min_length=1, max_length=16)
    preferred_theme: str | None = None


class UserOut(BaseModel):
    """Response model — NO password_hash or any secret field."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    username: str
    display_name: str | None = None
    email: str | None = None
    role: str
    preferred_theme: str | None = None
    last_seen_at: datetime | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class UserListOut(BaseModel):
    items: list[UserOut]
    total: int
    limit: int
    offset: int


class UserConflictBody(BaseModel):
    detail: str
    current: UserOut


# ---------------------------------------------------------------------------
# Permissions — Phase 1 tier-2 (cycle 4)
# The monolith uses a three-layer model:
#   Permission (catalogue) + RolePermission (role→code M2M)
#   + UserPermission (per-user override grant/revoke).
# The API surfaces the resolved set for a user plus the full catalogue.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Items — Phase 1 tier-2 (cycle 5)
# ---------------------------------------------------------------------------


class ItemBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    item_type: str = Field(default="inventory", min_length=1, max_length=16)
    description: str | None = None
    cost_method: str = Field(default="WAC", min_length=1, max_length=16)
    default_sale_price: Decimal = Decimal("0")
    inventory_account_id: uuid.UUID
    cogs_account_id: uuid.UUID
    income_account_id: uuid.UUID


class ItemCreate(ItemBase):
    """POST body for creating a new item."""

    on_hand_qty: Decimal = Decimal("0")
    wac_cost: Decimal = Decimal("0")


class ItemUpdate(BaseModel):
    """PATCH body — every field optional. on_hand_qty/wac_cost/cost_method/item_type not editable here."""

    model_config = ConfigDict(from_attributes=True)

    sku: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    default_sale_price: Decimal | None = None
    inventory_account_id: uuid.UUID | None = None
    cogs_account_id: uuid.UUID | None = None
    income_account_id: uuid.UUID | None = None


class ItemOut(ItemBase):
    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    on_hand_qty: Decimal
    wac_cost: Decimal
    version: int
    created_at: datetime
    archived_at: datetime | None = None


class ItemListOut(BaseModel):
    items: list[ItemOut]
    total: int
    limit: int
    offset: int


class ItemConflictBody(BaseModel):
    detail: str
    current: ItemOut


class StockOut(BaseModel):
    """Response body for GET /api/v1/items/{id}/stock."""

    model_config = ConfigDict(from_attributes=True)

    item_id: uuid.UUID
    sku: str
    item_type: str
    on_hand_qty: Decimal
    wac_cost: Decimal
    inventory_value: Decimal  # on_hand_qty * wac_cost


# ---------------------------------------------------------------------------
# Journal Entries — Phase 1 tier-3 (cycle 6)
# ---------------------------------------------------------------------------


class JournalLineOut(BaseModel):
    """One line of a journal entry."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_no: int
    account_id: uuid.UUID
    description: str | None = None
    debit: Decimal
    credit: Decimal
    tax_code_id: uuid.UUID | None = None
    gst_amount: Decimal | None = None
    project_id: uuid.UUID | None = None


class JournalLineCreate(BaseModel):
    """One line in a POST/PATCH payload."""

    account_id: uuid.UUID
    description: str | None = None
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    tax_code_id: uuid.UUID | None = None
    gst_amount: Decimal | None = None
    project_id: uuid.UUID | None = None


class JournalEntryBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    entry_date: date
    narration: str | None = None
    reference: str | None = None


class JournalEntryCreate(JournalEntryBase):
    """POST body."""

    lines: list[JournalLineCreate] = Field(default_factory=list)


class JournalEntryUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    entry_date: date | None = None
    narration: str | None = None
    reference: str | None = None
    status: str | None = None
    lines: list[JournalLineCreate] | None = None


class JournalEntryOut(BaseModel):
    """Full response — includes nested lines, tenant_id, version."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    ref: str
    entry_date: date
    description: str | None = None
    status: str
    posted_at: datetime | None = None
    posted_by: str | None = None
    reversal_of_id: uuid.UUID | None = None
    override_reason: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    lines: list[JournalLineOut] = Field(default_factory=list)


class JournalEntryListOut(BaseModel):
    items: list[JournalEntryOut]
    total: int
    limit: int
    offset: int


class JournalEntryConflictBody(BaseModel):
    detail: str
    current: JournalEntryOut


class PermissionOut(BaseModel):
    """One entry in the permission catalogue."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    description: str
    created_at: datetime


class UserPermissionOut(BaseModel):
    """A permission code as it applies to a user (resolved = True means granted)."""

    code: str
    description: str
    resolved: bool  # True = user has this permission; False = revoked / absent


class UserPermissionsBody(BaseModel):
    """PUT /api/v1/users/{id}/permissions — replace the user's per-user overrides."""

    grants: list[str] = Field(
        default_factory=list,
        description="Permission codes to explicitly grant (override role).",
    )
    revokes: list[str] = Field(
        default_factory=list,
        description="Permission codes to explicitly revoke (override role).",
    )


# ---------------------------------------------------------------------------
# Invoices — Phase 1 tier-3 (cycle 7)
# ---------------------------------------------------------------------------


class InvoiceLineOut(BaseModel):
    """One line of an invoice (nested in InvoiceOut)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_no: int
    description: str
    account_id: uuid.UUID
    tax_code_id: uuid.UUID | None = None
    quantity: Decimal
    unit_price: Decimal
    discount_pct: Decimal
    line_subtotal: Decimal
    line_tax: Decimal
    line_total: Decimal
    project_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None


class InvoiceLineCreate(BaseModel):
    """One line in a POST/PATCH payload."""

    description: str = Field(min_length=1)
    account_id: uuid.UUID
    tax_code_id: uuid.UUID | None = None
    quantity: Decimal = Decimal("1")
    unit_price: Decimal = Decimal("0")
    discount_pct: Decimal = Decimal("0")
    project_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None


class InvoiceBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    contact_id: uuid.UUID
    issue_date: date
    due_date: date
    notes: str | None = None
    payment_terms: str | None = None
    currency: str = Field(default="AUD", min_length=3, max_length=3)


class InvoiceCreate(InvoiceBase):
    """POST body."""

    lines: list[InvoiceLineCreate] = Field(default_factory=list)


class InvoiceUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    contact_id: uuid.UUID | None = None
    issue_date: date | None = None
    due_date: date | None = None
    notes: str | None = None
    payment_terms: str | None = None
    lines: list[InvoiceLineCreate] | None = None


class InvoiceOut(BaseModel):
    """Full invoice response — includes nested lines, tenant_id, version."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    contact_id: uuid.UUID
    number: str | None = None
    issue_date: date
    due_date: date
    status: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    amount_paid: Decimal
    currency: str
    fx_rate: Decimal
    notes: str | None = None
    payment_terms: str | None = None
    posted_at: datetime | None = None
    posted_by: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    lines: list[InvoiceLineOut] = Field(default_factory=list)


class InvoiceListOut(BaseModel):
    items: list[InvoiceOut]
    total: int
    limit: int
    offset: int


class InvoiceConflictBody(BaseModel):
    detail: str
    current: InvoiceOut


# ---------------------------------------------------------------------------
# Bills — Phase 1 tier-3 (cycle 8)
# ---------------------------------------------------------------------------


class BillLineOut(BaseModel):
    """One line of a bill (nested in BillOut)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_no: int
    description: str
    account_id: uuid.UUID
    tax_code_id: uuid.UUID | None = None
    quantity: Decimal
    unit_price: Decimal
    discount_pct: Decimal
    line_subtotal: Decimal
    line_tax: Decimal
    line_total: Decimal
    project_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None


class BillLineCreate(BaseModel):
    """One line in a POST/PATCH payload."""

    description: str = Field(min_length=1)
    account_id: uuid.UUID
    tax_code_id: uuid.UUID | None = None
    quantity: Decimal = Decimal("1")
    unit_price: Decimal = Decimal("0")
    discount_pct: Decimal = Decimal("0")
    project_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None


class BillBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    contact_id: uuid.UUID
    issue_date: date
    due_date: date
    notes: str | None = None
    supplier_reference: str | None = None
    currency: str = Field(default="AUD", min_length=3, max_length=3)


class BillCreate(BillBase):
    """POST body."""

    lines: list[BillLineCreate] = Field(default_factory=list)


class BillUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    contact_id: uuid.UUID | None = None
    issue_date: date | None = None
    due_date: date | None = None
    notes: str | None = None
    supplier_reference: str | None = None
    lines: list[BillLineCreate] | None = None


class BillOut(BaseModel):
    """Full bill response — includes nested lines, tenant_id, version."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    contact_id: uuid.UUID
    number: str | None = None
    supplier_reference: str | None = None
    issue_date: date
    due_date: date
    status: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    amount_paid: Decimal
    currency: str
    fx_rate: Decimal
    notes: str | None = None
    posted_at: datetime | None = None
    posted_by: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    lines: list[BillLineOut] = Field(default_factory=list)


class BillListOut(BaseModel):
    items: list[BillOut]
    total: int
    limit: int
    offset: int


class BillConflictBody(BaseModel):
    detail: str
    current: BillOut


# ---------------------------------------------------------------------------
# Payments — Phase 1 tier-3 (cycle 9)
# ---------------------------------------------------------------------------


class PaymentAllocationOut(BaseModel):
    """One allocation nested inside PaymentOut."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    payment_id: uuid.UUID
    invoice_id: uuid.UUID | None = None
    bill_id: uuid.UUID | None = None
    credit_note_id: uuid.UUID | None = None
    amount: Decimal


class PaymentAllocationCreate(BaseModel):
    """Allocation sub-object in a POST/PATCH payload."""

    invoice_id: uuid.UUID | None = None
    bill_id: uuid.UUID | None = None
    credit_note_id: uuid.UUID | None = None
    amount: Decimal


class PaymentBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    contact_id: uuid.UUID
    bank_account_id: uuid.UUID
    payment_date: date
    amount: Decimal
    direction: str = "INCOMING"
    method: str = "eft"
    reference: str | None = None
    notes: str | None = None
    currency: str = Field(default="AUD", min_length=3, max_length=3)


class PaymentCreate(PaymentBase):
    """POST body."""

    allocations: list[PaymentAllocationCreate] = Field(default_factory=list)


class PaymentUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    contact_id: uuid.UUID | None = None
    bank_account_id: uuid.UUID | None = None
    payment_date: date | None = None
    amount: Decimal | None = None
    direction: str | None = None
    method: str | None = None
    reference: str | None = None
    notes: str | None = None
    currency: str | None = None
    allocations: list[PaymentAllocationCreate] | None = None


class PaymentOut(BaseModel):
    """Full payment response — includes nested allocations, tenant_id, version."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    contact_id: uuid.UUID
    bank_account_id: uuid.UUID
    number: str | None = None
    direction: str
    method: str
    status: str
    payment_date: date
    amount: Decimal
    currency: str
    fx_rate: Decimal
    base_amount: Decimal
    reference: str | None = None
    notes: str | None = None
    posted_at: datetime | None = None
    posted_by: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    allocations: list[PaymentAllocationOut] = Field(default_factory=list)


class PaymentListOut(BaseModel):
    items: list[PaymentOut]
    total: int
    limit: int
    offset: int


class PaymentConflictBody(BaseModel):
    detail: str
    current: PaymentOut

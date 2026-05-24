"""Pydantic 2.x request/response models for API v1.

Kept in one module for Phase 0/1 — once more entities land, split into
``schemas/<entity>.py`` files under ``api/v1/schemas/``.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from saebooks.models.contact import ContactType
from saebooks.models.account import AccountType
from saebooks.models.recurring_invoice import RecurrenceFrequency, RecurrenceStatus


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
    currency_code: str | None = Field(default=None, max_length=3, description="ISO 4217 billing currency")
    is_one_off: bool = Field(
        default=False,
        description="Hidden from main /contacts list. Cash purchases / walk-ins / once-off counterparties.",
    )


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
    currency_code: str | None = Field(default=None, max_length=3)
    is_one_off: bool | None = None


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


class OneOffCandidate(BaseModel):
    """One row of /api/v1/contacts/one-off-candidates."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    contact_type: str
    kind: str | None = None
    posted_date: str | None = None
    posted_total: str | None = None
    last_seen: str | None = None


class OneOffCandidatesOut(BaseModel):
    items: list[OneOffCandidate]
    total: int


class OneOffBulkTagRequest(BaseModel):
    """POST body for /api/v1/contacts/bulk-tag-one-off.

    Flips ``is_one_off`` on all listed contacts. Default ``True`` retains
    the original mark these as one-offs semantics; pass ``False`` to
    move contacts back to the main pool.
    """

    contact_ids: list[uuid.UUID] = Field(min_length=1)
    is_one_off: bool = True


class OneOffBulkTagOut(BaseModel):
    flipped: int


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
    gst_registered: bool = False
    gst_effective_date: date | None = None
    psi_status: str = "unsure"
    address: dict[str, Any] | None = None
    # Cashbook edition (single-entry UI mode). Surfaced here so the
    # web UI can hide full-edition menu items when mode='cashbook'.
    # See docs/cashbook-edition-design.md §7.
    bookkeeping_mode: str = "full"
    cashbook_default_bank_account_id: uuid.UUID | None = None
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
    gst_registered: bool | None = None
    gst_effective_date: date | None = None
    psi_status: str | None = None
    address: dict[str, Any] | None = None

    @field_validator("psi_status")
    @classmethod
    def psi_status_valid(cls, v: str | None) -> str | None:
        if v is None:
            return v
        valid = {"yes", "no", "unsure"}
        if v not in valid:
            raise ValueError(f"psi_status must be one of: {sorted(valid)}")
        return v

    @field_validator("gst_effective_date")
    @classmethod
    def effective_date_not_future(cls, v: date | None) -> date | None:
        if v is None:
            return v
        today = date.today()
        if v > today:
            raise ValueError("gst_effective_date cannot be in the future")
        # ATO allows backdating up to 4 years
        earliest = today.replace(year=today.year - 4)
        if v < earliest:
            raise ValueError(
                "gst_effective_date cannot be more than 4 years in the past (ATO limit)"
            )
        return v


class CompanyCreate(BaseModel):
    """POST body for creating a new company."""

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(min_length=1, max_length=255)
    legal_name: str | None = None
    trading_name: str | None = None
    abn: str | None = None
    acn: str | None = None
    base_currency: str = Field(default="AUD", min_length=3, max_length=3)
    fin_year_start_month: int = Field(default=7, ge=1, le=12)


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
    role: str = Field(default="viewer", min_length=1, max_length=16)
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
    account_code: str | None = None
    account_name: str | None = None
    description: str | None = None
    debit: Decimal
    credit: Decimal
    tax_code_id: uuid.UUID | None = None
    gst_amount: Decimal | None = None
    project_id: uuid.UUID | None = None

    @model_validator(mode="before")
    @classmethod
    def _extract_account_fields(cls, v: object) -> object:
        """Populate account_code/account_name from the loaded account relationship."""
        if hasattr(v, "account") and v.account is not None:  # type: ignore[union-attr]
            acct = v.account  # type: ignore[union-attr]
            # Return a dict so subsequent field validators see the data.
            data = {
                "id": v.id,  # type: ignore[union-attr]
                "line_no": v.line_no,  # type: ignore[union-attr]
                "account_id": v.account_id,  # type: ignore[union-attr]
                "account_code": getattr(acct, "code", None),
                "account_name": getattr(acct, "name", None),
                "description": v.description,  # type: ignore[union-attr]
                "debit": v.debit,  # type: ignore[union-attr]
                "credit": v.credit,  # type: ignore[union-attr]
                "tax_code_id": v.tax_code_id,  # type: ignore[union-attr]
                "gst_amount": v.gst_amount,  # type: ignore[union-attr]
                "project_id": v.project_id,  # type: ignore[union-attr]
            }
            return data
        return v


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
    reference: str | None = Field(default=None, max_length=32)


class JournalEntryCreate(JournalEntryBase):
    """POST body."""

    lines: list[JournalLineCreate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _lines_must_balance(self) -> "JournalEntryCreate":
        """Reject unbalanced journal entries at the schema level (fast 422)."""
        if not self.lines:
            return self
        total_debit = sum(ln.debit for ln in self.lines)
        total_credit = sum(ln.credit for ln in self.lines)
        if total_debit != total_credit:
            raise ValueError(
                f"Journal entry lines are unbalanced: "
                f"debits={total_debit}, credits={total_credit}"
            )
        return self


class JournalEntryUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    entry_date: date | None = None
    narration: str | None = None
    reference: str | None = Field(default=None, max_length=32)
    status: str | None = None
    lines: list[JournalLineCreate] | None = None

    @model_validator(mode="after")
    def _lines_must_balance(self) -> "JournalEntryUpdate":
        """Reject unbalanced line replacements at the schema level (fast 422)."""
        if self.lines is None or len(self.lines) == 0:
            return self
        total_debit = sum(ln.debit for ln in self.lines)
        total_credit = sum(ln.credit for ln in self.lines)
        if total_debit != total_credit:
            raise ValueError(
                f"Journal entry lines are unbalanced: "
                f"debits={total_debit}, credits={total_credit}"
            )
        return self


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
    # Source document back-link (#27). Populated by the get-by-id handler
    # via get_source_doc(); not stored on the JE row — computed from the
    # reverse FK on invoices/bills/credit_notes/expenses/payments.
    source_type: str | None = None
    source_id: uuid.UUID | None = None
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


class JournalEntryPostBody(BaseModel):
    """Optional body for POST /{id}/post — carries override_reason for period-lock bypass."""

    override_reason: str | None = None


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
    service_start_date: date | None = None
    service_end_date: date | None = None


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
    service_start_date: date | None = None
    service_end_date: date | None = None


class InvoiceBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    contact_id: uuid.UUID | None = None
    issue_date: date
    due_date: date
    settlement_date: date | None = None
    notes: str | None = None
    payment_terms: str | None = None
    currency: str = Field(default="AUD", min_length=3, max_length=3)


class InvoiceCreate(InvoiceBase):
    """POST body."""

    lines: list[InvoiceLineCreate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _total_must_be_non_negative(self) -> "InvoiceCreate":
        """Reject invoices whose computed gross total would be negative.

        Negative totals must go through the credit-note path instead
        (Lane 1 P2 / overnight critic finding #28).
        """
        if not self.lines:
            return self
        from decimal import Decimal as _D
        total = sum(
            ln.unit_price * ln.quantity * (_D("1") - ln.discount_pct / _D("100"))
            for ln in self.lines
        )
        if total < _D("0"):
            raise ValueError(
                "invoice_negative_total: invoice total cannot be negative; "
                "use a credit note for refunds or adjustments"
            )
        return self


class InvoiceUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    contact_id: uuid.UUID | None = None
    issue_date: date | None = None
    due_date: date | None = None
    settlement_date: date | None = None
    notes: str | None = None
    payment_terms: str | None = None
    lines: list[InvoiceLineCreate] | None = None


class InvoiceOut(BaseModel):
    """Full invoice response — includes nested lines, tenant_id, version."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    contact_id: uuid.UUID | None = None
    number: str | None = None
    issue_date: date
    due_date: date
    settlement_date: date | None = None
    status: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    amount_paid: Decimal
    currency: str
    fx_rate: Decimal
    notes: str | None = None
    payment_terms: str | None = None
    journal_entry_id: uuid.UUID | None = None
    void_journal_entry_id: uuid.UUID | None = None
    posted_at: datetime | None = None
    posted_by: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    stripe_payment_link: str | None = None
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
    tracking_vehicle_id: str | None = None


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
    tracking_vehicle_id: str | None = Field(default=None, max_length=64)


class BillBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    contact_id: uuid.UUID | None = None
    issue_date: date
    due_date: date
    notes: str | None = None
    supplier_reference: str | None = None
    currency: str = Field(default="AUD", min_length=3, max_length=3)
    fx_rate: Decimal = Field(default=Decimal("1"), gt=Decimal("0"))


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
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    fx_rate: Decimal | None = Field(default=None, gt=Decimal("0"))
    lines: list[BillLineCreate] | None = None


class BillOut(BaseModel):
    """Full bill response — includes nested lines, tenant_id, version."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    contact_id: uuid.UUID | None = None
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
    journal_entry_id: uuid.UUID | None = None
    void_journal_entry_id: uuid.UUID | None = None
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


# ---------------------------------------------------------------------------
# Credit Notes — Phase 1 tier-3 (cycle 10)
# ---------------------------------------------------------------------------


class CreditNoteLineOut(BaseModel):
    """One line of a credit note (nested in CreditNoteOut)."""

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


class CreditNoteLineCreate(BaseModel):
    """One line in a POST/PATCH payload."""

    description: str = Field(min_length=1)
    account_id: uuid.UUID
    tax_code_id: uuid.UUID | None = None
    quantity: Decimal = Decimal("1")
    unit_price: Decimal = Decimal("0")
    discount_pct: Decimal = Decimal("0")


class CreditNoteBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    contact_id: uuid.UUID
    issue_date: date
    reason: str | None = None
    notes: str | None = None
    original_invoice_id: uuid.UUID | None = None


class CreditNoteCreate(CreditNoteBase):
    """POST body."""

    lines: list[CreditNoteLineCreate] = Field(default_factory=list)


class CreditNoteUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    contact_id: uuid.UUID | None = None
    issue_date: date | None = None
    reason: str | None = None
    notes: str | None = None
    original_invoice_id: uuid.UUID | None = None
    lines: list[CreditNoteLineCreate] | None = None


class CreditNoteOut(BaseModel):
    """Full credit note response — includes nested lines, tenant_id, version."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    contact_id: uuid.UUID
    number: str | None = None
    issue_date: date
    status: str
    original_invoice_id: uuid.UUID | None = None
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    amount_allocated: Decimal
    reason: str | None = None
    notes: str | None = None
    posted_at: datetime | None = None
    posted_by: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    lines: list[CreditNoteLineOut] = Field(default_factory=list)


class CreditNoteListOut(BaseModel):
    items: list[CreditNoteOut]
    total: int
    limit: int
    offset: int


class CreditNoteConflictBody(BaseModel):
    detail: str
    current: CreditNoteOut


# ---------------------------------------------------------------------------
# Bank Accounts — Phase 1 tier-4
#
# View over accounts where bsb IS NOT NULL.  Exposes BSB, account number,
# account title, APCA user ID, and bank abbreviation (ABA fields).
# ---------------------------------------------------------------------------


class BankAccountCreate(BaseModel):
    """POST body for creating a new bank-side account (bank/card/loan/cash)."""

    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    account_kind: str = Field(
        default="BANK_CHECKING",
        description="One of BANK_CHECKING / BANK_SAVINGS / CREDIT_CARD / BANK_LOAN / CASH / OTHER",
    )
    bsb: str | None = Field(default=None, min_length=6, max_length=7,
        description="BSB formatted 'xxx-xxx' — required for BANK_* kinds, ignored otherwise")
    bank_account_number: str | None = Field(default=None, max_length=32)
    bank_account_title: str | None = Field(default=None, max_length=32)
    apca_user_id: str | None = Field(default=None, max_length=6)
    bank_abbreviation: str | None = Field(default=None, max_length=3)
    is_trust_account: bool = False


class BankAccountUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    code: str | None = Field(default=None, min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    account_kind: str | None = Field(default=None,
        description="Re-classify between BANK_*/CREDIT_CARD/BANK_LOAN/CASH/OTHER")
    bsb: str | None = Field(default=None, min_length=6, max_length=7)
    bank_account_number: str | None = None
    bank_account_title: str | None = None
    apca_user_id: str | None = None
    bank_abbreviation: str | None = None
    is_trust_account: bool | None = None


class BankAccountOut(BaseModel):
    """Full bank account response."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    code: str
    name: str
    account_kind: str | None = None
    account_type: str | None = None
    bsb: str | None = None
    bank_account_number: str | None = None
    bank_account_title: str | None = None
    apca_user_id: str | None = None
    bank_abbreviation: str | None = None
    is_trust_account: bool = False
    version: int
    created_at: datetime
    archived_at: datetime | None = None
    # Populated by the list handler only when ?include_balance=true /
    # ?include_statement_balance=true is set. ``balance`` is the GL
    # balance (POSTED journal lines, debit − credit) as-of today;
    # ``statement_balance`` is the cumulative SUM(amount) of all
    # non-archived bank_statement_lines on this account — the latest
    # running balance the bsl view would show.
    balance: Decimal | None = None
    statement_balance: Decimal | None = None


class BankAccountListOut(BaseModel):
    items: list[BankAccountOut]
    total: int
    limit: int
    offset: int


class BankAccountConflictBody(BaseModel):
    detail: str
    current: BankAccountOut


# ---------------------------------------------------------------------------
# Bank Statement Lines — Phase 1 tier-4 (cycle 12)
#
# Individual transaction lines imported from bank statements.
# Each line belongs to a bank account (accounts.bsb IS NOT NULL).
# amount: positive = deposit/inflow, negative = withdrawal/outflow.
# ---------------------------------------------------------------------------


class BankStatementLineCreate(BaseModel):
    """POST body for creating a new bank statement line."""

    account_id: uuid.UUID
    txn_date: date
    amount: Decimal
    description: str | None = None
    balance: Decimal | None = None
    reference: str | None = Field(default=None, max_length=128)
    status: str = Field(default="UNMATCHED")
    external_id: str | None = Field(default=None, max_length=255)
    bank_feed_account_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None


class BankStatementLineUpdate(BaseModel):
    """PATCH body — every field optional. Primarily for reconciliation."""

    model_config = ConfigDict(from_attributes=True)

    description: str | None = None
    reference: str | None = Field(default=None, max_length=128)
    status: str | None = None
    matched_entry_id: uuid.UUID | None = None
    matched_at: datetime | None = None
    matched_by: str | None = None
    contact_id: uuid.UUID | None = None
    balance: Decimal | None = None


class BankStatementLineOut(BaseModel):
    """Full bank statement line response."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    txn_date: date
    description: str | None = None
    amount: Decimal
    balance: Decimal | None = None
    reference: str | None = None
    status: str
    matched_entry_id: uuid.UUID | None = None
    matched_at: datetime | None = None
    matched_by: str | None = None
    matched_to_type: str | None = None
    matched_to_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    bank_rule_id: uuid.UUID | None = None
    bank_feed_account_id: uuid.UUID | None = None
    external_id: str | None = None
    version: int
    created_at: datetime
    archived_at: datetime | None = None


class BankStatementLineListOut(BaseModel):
    items: list[BankStatementLineOut]
    total: int
    limit: int
    offset: int


class BankStatementLineConflictBody(BaseModel):
    detail: str
    current: BankStatementLineOut


class BankStatementLineMatchRequest(BaseModel):
    """POST body for /bank_statement_lines/{id}/match."""

    matched_to_type: str = Field(
        ..., description="PAYMENT or JOURNAL_ENTRY"
    )
    matched_to_id: uuid.UUID = Field(
        ..., description="UUID of the matching payment or journal entry"
    )


class SplitAllocation(BaseModel):
    """One GL allocation row for a split-match journal entry."""

    account_id: uuid.UUID
    debit: Decimal = Field(default=Decimal("0"), ge=0)
    credit: Decimal = Field(default=Decimal("0"), ge=0)
    description: str | None = None
    tax_code_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _not_both_zero(self) -> "SplitAllocation":
        if self.debit == 0 and self.credit == 0:
            raise ValueError("Each allocation must have a non-zero debit or credit")
        return self


class BankStatementLineSplitMatchRequest(BaseModel):
    """POST body for /bank_statement_lines/{id}/split_match.

    ``allocations`` are the non-bank-account sides of the journal entry.
    The bank account side is auto-generated from the BSL amount.

    Validation: sum(credit) - sum(debit) across allocations must equal the
    BSL amount (positive for deposits, negative for withdrawals).
    """

    allocations: list[SplitAllocation] = Field(min_length=1)
    entry_date: date | None = None
    description: str | None = None


# ---------------------------------------------------------------------------
# Projects — Phase 1 tier-4 (cycle 13)
#
# Flat job/cost-centre entities. Attached to transaction lines for
# job costing and project-level P&L reporting.
# ---------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    """POST body for creating a new project."""

    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    status: str = Field(default="ACTIVE")
    start_date: date | None = None
    end_date: date | None = None
    notes: str | None = None
    extra: dict | None = None


class ProjectUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    code: str | None = Field(default=None, min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    status: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    notes: str | None = None
    extra: dict | None = None


class ProjectOut(BaseModel):
    """Full project response."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    code: str
    name: str
    status: str
    start_date: date | None = None
    end_date: date | None = None
    notes: str | None = None
    extra: dict | None = None
    version: int
    created_at: datetime
    archived_at: datetime | None = None


class ProjectListOut(BaseModel):
    items: list[ProjectOut]
    total: int
    limit: int
    offset: int


class ProjectConflictBody(BaseModel):
    detail: str
    current: ProjectOut


# ---------------------------------------------------------------------------
# Fixed Assets — Phase 1 tier-4 (cycle 14)
#
# Capitalised items with a depreciation schedule. Linked to a
# ``depreciation_model`` (book method) and three GL accounts
# (cost / accumulated depreciation / depreciation expense).
# Status: active | disposed | archived (lowercase, matching DB default).
# ---------------------------------------------------------------------------


class FixedAssetCreate(BaseModel):
    """POST body for creating a new fixed asset."""

    name: str = Field(min_length=1, max_length=255)
    depreciation_model_id: str = Field(min_length=1, max_length=64)
    cost_account_id: uuid.UUID
    accum_dep_account_id: uuid.UUID
    dep_expense_account_id: uuid.UUID
    purchase_date: date
    cost: Decimal
    in_service_date: date | None = None
    residual_value: Decimal = Decimal("0")
    code: str | None = Field(default=None, min_length=1, max_length=32)
    description: str | None = None
    tax_model_id: str | None = Field(default=None, max_length=64)
    serial_number: str | None = None
    manufacturer: str | None = None
    model_number: str | None = None
    location: str | None = None
    custody_person: str | None = None
    warranty_end: date | None = None
    extra: dict | None = None


class FixedAssetUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    depreciation_model_id: str | None = Field(default=None, min_length=1, max_length=64)
    tax_model_id: str | None = Field(default=None, max_length=64)
    purchase_date: date | None = None
    in_service_date: date | None = None
    residual_value: Decimal | None = None
    serial_number: str | None = None
    manufacturer: str | None = None
    model_number: str | None = None
    location: str | None = None
    custody_person: str | None = None
    warranty_end: date | None = None
    extra: dict | None = None


class DepreciationModelOut(BaseModel):
    """Full depreciation model response — used both embedded in FixedAssetOut
    and as the list-endpoint item schema for ``/api/v1/depreciation_models``."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    method: str
    method_number: int
    method_period: int
    method_progress_factor: Decimal | None = None
    rate_pct: Decimal | None = None
    created_at: datetime


class DepreciationModelListOut(BaseModel):
    items: list[DepreciationModelOut]
    total: int
    limit: int
    offset: int


class FixedAssetOut(BaseModel):
    """Full fixed asset response — includes depreciation_model name for UX."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    code: str
    name: str
    description: str | None = None
    status: str
    depreciation_model_id: str
    depreciation_model: DepreciationModelOut | None = None
    tax_model_id: str | None = None
    cost_account_id: uuid.UUID
    accum_dep_account_id: uuid.UUID
    dep_expense_account_id: uuid.UUID
    purchase_date: date
    in_service_date: date
    cost: Decimal
    residual_value: Decimal
    last_depreciation_posted_through: date | None = None
    disposal_date: date | None = None
    disposal_proceeds: Decimal | None = None
    disposal_journal_id: uuid.UUID | None = None
    serial_number: str | None = None
    manufacturer: str | None = None
    model_number: str | None = None
    location: str | None = None
    custody_person: str | None = None
    warranty_end: date | None = None
    extra: dict | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class FixedAssetListOut(BaseModel):
    items: list[FixedAssetOut]
    total: int
    limit: int
    offset: int


class FixedAssetConflictBody(BaseModel):
    detail: str
    current: FixedAssetOut


# ---------------------------------------------------------------------------
# Recurring Invoices — ``/api/v1/recurring_invoices``
# Templates that carry a schedule + line items. Invoice spawning is out of scope
# here — this is CRUD + list only.
# Status enum: ACTIVE | PAUSED | ENDED (model values)
# Frequency enum: WEEKLY | FORTNIGHTLY | MONTHLY | QUARTERLY | YEARLY
# Archive is terminal (archived_at set); lifecycle transitions via PATCH status.
# ---------------------------------------------------------------------------


class RecurringInvoiceLineCreate(BaseModel):
    """One line in a recurring invoice template (create / replace)."""

    description: str = Field(min_length=1)
    account_id: uuid.UUID
    tax_code_id: uuid.UUID | None = None
    quantity: Decimal = Decimal("1")
    unit_price: Decimal = Decimal("0")
    discount_pct: Decimal = Decimal("0")


class RecurringInvoiceCreate(BaseModel):
    """POST body for creating a recurring invoice template."""

    name: str = Field(min_length=1, max_length=128)
    contact_id: uuid.UUID
    frequency: RecurrenceFrequency
    next_run: date
    status: RecurrenceStatus = RecurrenceStatus.ACTIVE
    anchor_day: int | None = None
    end_date: date | None = None
    due_days: int = 30
    payment_terms: str | None = None
    notes: str | None = None
    auto_post: bool = False
    lines: list[RecurringInvoiceLineCreate] = Field(default_factory=list)


class RecurringInvoiceUpdate(BaseModel):
    """PATCH body — every field optional.

    If ``lines`` is present, existing lines are replaced in full.
    If ``lines`` is absent, existing lines are left untouched.
    """

    model_config = ConfigDict(from_attributes=True)

    name: str | None = Field(default=None, min_length=1, max_length=128)
    contact_id: uuid.UUID | None = None
    frequency: RecurrenceFrequency | None = None
    next_run: date | None = None
    status: RecurrenceStatus | None = None
    anchor_day: int | None = None
    end_date: date | None = None
    due_days: int | None = None
    payment_terms: str | None = None
    notes: str | None = None
    auto_post: bool | None = None
    lines: list[RecurringInvoiceLineCreate] | None = None


class RecurringInvoiceLineOut(BaseModel):
    """Line item in a recurring invoice template response."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_no: int
    description: str
    account_id: uuid.UUID
    tax_code_id: uuid.UUID | None = None
    quantity: Decimal
    unit_price: Decimal
    discount_pct: Decimal


class RecurringInvoiceOut(BaseModel):
    """Full recurring invoice template response — lines nested."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    contact_id: uuid.UUID
    name: str
    frequency: RecurrenceFrequency
    status: RecurrenceStatus
    anchor_day: int | None = None
    next_run: date
    end_date: date | None = None
    last_run: date | None = None
    due_days: int
    payment_terms: str | None = None
    notes: str | None = None
    auto_post: bool
    invoices_generated: int
    version: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    lines: list[RecurringInvoiceLineOut] = Field(default_factory=list)


class RecurringInvoiceListOut(BaseModel):
    items: list[RecurringInvoiceOut]
    total: int
    limit: int
    offset: int


class RecurringInvoiceConflictBody(BaseModel):
    detail: str
    current: RecurringInvoiceOut


class RecurringInvoiceGenerateResponse(BaseModel):
    """Response body for POST /{id}/generate — the newly created invoice."""

    invoice_id: uuid.UUID
    invoice: InvoiceOut


# ---------------------------------------------------------------------------
# Budgets — Phase 1 tier-4 (cycle 16)
#
# Budget rows are flat monthly-amount-per-account entries.
# No line items — the row IS the line: (company, account, year, month) → amount.
# Status lifecycle: rows are soft-archived via archived_at (DELETE /id).
# Unique key: (company_id, account_id, year, month).
# ---------------------------------------------------------------------------


class BudgetCreate(BaseModel):
    """POST body for creating a budget row."""

    account_id: uuid.UUID
    year: int = Field(ge=1900, le=9999)
    month: int = Field(ge=1, le=12)
    amount: Decimal = Decimal("0")
    notes: str | None = None


class BudgetUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    account_id: uuid.UUID | None = None
    year: int | None = Field(default=None, ge=1900, le=9999)
    month: int | None = Field(default=None, ge=1, le=12)
    amount: Decimal | None = None
    notes: str | None = None


class BudgetOut(BaseModel):
    """Full budget row response."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    year: int
    month: int
    amount: Decimal
    notes: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class BudgetListOut(BaseModel):
    items: list[BudgetOut]
    total: int
    limit: int
    offset: int


class BudgetConflictBody(BaseModel):
    detail: str
    current: BudgetOut


# ---------------------------------------------------------------------------
# Reports — Tier 5 (cycle 18): Aged Receivables + Aged Payables
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Reports — Tier 5 (cycle 19): Profit & Loss + Balance Sheet
# ---------------------------------------------------------------------------


class PnLAccountLine(BaseModel):
    """One account's net amount in a P&L section."""

    account_id: uuid.UUID
    account_name: str
    code: str
    amount: float


class PnLIncome(BaseModel):
    """Income section of a P&L report."""

    INCOME: list[PnLAccountLine] = Field(default_factory=list)
    OTHER_INCOME: list[PnLAccountLine] = Field(default_factory=list)
    total_income: float


class PnLExpenses(BaseModel):
    """Expenses section of a P&L report."""

    EXPENSE: list[PnLAccountLine] = Field(default_factory=list)
    COST_OF_SALES: list[PnLAccountLine] = Field(default_factory=list)
    OTHER_EXPENSE: list[PnLAccountLine] = Field(default_factory=list)
    total_expenses: float


class PnLReport(BaseModel):
    """Full profit & loss report for a date range."""

    from_date: date
    to_date: date
    income: PnLIncome
    expenses: PnLExpenses
    net_profit: float


class BSAccountLine(BaseModel):
    """One account's balance in a balance sheet section."""

    account_id: uuid.UUID
    account_name: str
    code: str
    balance: float


class BSAssets(BaseModel):
    """Assets section of a balance sheet."""

    ASSET: list[BSAccountLine] = Field(default_factory=list)
    total_assets: float


class BSLiabilities(BaseModel):
    """Liabilities section of a balance sheet."""

    LIABILITY: list[BSAccountLine] = Field(default_factory=list)
    total_liabilities: float


class BSEquity(BaseModel):
    """Equity section of a balance sheet."""

    EQUITY: list[BSAccountLine] = Field(default_factory=list)
    total_equity: float


class BSReport(BaseModel):
    """Full balance sheet as at a given date."""

    as_of_date: date
    assets: BSAssets
    liabilities: BSLiabilities
    equity: BSEquity
    balanced: bool
    difference: float


class AgedContact(BaseModel):
    """One contact's aged balance row.

    Keys in ``buckets`` are dynamic strings built from the caller's
    ``bucket_days`` parameter — e.g. "current", "1-30 days", "31-60 days".
    They are also surfaced as individual top-level fields for convenience
    (JSON clients that want simple key access).  ``extra="allow"`` lets
    Pydantic pass the dynamic bucket keys through without a fixed schema.
    """

    model_config = ConfigDict(extra="allow")

    contact_id: uuid.UUID
    contact_name: str
    total: Decimal


class AgedReport(BaseModel):
    """Full aged receivables / payables report.

    ``buckets`` is the ordered list of bucket label strings.
    ``contacts`` is one row per contact (each row has keys matching
    ``buckets`` plus ``contact_id``, ``contact_name``, ``total``).
    ``totals`` is the grand-total row (Trade Debtors only — excludes
    retentions) with the same bucket keys plus ``total``.
    ``retentions_receivable`` is present on AR reports only; it carries
    the cumulative Retentions Receivable balance split by aging bucket,
    matching the same bucket keys as ``totals``.  None when there are no
    outstanding retentions (or for AP reports where retentions don't apply).
    """

    as_of_date: date
    buckets: list[str]
    contacts: list[dict]
    totals: dict
    retentions_receivable: dict | None = None


# ---------------------------------------------------------------------------
# BAS Summary — tier-5 (cycle 20)
# ---------------------------------------------------------------------------


class BASSummary(BaseModel):
    """Australian Business Activity Statement summary for a date range.

    BAS labels follow ATO nomenclature:
      G1  — Total taxable sales (inc. GST)
      G2  — Export sales (always 0 in v1 — no export tracking)
      G3  — Other GST-free sales
      G10 — Capital acquisitions (always 0 in v1 — no capital tracking)
      G11 — Other (non-capital) acquisitions (taxable expenses)
      1A  — GST collected on sales (G1 × 10%)
      1B  — GST credits on purchases (G11 × 1/11, i.e. tax-inclusive component)

    When registration_effective_date is set (mid-quarter GST registration),
    G1 is split into pre- and post-registration totals. Only g1_post_registration
    feeds into 1A; g1_total_sales is the sum of both for ATO disclosure.
    """

    from_date: date
    to_date: date
    g1_total_sales: float
    g2_export_sales: float
    g3_other_gst_free_sales: float
    g10_capital_acquisitions: float
    g11_other_acquisitions: float
    label_1a_gst_on_sales: float
    label_1b_gst_on_purchases: float
    net_gst: float
    remit_or_refund: str  # "REMIT" | "REFUND"
    # Mid-quarter GST registration split (None when not applicable)
    registration_effective_date: date | None = None
    g1_pre_registration: float = 0.0
    g1_post_registration: float = 0.0


# ---------------------------------------------------------------------------
# Cashflow Statement (indirect method) — tier-5 (cycle 20)
# ---------------------------------------------------------------------------


class CashflowOperating(BaseModel):
    net_profit: float
    adjustments: list = Field(default_factory=list)
    total_operating: float


class CashflowInvesting(BaseModel):
    asset_purchases: float
    asset_disposals: float
    total_investing: float


class CashflowFinancing(BaseModel):
    loan_proceeds: float
    loan_repayments: float
    total_financing: float


class CashflowStatement(BaseModel):
    """Indirect-method cashflow statement for a date range."""

    from_date: date
    to_date: date
    operating: CashflowOperating
    investing: CashflowInvesting
    financing: CashflowFinancing
    net_change: float
    opening_cash: float
    closing_cash: float


# ---------------------------------------------------------------------------
# Depreciation Schedule Report — tier-5 (cycle 21)
# ---------------------------------------------------------------------------


class DepreciationAssetLine(BaseModel):
    """One asset row in the depreciation schedule report."""

    asset_id: uuid.UUID
    asset_number: str
    description: str | None
    acquisition_date: date
    cost: float
    residual_value: float
    useful_life_months: int
    depreciation_method: str
    accumulated_depreciation: float
    current_book_value: float
    next_month_depreciation: float
    fully_depreciated: bool


class DepreciationSchedule(BaseModel):
    """Depreciation schedule as at a given date.

    ``assets`` is sorted by asset_number.  ``method`` query param
    filters to ``linear`` or ``diminishing_value`` (DB method strings).
    The convenience aliases ``STRAIGHT_LINE`` and ``DECLINING_BALANCE``
    are also accepted and mapped internally.
    """

    as_of_date: date
    assets: list[DepreciationAssetLine]
    total_cost: float
    total_accumulated: float
    total_book_value: float


# ---------------------------------------------------------------------------
# FX Revaluation Report — tier-5 (cycle 25)
# ---------------------------------------------------------------------------


class FXRevaluationItem(BaseModel):
    """One foreign-currency document line in the FX revaluation report."""

    entity_type: str  # "INVOICE" or "BILL"
    entity_id: uuid.UUID
    entity_ref: str | None  # document number if minted, else None
    contact_name: str
    currency: str
    original_amount: float
    amount_paid: float
    outstanding_foreign: float
    outstanding_base: float | None  # None when FX rate unavailable
    note: str


class FXRevaluationReport(BaseModel):
    """FX revaluation report as at a given date."""

    as_of_date: date
    base_currency: str
    items: list[FXRevaluationItem]
    total_items: int
    note: str


# ---------------------------------------------------------------------------
# Fixed Asset Disposal — tier-4 (cycle 21)
# ---------------------------------------------------------------------------


class FixedAssetDispose(BaseModel):
    """POST body for disposing a fixed asset."""

    disposal_date: date
    proceeds: Decimal
    notes: str | None = None


class FixedAssetDepreciationRunRequest(BaseModel):
    """POST body for running a depreciation posting on a fixed asset."""

    through: date


class FixedAssetDepreciationRunResponse(BaseModel):
    """Response body for POST /{id}/post_depreciation."""

    asset: FixedAssetOut
    amount_posted: Decimal
    note: str


class DepreciationRunAllRequest(BaseModel):
    """POST body for POST /depreciation_run_all — batch run across all active assets."""

    through: date


class DepreciationRunAllResultItem(BaseModel):
    """One asset's result row in a batch depreciation run."""

    asset_id: uuid.UUID
    asset_code: str
    amount_posted: Decimal
    note: str  # e.g. "Posted AUD 500.00" or "No depreciation to post"


class DepreciationRunAllResponse(BaseModel):
    """Response body for POST /depreciation_run_all."""

    through: date
    total_assets: int
    total_amount: Decimal
    results: list[DepreciationRunAllResultItem]
    errors: list[str]  # any assets that failed (asset code + error message)


# ---------------------------------------------------------------------------
# Fixed Asset Convert-to-Inventory
# ---------------------------------------------------------------------------


class FixedAssetConvertToInventory(BaseModel):
    """POST body for converting an active FA demonstrator to used-inventory stock.

    The conversion journal debits the inventory account at NBV, debits the
    accumulated-depreciation account (clearing this asset's share), and credits
    the FA cost account (clearing the full original cost). The asset is then
    stamped disposed at NBV proceeds and an inventory Item is created with
    on_hand_qty=1, wac_cost=NBV.

    ``sku`` defaults to the asset's FA code when not supplied.
    ``vin`` is stored as the item's description for used-vehicle tracking.
    """

    conversion_date: date
    inventory_account_id: uuid.UUID
    cogs_account_id: uuid.UUID
    income_account_id: uuid.UUID
    sku: str | None = None
    vin: str | None = None


class FixedAssetConvertToInventoryResponse(BaseModel):
    """Response from POST /{id}/convert_to_inventory."""

    asset: FixedAssetOut
    item_id: uuid.UUID
    item_sku: str
    nbv: Decimal
    journal_id: uuid.UUID


# ---------------------------------------------------------------------------
# Trial Balance — tier-5 (cycle 27)
# ---------------------------------------------------------------------------


class TrialBalanceLine(BaseModel):
    """One account row in the trial balance."""

    account_id: uuid.UUID
    code: str
    name: str
    account_type: AccountType
    debit_total: float
    credit_total: float
    balance: float


class TrialBalanceReport(BaseModel):
    """Full trial balance as at a given date."""

    as_of_date: date
    accounts: list[TrialBalanceLine]
    total_debits: float
    total_credits: float
    balanced: bool


# ---------------------------------------------------------------------------
# Budget vs Actual — tier-5 (cycle 27)
# ---------------------------------------------------------------------------


class BudgetVsActualLine(BaseModel):
    """One account row in the budget vs actual report."""

    account_id: uuid.UUID
    account_code: str
    account_name: str
    budget: float
    actual: float
    variance: float
    variance_pct: float | None  # None when budget is zero


class BudgetVsActualReport(BaseModel):
    """Budget vs actual report for a year (optionally a single month)."""

    year: int
    month: int | None  # None means full year
    lines: list[BudgetVsActualLine]
    total_budget: float
    total_actual: float
    total_variance: float


# ---------------------------------------------------------------------------
# P&L by Segment — tier-5 (cycle 27)
# ---------------------------------------------------------------------------


class PLSegmentAccountLine(BaseModel):
    """One account line within a segment section."""

    account_id: uuid.UUID
    code: str
    name: str
    amount: float  # natural-sign positive (income=credit-debit, expense=debit-credit)


class PLSegmentSection(BaseModel):
    """One account-type grouping within a segment."""

    account_type: str
    lines: list[PLSegmentAccountLine]
    total: float


class PLSegmentRow(BaseModel):
    """One project segment's P&L."""

    segment_id: uuid.UUID | None
    segment_label: str
    sections: list[PLSegmentSection]
    net_profit: float


class PLBySegmentReport(BaseModel):
    """P&L by segment (project) for a date range."""

    from_date: date
    to_date: date
    segment_type: str
    segments: list[PLSegmentRow]


# ---------------------------------------------------------------------------
# Revenue by Customer
# ---------------------------------------------------------------------------


class RevenueByCustomerRow(BaseModel):
    """One customer's invoiced revenue for the period."""

    contact_id: uuid.UUID
    contact_name: str
    revenue: float
    pct_of_total: float


class RevenueByCustomerReport(BaseModel):
    """Revenue breakdown by customer with PSI concentration metrics."""

    from_date: date
    to_date: date
    rows: list[RevenueByCustomerRow]
    total_revenue: float
    top_customer_pct: float | None
    concentration_warning: bool


# ---------------------------------------------------------------------------
# Journal Templates — cycle 40
# ---------------------------------------------------------------------------


class JournalTemplateLineCreate(BaseModel):
    """One line in a journal template (create / replace)."""

    account_id: uuid.UUID
    description: str | None = None
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    tax_code_id: uuid.UUID | None = None


class JournalTemplateCreate(BaseModel):
    """POST body for creating a journal template."""

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    lines: list[JournalTemplateLineCreate] = Field(default_factory=list)


class JournalTemplateUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    lines: list[JournalTemplateLineCreate] | None = None


class JournalTemplateLineOut(BaseModel):
    """One line in a journal template response."""

    model_config = ConfigDict(from_attributes=True)

    account_id: uuid.UUID
    description: str | None = None
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    tax_code_id: uuid.UUID | None = None


class JournalTemplateOut(BaseModel):
    """Full journal template response."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    name: str
    description: str | None = None
    lines: list[dict] = Field(default_factory=list)
    created_at: datetime
    archived_at: datetime | None = None


class JournalTemplateListOut(BaseModel):
    items: list[JournalTemplateOut]
    total: int
    limit: int
    offset: int


class JournalTemplateApplyOut(BaseModel):
    """Response body for POST /journal_templates/{id}/apply.

    Returns the suggested pre-filled lines; the caller POSTs these
    to /api/v1/journal_entries to create the actual entry.
    """

    template_id: uuid.UUID
    template_name: str
    suggested_lines: list[JournalTemplateLineOut]


# ---------------------------------------------------------------------------
# Account Ranges — cycle 40
# ---------------------------------------------------------------------------


class AccountRangeCreate(BaseModel):
    """POST body for creating an account range."""

    prefix: str = Field(min_length=1, max_length=16)
    label: str = Field(min_length=1, max_length=255)
    account_types: list[str]
    sort_order: int = 0


class AccountRangeUpdate(BaseModel):
    """PATCH body — prefix is immutable; every other field optional."""

    model_config = ConfigDict(from_attributes=True)

    label: str | None = Field(default=None, min_length=1, max_length=255)
    account_types: list[str] | None = None
    sort_order: int | None = None


class AccountRangeOut(BaseModel):
    """Full account range response."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    prefix: str
    label: str
    account_types: list[str]
    sort_order: int
    created_at: datetime


class AccountRangeListOut(BaseModel):
    items: list[AccountRangeOut]
    total: int


class PrefixModeOut(BaseModel):
    """Response body for GET /account_ranges/prefix_mode."""

    mode: str


class PrefixModeUpdate(BaseModel):
    """PATCH body for /account_ranges/prefix_mode."""

    mode: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Search — global across contacts/invoices/bills/accounts (cycle 36)
# ---------------------------------------------------------------------------


class SearchHitOut(BaseModel):
    """One search result row."""

    id: uuid.UUID
    kind: str  # "contact", "invoice", "bill", "account"
    title: str
    subtitle: str | None
    url: str


class SearchResponse(BaseModel):
    """Response body for GET /api/v1/search."""

    query: str
    hits: list[SearchHitOut]
    total: int


# ---------------------------------------------------------------------------
# Bank Rules — cycle 41
# ---------------------------------------------------------------------------


class BankRuleCreate(BaseModel):
    """POST body for creating a bank rule."""

    name: str = Field(min_length=1, max_length=255)
    match_pattern: str = Field(min_length=1)
    match_type: str = "CONTAINS"
    account_id: uuid.UUID
    tax_code: str | None = None
    contact_id: uuid.UUID | None = None
    description_template: str | None = None
    auto_create: bool = False
    priority: int = 0
    is_active: bool = True


class BankRuleUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    name: str | None = Field(default=None, min_length=1, max_length=255)
    match_pattern: str | None = None
    match_type: str | None = None
    account_id: uuid.UUID | None = None
    tax_code: str | None = None
    contact_id: uuid.UUID | None = None
    description_template: str | None = None
    auto_create: bool | None = None
    priority: int | None = None
    is_active: bool | None = None


class BankRuleOut(BaseModel):
    """Full bank rule response."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    name: str
    match_pattern: str
    match_type: str
    account_id: uuid.UUID
    tax_code: str | None = None
    contact_id: uuid.UUID | None = None
    description_template: str | None = None
    auto_create: bool
    priority: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class BankRuleListOut(BaseModel):
    items: list[BankRuleOut]
    total: int
    limit: int
    offset: int


class BankRuleApplyOut(BaseModel):
    """Response body for POST /bank_rules/apply and POST /bank_rules/{id}/apply."""

    applied: int


class YTDTurnoverReport(BaseModel):
    """YTD gross turnover and GST registration threshold status.

    fy_start / fy_end are the Australian financial-year bounds used
    (1 July - 30 June).  ytd_turnover is the sum of all INCOME and
    OTHER_INCOME journal credits (net of debits) for posted JEs in that
    window.  threshold is always 75000.00 (ATO GST registration limit
    for for-profit entities).  threshold_crossed is true when
    ytd_turnover >= threshold.  threshold_approaching is true when
    ytd_turnover >= 80% of threshold but < threshold (amber warning zone).
    """

    fy_start: date
    fy_end: date
    ytd_turnover: float
    threshold: float
    threshold_crossed: bool
    threshold_approaching: bool


# ---------------------------------------------------------------------------
# Allocation rules
# ---------------------------------------------------------------------------


class AllocationTarget(BaseModel):
    """One target entry in an allocation rule."""

    account_id: uuid.UUID
    label: str = Field(default="", max_length=200)
    percentage: Decimal = Field(gt=Decimal("0"), le=Decimal("100"))


class AllocationRuleCreate(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    source_account_id: uuid.UUID
    targets: list[AllocationTarget] = Field(min_length=1)
    is_active: bool = True

    @model_validator(mode="after")
    def targets_sum_100(self) -> "AllocationRuleCreate":
        total = sum(t.percentage for t in self.targets)
        if abs(total - Decimal("100")) > Decimal("0.01"):
            raise ValueError(
                f"Target percentages must sum to 100 (got {total})"
            )
        return self


class AllocationRuleUpdate(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    source_account_id: uuid.UUID | None = None
    targets: list[AllocationTarget] | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def targets_sum_100(self) -> "AllocationRuleUpdate":
        if self.targets is not None:
            total = sum(t.percentage for t in self.targets)
            if abs(total - Decimal("100")) > Decimal("0.01"):
                raise ValueError(
                    f"Target percentages must sum to 100 (got {total})"
                )
        return self


class AllocationRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str | None
    source_account_id: uuid.UUID
    targets: list[dict]
    is_active: bool
    version: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class AllocationRuleListOut(BaseModel):
    items: list[AllocationRuleOut]
    total: int
    limit: int
    offset: int


class AllocationRuleConflictBody(BaseModel):
    current: AllocationRuleOut
    message: str = "Version conflict — record was modified by another request"


class AllocationApplyIn(BaseModel):
    """Request body for POST /api/v1/allocation_rules/{id}/apply."""

    entry_date: date
    amount: Decimal = Field(gt=Decimal("0"))
    description: str | None = None


class AllocationApplyOut(BaseModel):
    """Response from apply — the generated journal entry id."""

    journal_entry_id: uuid.UUID
    lines_count: int
    total_amount: Decimal


# ---------------------------------------------------------------------------
# Pay Runs
# ---------------------------------------------------------------------------


class PayRunLineCreate(BaseModel):
    employee_id: uuid.UUID
    gross: Decimal = Field(gt=Decimal("0"))
    tax: Decimal = Field(ge=Decimal("0"))
    super_amount: Decimal = Field(ge=Decimal("0"))
    net: Decimal = Field(gt=Decimal("0"))


class PayRunLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    pay_run_id: uuid.UUID
    employee_id: uuid.UUID
    gross: Decimal
    tax: Decimal
    super_amount: Decimal
    net: Decimal
    created_at: datetime


class PayRunCreate(BaseModel):
    period_start: date
    period_end: date
    payment_date: date
    description: str | None = None

    @model_validator(mode="after")
    def period_end_after_start(self) -> "PayRunCreate":
        if self.period_end < self.period_start:
            raise ValueError("period_end must be >= period_start")
        return self


class PayRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    period_start: date
    period_end: date
    payment_date: date
    description: str | None
    status: str
    journal_id: uuid.UUID | None
    version: int
    created_at: datetime
    updated_at: datetime
    lines: list[PayRunLineOut] = []


class PayRunListOut(BaseModel):
    items: list[PayRunOut]
    total: int
    limit: int
    offset: int


class PayRunConflictBody(BaseModel):
    detail: str
    current: PayRunOut


class ExportAbaOut(BaseModel):
    aba_file_b64: str
    journal_id: uuid.UUID


# ---------------------------------------------------------------------------
# Purchase orders — commitment document (no GL impact)
# ---------------------------------------------------------------------------


class PurchaseOrderLineOut(BaseModel):
    """One line of a PO. ``received_qty`` reflects what's already been
    rolled into a converted bill — the unbilled remainder is
    ``quantity - received_qty``."""

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
    received_qty: Decimal
    project_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None


class PurchaseOrderLineCreate(BaseModel):
    """One line in a POST/PATCH payload.

    ``received_qty`` is optional and defaults to 0 on a fresh line. On
    PATCH against an OPEN/PARTIAL PO, callers MUST round-trip the
    ``received_qty`` returned by the GET — see service docstring.
    """

    description: str = Field(min_length=1)
    account_id: uuid.UUID
    tax_code_id: uuid.UUID | None = None
    quantity: Decimal = Decimal("1")
    unit_price: Decimal = Decimal("0")
    discount_pct: Decimal = Decimal("0")
    received_qty: Decimal = Decimal("0")
    project_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None


class PurchaseOrderBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    contact_id: uuid.UUID
    issue_date: date
    expected_date: date | None = None
    delivery_address: str | None = None
    notes: str | None = None
    currency: str = Field(default="AUD", min_length=3, max_length=3)
    fx_rate: Decimal = Field(default=Decimal("1"), gt=Decimal("0"))


class PurchaseOrderCreate(PurchaseOrderBase):
    """POST body — DRAFT is implied; lines optional."""

    lines: list[PurchaseOrderLineCreate] = Field(default_factory=list)


class PurchaseOrderUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    contact_id: uuid.UUID | None = None
    issue_date: date | None = None
    expected_date: date | None = None
    delivery_address: str | None = None
    notes: str | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    fx_rate: Decimal | None = Field(default=None, gt=Decimal("0"))
    lines: list[PurchaseOrderLineCreate] | None = None


class PurchaseOrderOut(BaseModel):
    """Full PO response — nested lines, tenant_id, version."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    contact_id: uuid.UUID
    number: str | None = None
    issue_date: date
    expected_date: date | None = None
    status: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    currency: str
    fx_rate: Decimal
    delivery_address: str | None = None
    notes: str | None = None
    sent_at: datetime | None = None
    closed_at: datetime | None = None
    cancelled_at: datetime | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    lines: list[PurchaseOrderLineOut] = Field(default_factory=list)


class PurchaseOrderListOut(BaseModel):
    items: list[PurchaseOrderOut]
    total: int
    limit: int
    offset: int


class PurchaseOrderConflictBody(BaseModel):
    detail: str
    current: PurchaseOrderOut


class PurchaseOrderConvertBody(BaseModel):
    """POST /{id}/convert-to-bill payload — all fields optional.

    ``quantities`` lets the caller bill less than the full unreceived
    remainder per line. Keys are ``line_no`` (1-indexed) values are the
    quantity to bill on that line. Lines omitted are skipped. If
    ``quantities`` is ``None`` the service bills everything outstanding.
    """

    quantities: dict[int, Decimal] | None = None
    bill_issue_date: date | None = None
    bill_due_date: date | None = None
    supplier_reference: str | None = None


class PurchaseOrderConvertOut(BaseModel):
    purchase_order: PurchaseOrderOut
    bill_id: uuid.UUID
    bill_number: str | None = None


# ---------------------------------------------------------------------------
# Proration (/api/v1/proration/*) — see saebooks/services/proration.py
# ---------------------------------------------------------------------------


class ProratePreviewBody(BaseModel):
    """Generic per-line/date-range prorate (Prorate flow #3)."""

    full_period_amount: Decimal = Field(
        ...,
        description="Full-period amount (e.g. monthly rent, annual subscription).",
    )
    basis: str = Field(
        ...,
        description="DAILY / WEEKLY / MONTHLY / QUARTERLY / ANNUAL",
    )
    service_start: date
    service_end: date


class ProratePreviewOut(BaseModel):
    full_period_amount: Decimal
    basis: str
    service_start: date
    service_end: date
    days_used: int
    days_in_full: int
    factor: Decimal
    prorated_amount: Decimal


class FirstPeriodPreviewBody(BaseModel):
    """First-period recurring prorate (Prorate flow #1).

    Identical body to the generic preview but the response includes a
    suggested line-description string the caller can drop straight into
    the first invoice's line."""

    full_period_amount: Decimal
    basis: str
    service_start: date
    service_end: date


class FirstPeriodPreviewOut(BaseModel):
    full_period_amount: Decimal
    basis: str
    service_start: date
    service_end: date
    days_used: int
    days_in_full: int
    factor: Decimal
    prorated_amount: Decimal
    line_description_suggestion: str


class PlanChangePreviewBody(BaseModel):
    """Mid-period plan-change (Prorate flow #2)."""

    old_period_amount: Decimal = Field(
        ..., description="Old plan's full-period amount."
    )
    new_period_amount: Decimal = Field(
        ..., description="New plan's full-period amount."
    )
    period_start: date
    period_end: date
    change_date: date = Field(
        ..., description="Day the customer switches plans (inclusive of new plan)."
    )


class PlanChangePreviewOut(BaseModel):
    period_start: date
    period_end: date
    change_date: date
    days_total: int
    days_used: int
    days_remaining: int
    credit: Decimal
    charge: Decimal
    net: Decimal


class DeferredRevenuePreviewBody(BaseModel):
    """``period_date`` is any date inside the calendar month to recognise."""

    period_date: date


class DeferredRevenuePreviewLine(BaseModel):
    invoice_line_id: uuid.UUID
    invoice_number: str
    description: str
    income_account_id: uuid.UUID
    amount: Decimal


class DeferredRevenuePreviewOut(BaseModel):
    period_first: date
    total_recognized: Decimal
    lines: list[DeferredRevenuePreviewLine] = Field(default_factory=list)


class DeferredRevenueRecognizeBody(BaseModel):
    period_date: date
    override_reason: str | None = None


class DeferredRevenueRecognizeOut(BaseModel):
    period_first: date
    total_recognized: Decimal
    lines_recognized: int
    posted: bool


# ---------------------------------------------------------------------------
# Quotes — pre-invoice sales documents
# ---------------------------------------------------------------------------


class QuoteLineOut(BaseModel):
    """One line of a quote (nested in QuoteOut)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_no: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal
    tax_code_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    section_label: str | None = None
    material: str | None = None
    length_note: str | None = None
    drawing_ref: str | None = None


class QuoteLineCreate(BaseModel):
    """One line in a POST/PATCH payload."""

    description: str = Field(min_length=1)
    quantity: Decimal = Decimal("1")
    unit_price: Decimal = Decimal("0")
    tax_code_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    section_label: str | None = Field(default=None, max_length=255)
    material: str | None = Field(default=None, max_length=255)
    length_note: str | None = Field(default=None, max_length=255)
    drawing_ref: str | None = Field(default=None, max_length=255)


class QuoteBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    customer_id: uuid.UUID
    issue_date: date
    expiry_date: date | None = None
    currency: str = Field(default="AUD", min_length=3, max_length=3)
    validity_days: int = Field(default=28, ge=1)
    deposit_pct: Decimal = Decimal("50")
    late_fee_pct_per_month: Decimal = Decimal("2.5")
    is_supply_only: bool = False
    title: str | None = Field(default=None, max_length=255)
    scope: str | None = None
    notes: str | None = None
    terms: str | None = None


class QuoteCreate(QuoteBase):
    """POST body — DRAFT is implied."""

    lines: list[QuoteLineCreate] = Field(default_factory=list)


class QuoteUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    customer_id: uuid.UUID | None = None
    issue_date: date | None = None
    expiry_date: date | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    validity_days: int | None = Field(default=None, ge=1)
    deposit_pct: Decimal | None = None
    late_fee_pct_per_month: Decimal | None = None
    is_supply_only: bool | None = None
    title: str | None = Field(default=None, max_length=255)
    scope: str | None = None
    notes: str | None = None
    terms: str | None = None
    lines: list[QuoteLineCreate] | None = None


class QuoteOut(BaseModel):
    """Full quote response — nested lines, tenant_id, version."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    customer_id: uuid.UUID
    number: str | None = None
    issue_date: date
    expiry_date: date | None = None
    status: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    currency: str
    validity_days: int
    deposit_pct: Decimal
    late_fee_pct_per_month: Decimal
    is_supply_only: bool
    title: str | None = None
    scope: str | None = None
    notes: str | None = None
    terms: str | None = None
    accepted_at: datetime | None = None
    declined_at: datetime | None = None
    invoiced_at: datetime | None = None
    invoice_id: uuid.UUID | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    lines: list[QuoteLineOut] = Field(default_factory=list)


class QuoteListOut(BaseModel):
    items: list[QuoteOut]
    total: int
    limit: int
    offset: int


class QuoteConflictBody(BaseModel):
    detail: str
    current: QuoteOut


class QuoteConvertOut(BaseModel):
    """Response from POST /quotes/{id}/convert-to-invoice."""

    quote: QuoteOut
    invoice_id: uuid.UUID


# ---------------------------------------------------------------------------
# Expenses — paid-at-checkout sibling of bills
# ---------------------------------------------------------------------------


class ExpenseLineOut(BaseModel):
    """One line of an expense (nested in ExpenseOut)."""

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


class ExpenseLineCreate(BaseModel):
    """One line in a POST/PATCH payload."""

    description: str = Field(min_length=1)
    account_id: uuid.UUID
    tax_code_id: uuid.UUID | None = None
    quantity: Decimal = Decimal("1")
    unit_price: Decimal = Decimal("0")
    discount_pct: Decimal = Decimal("0")
    project_id: uuid.UUID | None = None


class ExpenseBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payment_account_id: uuid.UUID
    expense_date: date
    contact_id: uuid.UUID | None = None
    reference: str | None = None
    notes: str | None = None
    currency: str = Field(default="AUD", min_length=3, max_length=3)
    fx_rate: Decimal = Field(default=Decimal("1"), gt=Decimal("0"))


class ExpenseCreate(ExpenseBase):
    """POST body."""

    lines: list[ExpenseLineCreate] = Field(default_factory=list)


class ExpenseUpdate(BaseModel):
    """PATCH body — every field optional."""

    model_config = ConfigDict(from_attributes=True)

    payment_account_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    expense_date: date | None = None
    notes: str | None = None
    reference: str | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    fx_rate: Decimal | None = Field(default=None, gt=Decimal("0"))
    lines: list[ExpenseLineCreate] | None = None


class ExpenseOut(BaseModel):
    """Full expense response — includes nested lines, tenant_id, version."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tenant_id: uuid.UUID
    contact_id: uuid.UUID | None = None
    payment_account_id: uuid.UUID
    number: str | None = None
    reference: str | None = None
    expense_date: date
    status: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    currency: str
    fx_rate: Decimal
    notes: str | None = None
    journal_entry_id: uuid.UUID | None = None
    void_journal_entry_id: uuid.UUID | None = None
    posted_at: datetime | None = None
    posted_by: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    lines: list[ExpenseLineOut] = Field(default_factory=list)


class ExpenseListOut(BaseModel):
    items: list[ExpenseOut]
    total: int
    limit: int
    offset: int


class ExpenseConflictBody(BaseModel):
    detail: str
    current: ExpenseOut


# ---------------------------------------------------------------------------
# Time entries (v1 — standalone, ahead of the future ``employees`` table)
# ---------------------------------------------------------------------------


class TimeEntryBase(BaseModel):
    """Shared fields between create / update / out shapes."""

    work_date: date
    hours: Decimal = Field(gt=Decimal("0"))
    start_time: time | None = None
    end_time: time | None = None
    break_minutes: int = Field(default=0, ge=0)
    description: str = ""
    project_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    cost_centre_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    billable: bool = False
    rate: Decimal | None = None


class TimeEntryCreate(TimeEntryBase):
    """POST /api/v1/time-entries body."""

    # Default = logged-in user. Explicit override allowed for admins
    # logging hours on behalf of another worker.
    user_id: uuid.UUID | None = None


class TimeEntryUpdate(BaseModel):
    """PATCH /api/v1/time-entries/{id} — every field optional."""

    work_date: date | None = None
    hours: Decimal | None = Field(default=None, gt=Decimal("0"))
    start_time: time | None = None
    end_time: time | None = None
    break_minutes: int | None = Field(default=None, ge=0)
    description: str | None = None
    project_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    cost_centre_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    billable: bool | None = None
    rate: Decimal | None = None


class TimeEntryOut(BaseModel):
    """Full time entry as returned by the API."""

    id: uuid.UUID
    company_id: uuid.UUID
    user_id: uuid.UUID
    contact_id: uuid.UUID | None = None
    work_date: date
    hours: Decimal
    start_time: time | None = None
    end_time: time | None = None
    break_minutes: int
    description: str
    project_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    cost_centre_id: uuid.UUID | None = None
    billable: bool
    rate: Decimal | None = None
    invoice_line_id: uuid.UUID | None = None
    approval_status: str
    submitted_at: datetime | None = None
    approved_at: datetime | None = None
    approved_by: uuid.UUID | None = None
    rejection_reason: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class TimeEntryListOut(BaseModel):
    items: list[TimeEntryOut]
    total: int
    limit: int
    offset: int


class TimeEntryConvertToInvoiceLineRequest(BaseModel):
    """Body for POST /api/v1/time-entries/convert-to-invoice."""

    # Either append to an existing draft invoice ...
    invoice_id: uuid.UUID | None = None
    # ... OR create a new draft against this contact.
    contact_id: uuid.UUID | None = None
    # IDs to convert. Service refuses if any are non-billable, already
    # converted, archived, or LOCKED.
    entry_ids: list[uuid.UUID] = Field(min_length=1)


class TimeEntryConvertToInvoiceLineResponse(BaseModel):
    invoice_id: uuid.UUID
    invoice_line_id: uuid.UUID
    converted_entry_ids: list[uuid.UUID]
    total_hours: Decimal
    total_amount: Decimal


# ---------------------------------------------------------------------------
# Super funds (Phase 1A — payroll foundation)
# ---------------------------------------------------------------------------


class SuperFundBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    is_smsf: bool = False
    usi: str | None = Field(default=None, min_length=11, max_length=11)
    employer_abn: str | None = None
    esa: str | None = None
    smsf_bsb: str | None = None
    smsf_account_number: str | None = None
    smsf_account_name: str | None = None


class SuperFundCreate(SuperFundBase):
    is_default: bool = False


class SuperFundUpdate(BaseModel):
    name: str | None = None
    usi: str | None = None
    employer_abn: str | None = None
    esa: str | None = None
    smsf_bsb: str | None = None
    smsf_account_number: str | None = None
    smsf_account_name: str | None = None


class SuperFundOut(BaseModel):
    id: uuid.UUID
    company_id: uuid.UUID
    name: str
    usi: str | None = None
    spin: str | None = None
    is_smsf: bool
    employer_abn: str | None = None
    esa: str | None = None
    # SMSF bank fields are masked in the default response. Clients with
    # super_fund.edit + the explicit /reveal-bank endpoint get plaintext.
    has_smsf_bank: bool = False
    is_default: bool
    version: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class SuperFundListOut(BaseModel):
    items: list[SuperFundOut]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------


class EmployeeBase(BaseModel):
    """Shared shape for create / update."""

    contact_id: uuid.UUID
    start_date: date
    employment_basis: str = Field(
        description="STP2 single-letter code: F/P/C/L/V/N",
    )
    base_rate: Decimal = Field(ge=Decimal("0"))

    # All optional below — sensible defaults applied server-side.
    employee_number: str | None = None
    dob: date | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    suburb: str | None = None
    state: str | None = None
    postcode: str | None = None
    country_code: str = "AU"
    tax_treatment_code: str | None = None
    claims_tax_free_threshold: bool = False
    is_australian_resident: bool = True
    study_training_support_loan: bool = False
    working_holiday_maker: bool = False
    whm_country_code: str | None = None
    income_stream_type: str = "SAW"
    payg_branch_code: str | None = None
    super_fund_id: uuid.UUID | None = None
    super_member_number: str | None = None
    payslip_email: str | None = None
    payslip_delivery: str = "EMAIL"
    pay_frequency: str = "WEEKLY"
    pay_basis: str = "HOURLY"
    weekly_hours: Decimal = Decimal("38.00")
    notes: str | None = None


class EmployeeCreate(EmployeeBase):
    """POST /api/v1/employees body. TFN + bank are write-only — never echoed back."""

    tfn: str | None = None
    tfn_status: str = "NOT_PROVIDED"
    bsb: str | None = None
    account_number: str | None = None
    account_name: str | None = None


class EmployeeUpdate(BaseModel):
    """PATCH /api/v1/employees/{id} — every field optional. To clear a
    sensitive field, send the empty string explicitly; to leave it
    untouched, omit the key.
    """

    employee_number: str | None = None
    contact_id: uuid.UUID | None = None
    start_date: date | None = None
    dob: date | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    suburb: str | None = None
    state: str | None = None
    postcode: str | None = None
    country_code: str | None = None
    employment_basis: str | None = None
    tax_treatment_code: str | None = None
    claims_tax_free_threshold: bool | None = None
    is_australian_resident: bool | None = None
    study_training_support_loan: bool | None = None
    working_holiday_maker: bool | None = None
    whm_country_code: str | None = None
    income_stream_type: str | None = None
    payg_branch_code: str | None = None
    super_fund_id: uuid.UUID | None = None
    super_member_number: str | None = None
    payslip_email: str | None = None
    payslip_delivery: str | None = None
    pay_frequency: str | None = None
    pay_basis: str | None = None
    base_rate: Decimal | None = None
    weekly_hours: Decimal | None = None
    tfn_status: str | None = None
    notes: str | None = None
    # Sensitive write paths
    tfn: str | None = None
    bsb: str | None = None
    account_number: str | None = None
    account_name: str | None = None


class EmployeeOut(BaseModel):
    """Default API response. TFN + bank are MASKED.

    Privileged callers (with employee.tfn_view) hit /employees/{id}/tfn
    for plaintext; bank decryption flows through the pay-run service
    when issuing ABA disbursements.
    """

    id: uuid.UUID
    company_id: uuid.UUID
    contact_id: uuid.UUID
    employee_number: str
    payee_id_bms: uuid.UUID
    previous_payee_id: str | None = None

    # Sensitive — masked in this DTO.
    tfn_masked: str | None = None
    tfn_status: str
    has_bank: bool = False

    dob: date | None = None
    start_date: date
    end_date: date | None = None
    termination_reason: str | None = None

    address_line1: str | None = None
    address_line2: str | None = None
    suburb: str | None = None
    state: str | None = None
    postcode: str | None = None
    country_code: str

    employment_basis: str
    tax_treatment_code: str | None = None
    claims_tax_free_threshold: bool
    is_australian_resident: bool
    study_training_support_loan: bool
    working_holiday_maker: bool
    whm_country_code: str | None = None
    income_stream_type: str
    payg_branch_code: str | None = None

    super_fund_id: uuid.UUID | None = None
    super_member_number: str | None = None

    payslip_email: str | None = None
    payslip_delivery: str
    pay_frequency: str
    pay_basis: str
    base_rate: Decimal
    weekly_hours: Decimal

    notes: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class EmployeeListOut(BaseModel):
    items: list[EmployeeOut]
    total: int
    limit: int
    offset: int


class EmployeeTerminateRequest(BaseModel):
    end_date: date
    reason: str = Field(description="STP2 code: V/I/D/R/F/C/T")


class EmployeeTfnRevealOut(BaseModel):
    """Returned by /employees/{id}/tfn when the caller has employee.tfn_view."""

    employee_id: uuid.UUID
    tfn: str

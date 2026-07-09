"""Structural tests for the gRPC entity expansion — invoices, bills, payments, JEs.

These tests are import/callable checks only — no real gRPC port binding or DB
connection required.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Proto message presence
# ---------------------------------------------------------------------------


def test_proto_has_invoice_messages() -> None:
    """saebooks_pb2 exposes ListInvoicesRequest and InvoiceRecord."""
    from saebooks.grpc_gen import saebooks_pb2

    assert hasattr(saebooks_pb2, "ListInvoicesRequest")
    assert hasattr(saebooks_pb2, "InvoiceRecord")
    # Confirm they are constructible
    req = saebooks_pb2.ListInvoicesRequest()
    rec = saebooks_pb2.InvoiceRecord()
    assert req is not None
    assert rec is not None


def test_proto_has_bill_messages() -> None:
    """saebooks_pb2 exposes ListBillsRequest and BillRecord."""
    from saebooks.grpc_gen import saebooks_pb2

    assert hasattr(saebooks_pb2, "ListBillsRequest")
    assert hasattr(saebooks_pb2, "BillRecord")
    req = saebooks_pb2.ListBillsRequest()
    rec = saebooks_pb2.BillRecord()
    assert req is not None
    assert rec is not None


def test_proto_has_payment_messages() -> None:
    """saebooks_pb2 exposes ListPaymentsRequest and PaymentRecord."""
    from saebooks.grpc_gen import saebooks_pb2

    assert hasattr(saebooks_pb2, "ListPaymentsRequest")
    assert hasattr(saebooks_pb2, "PaymentRecord")
    req = saebooks_pb2.ListPaymentsRequest()
    rec = saebooks_pb2.PaymentRecord()
    assert req is not None
    assert rec is not None


def test_proto_has_journal_entry_messages() -> None:
    """saebooks_pb2 exposes ListJournalEntriesRequest and JournalEntryRecord."""
    from saebooks.grpc_gen import saebooks_pb2

    assert hasattr(saebooks_pb2, "ListJournalEntriesRequest")
    assert hasattr(saebooks_pb2, "JournalEntryRecord")
    req = saebooks_pb2.ListJournalEntriesRequest()
    rec = saebooks_pb2.JournalEntryRecord()
    assert req is not None
    assert rec is not None


# ---------------------------------------------------------------------------
# Servicer method presence
# ---------------------------------------------------------------------------


def test_servicer_has_list_invoices() -> None:
    """SAEBooksServicer has a callable ListInvoices method."""
    from saebooks.grpc_server import SAEBooksServicer

    svc = SAEBooksServicer()
    assert callable(getattr(svc, "ListInvoices", None))


def test_servicer_has_list_bills() -> None:
    """SAEBooksServicer has a callable ListBills method."""
    from saebooks.grpc_server import SAEBooksServicer

    svc = SAEBooksServicer()
    assert callable(getattr(svc, "ListBills", None))


def test_servicer_has_list_payments() -> None:
    """SAEBooksServicer has a callable ListPayments method."""
    from saebooks.grpc_server import SAEBooksServicer

    svc = SAEBooksServicer()
    assert callable(getattr(svc, "ListPayments", None))


def test_servicer_has_list_journal_entries() -> None:
    """SAEBooksServicer has a callable ListJournalEntries method."""
    from saebooks.grpc_server import SAEBooksServicer

    svc = SAEBooksServicer()
    assert callable(getattr(svc, "ListJournalEntries", None))

"""Marker mixin for company-scoped models.

A declarative class that inherits from ``CompanyScoped`` opts into
the row-level tenant filter implemented in
``saebooks.services.tenant``. The mixin declares NO columns — it's a
pure type marker. Every subclass is expected to already carry a
``company_id`` column (which 19 of them do at time of introduction).

Why mixin + not base:
    Each model declares ``company_id`` with its own FK and ondelete
    policy (CASCADE / SET NULL / RESTRICT). Moving the column onto a
    shared base would flatten those per-model decisions. A pure
    marker mixin leaves the existing columns alone.

Listener hook:
    ``services.tenant._scope_guard`` walks the execution plan and,
    for every entity whose mapper is a ``CompanyScoped`` subclass,
    wraps the statement with
    ``with_loader_criteria(cls, cls.company_id == current_company_id())``.
"""
from __future__ import annotations


class CompanyScoped:
    """Marker mixin — subclasses are filtered by the active company.

    The class intentionally has no ``company_id`` attribute on itself;
    the listener's lambda accesses it through the concrete subclass
    via SQLAlchemy's ``with_loader_criteria`` ``cls`` parameter.
    """

    __abstract__ = True

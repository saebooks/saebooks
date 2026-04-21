"""Row-level company-scope guard — defence-in-depth for multi-company.

Design
------
A contextvar (``_current_company``) carries the "active" company id
for the duration of a request (or a CLI job). When that var is set,
an ORM event listener installed on ``Session.do_orm_execute`` injects
``WHERE company_id = :current_company_id`` into every ``SELECT`` that
touches a ``CompanyScoped`` entity.

This is belt-and-braces: services should still filter by ``company_id``
explicitly, but if one forgets, the listener catches the leak at the
session layer. The listener is a **no-op when the contextvar is
unset**, so legacy single-company code paths that never bind a
company id keep working unchanged.

Opting out
----------
Admin tooling that needs cross-company visibility (e.g. Prometheus
metric scrapes, `services/dashboard.py` aggregating all companies,
period-close across tenants) must opt out explicitly:

    with bypass_tenant_scope():
        rows = await session.execute(select(Invoice))

The ``bypass_tenant_scope`` contextmgr sets a second contextvar that
the listener respects on its next tick.

Usage from middleware
---------------------
A future `TenantMiddleware` will call::

    token = set_current_company(request.state.company_id)
    try:
        response = await call_next(request)
    finally:
        reset_current_company(token)

in the same pattern the stdlib uses for request-scoped state. The
contextvar is safe across ``await`` boundaries because it follows
:pep:`567` semantics — ``asyncio.Task`` copies the current
`contextvars.Context` on creation.

Single-company today
--------------------
SAE Books runs one company per install at time of writing (community
edition). The listener is still exercised by the test suite so the
semantics are proven before multi-company flips on.
"""
from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Callable, Iterator
from contextvars import ContextVar, Token
from typing import Any

from sqlalchemy import ColumnElement, event
from sqlalchemy.orm import Session, with_loader_criteria

from saebooks.models._scope import CompanyScoped

log = logging.getLogger("saebooks.tenant")

# Request-scoped active company. Unset → listener is a no-op.
_current_company: ContextVar[uuid.UUID | None] = ContextVar(
    "saebooks.current_company_id", default=None
)

# Request-scoped bypass flag. When True the listener skips scoping
# even if a company id is set. Used by cross-company admin queries.
_bypass_scope: ContextVar[bool] = ContextVar(
    "saebooks.bypass_tenant_scope", default=False
)


def current_company_id() -> uuid.UUID | None:
    """Read the active company id, or None if no scope is active."""
    return _current_company.get()


def set_current_company(company_id: uuid.UUID | None) -> Token[uuid.UUID | None]:
    """Bind ``company_id`` as the active scope. Returns a reset token.

    Caller is responsible for ``reset_current_company(token)`` on
    completion — usually in a ``finally`` block. Middleware is the
    intended caller; tests use the ``scope`` context manager below.
    """
    return _current_company.set(company_id)


def reset_current_company(token: Token[uuid.UUID | None]) -> None:
    _current_company.reset(token)


@contextlib.contextmanager
def scope(company_id: uuid.UUID | None) -> Iterator[None]:
    """Bind a company scope for the duration of a ``with`` block.

    Primarily for tests + CLI jobs. Middleware uses ``set_current_company``
    directly so the token can cross the ASGI request boundary.
    """
    token = _current_company.set(company_id)
    try:
        yield
    finally:
        _current_company.reset(token)


@contextlib.contextmanager
def bypass_tenant_scope() -> Iterator[None]:
    """Temporarily suppress scope injection for cross-company queries.

    Use sparingly — admin metrics, cross-tenant reports, migrations.
    Never from user-facing code paths.
    """
    token = _bypass_scope.set(True)
    try:
        yield
    finally:
        _bypass_scope.reset(token)


# --------------------------------------------------------------------- #
# ORM event listener                                                      #
# --------------------------------------------------------------------- #


def _make_pred(
    cls: type, cid: uuid.UUID
) -> Callable[[Any], ColumnElement[bool]]:
    """Return a lambda that binds ``cls`` + ``cid`` for a scope criteria.

    ``with_loader_criteria`` calls the lambda with the entity class
    once at compile time. We bind ``cls`` in the closure via the
    factory so the loop variable doesn't leak.
    """
    def _pred(_: Any) -> ColumnElement[bool]:
        return cls.company_id == cid  # type: ignore[attr-defined, no-any-return]
    return _pred


def _scope_guard(orm_execute_state: object) -> None:
    """``do_orm_execute`` handler — injects the company filter.

    SQLAlchemy calls this for every ``Session.execute`` of an ORM
    statement. We narrow to SELECTs (no point filtering UPDATE/DELETE
    since a cross-tenant write would already be a service-layer bug)
    and skip when the contextvar is unset or bypass is on.

    Walks the plan's mappers and adds one ``with_loader_criteria`` per
    concrete CompanyScoped entity. ``with_loader_criteria`` needs the
    concrete class (not the marker base) so its lambda can resolve
    ``cls.company_id`` against the real mapped column. Calling it with
    the marker base raises ``AttributeError: CompanyScoped has no
    attribute company_id``.
    """
    if _bypass_scope.get():
        return
    cid = _current_company.get()
    if cid is None:
        return
    if not getattr(orm_execute_state, "is_select", False):
        return

    mappers = getattr(orm_execute_state, "all_mappers", None)
    if not mappers:
        return

    options = []
    seen: set[type] = set()
    for mapper in mappers:
        cls = mapper.class_
        if cls in seen:
            continue
        if not issubclass(cls, CompanyScoped):
            continue
        seen.add(cls)
        # Bind ``cls`` explicitly via a factory — the lambda closure
        # otherwise captures the loop variable by reference and every
        # iteration would filter against whichever class was last in
        # the loop.
        options.append(
            with_loader_criteria(
                cls,
                _make_pred(cls, cid),
                include_aliases=True,
            )
        )

    if options:
        orm_execute_state.statement = (  # type: ignore[attr-defined]
            orm_execute_state.statement.options(*options)  # type: ignore[attr-defined]
        )


def install() -> None:
    """Register the scope guard on the shared ``Session`` class.

    Idempotent — safe to call multiple times. Called once at app
    startup from ``saebooks.main.create_app``.
    """
    if getattr(install, "_installed", False):
        return
    event.listen(Session, "do_orm_execute", _scope_guard)
    install._installed = True  # type: ignore[attr-defined]
    log.debug("tenant scope guard installed on Session")

"""Boot-isolation contract for ``saebooks.api.v1``'s router manifest.

M2 §5 build-sequence step 2 / audit §4 Layer A: a broken router module
must not fail the whole app's boot. It must instead serve
``503 {"module": ..., "status": "unavailable"}`` on that one prefix,
while every other router (including ones later in the manifest) still
mounts and works normally.

Two kinds of "broken" are exercised, matching the two failure modes
the design calls out:

* ``ImportError`` — the module itself does not exist / fails to import
  (``importlib.import_module`` raises before any router object exists).
* An exception raised for some other reason while resolving the router
  object (stands in for "decorator-time route-registration error" —
  the dominant real-world failure mode per the audit, since FastAPI
  registers routes via ``@router.get(...)`` at *import* time, not at
  ``include_router()`` time).

Also covered: the safe-vs-unsafe-to-stub prefix logic
(``_stub_safe_prefixes``) and a regression pin on the real production
manifest (route count + a spot-check of real endpoints) so the guarded
loop's success path stays byte-for-byte equivalent to the prior static
top-of-file import list.
"""
from __future__ import annotations

import importlib
from typing import Any

from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

from saebooks.api.v1 import (
    MODULE_MANIFEST,
    _RouterSpec,
    _stub_safe_prefixes,
    build_v1_router,
)

# ---------------------------------------------------------------------- #
# _stub_safe_prefixes                                                    #
# ---------------------------------------------------------------------- #


def test_empty_prefix_is_never_stub_safe() -> None:
    manifest = (_RouterSpec("a", "router", ""),)
    assert _stub_safe_prefixes(manifest) == frozenset()


def test_exact_shared_prefix_is_never_stub_safe() -> None:
    """Two entries at the identical prefix -- neither is stub-safe."""
    manifest = (
        _RouterSpec("a", "router", "/shared"),
        _RouterSpec("b", "router", "/shared"),
    )
    assert _stub_safe_prefixes(manifest) == frozenset()


def test_parent_prefix_is_not_stub_safe_but_child_is() -> None:
    """A prefix that is the PARENT of another manifest entry must not be
    catch-all-stubbed (it would shadow the child's real routes), but the
    child itself (a leaf, no further children) is safe."""
    manifest = (
        _RouterSpec("parent", "router", "/admin"),
        _RouterSpec("child", "router", "/admin/inspect"),
    )
    safe = _stub_safe_prefixes(manifest)
    assert "/admin" not in safe
    assert "/admin/inspect" in safe


def test_unique_prefix_is_stub_safe() -> None:
    manifest = (
        _RouterSpec("a", "router", "/foo"),
        _RouterSpec("b", "router", "/bar"),
    )
    assert _stub_safe_prefixes(manifest) == frozenset({"/foo", "/bar"})


# ---------------------------------------------------------------------- #
# build_v1_router — boot survives a broken module                       #
# ---------------------------------------------------------------------- #


async def test_boot_survives_nonexistent_module_import_error() -> None:
    """A manifest entry naming a module that doesn't exist on disk must
    not raise out of ``build_v1_router`` -- it gets a 503 stub instead,
    and routers after it in the manifest still mount."""
    manifest = (
        _RouterSpec("health", "router", ""),
        _RouterSpec("this_module_does_not_exist_anywhere", "router", "/broken-import"),
        _RouterSpec("contacts", "router", "/contacts"),
    )
    r = build_v1_router(manifest)

    app = FastAPI()
    app.include_router(r)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # The broken module's prefix is unique (not shared/parent) -> stub.
        resp = await client.get("/api/v1/broken-import/anything")
        assert resp.status_code == 503, resp.text
        body = resp.json()
        assert body == {
            "module": "this_module_does_not_exist_anywhere.router",
            "status": "unavailable",
        }
        # Bare prefix (no trailing path) also stubbed.
        resp_bare = await client.get("/api/v1/broken-import")
        assert resp_bare.status_code == 503, resp_bare.text

        # A router BEFORE the broken entry still mounted.
        resp_before = await client.get("/api/v1/healthz")
        assert resp_before.status_code == 200, resp_before.text

        # A router AFTER the broken entry still mounted -- proves the
        # loop continues past a failure rather than aborting.
        resp_after = await client.get("/api/v1/contacts")
        assert resp_after.status_code in (200, 401), resp_after.text
        # (401 acceptable: contacts requires require_bearer; the point
        # is it's ROUTED, not a 404/503 from a missing/stubbed router.)


async def test_boot_survives_attribute_resolution_failure() -> None:
    """A manifest entry pointing at a real, successfully-importable
    module but a bogus attribute name (stands in for a router object
    that failed to build/register for some other reason) also degrades
    to a stub rather than raising.

    ``health.router``'s real ``APIRouter(...)`` has NO prefix baked in
    (health/version/license are mounted at the bare ``/api/v1/`` root)
    -- a successful ``include_router()`` always uses the router
    object's own real prefix, never the manifest entry's ``prefix``
    field (that field only feeds the STUB path, since a failed import
    never produces an object to introspect). So the "good sibling"
    here is asserted at ``/api/v1/healthz``, matching ``health.py``'s
    actual route, while the broken entry's manifest-declared prefix
    (``/health-broken-attr``) is honoured for its stub.
    """
    manifest = (
        _RouterSpec("health", "router", ""),
        _RouterSpec("health", "this_attr_does_not_exist", "/health-broken-attr"),
    )
    r = build_v1_router(manifest)

    app = FastAPI()
    app.include_router(r)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/health-broken-attr/x")
        assert resp.status_code == 503, resp.text
        assert resp.json()["module"] == "health.this_attr_does_not_exist"

        # The good entry for the SAME module still works (real prefix).
        resp_ok = await client.get("/api/v1/healthz")
        assert resp_ok.status_code == 200, resp_ok.text


async def test_broken_entry_at_unsafe_prefix_yields_plain_404_not_503() -> None:
    """When the failing entry's prefix is shared with a working sibling,
    no catch-all is mounted -- an unmatched path there is a normal 404,
    not an incorrect 503 that would also risk shadowing the sibling.

    Uses the REAL production shared-prefix pair (``login.router`` +
    ``signup.router``, both genuinely ``prefix="/auth"``) plus a third,
    fictitious entry declared at the same prefix to simulate its
    sibling failing to import -- exercising the exact shape that
    exists in the live manifest (``login``/``signup``), not a
    synthetic prefix override that wouldn't reflect a real router's
    baked-in prefix.
    """
    manifest = (
        _RouterSpec("login", "router", "/auth"),  # real, genuinely prefix="/auth"
        _RouterSpec(
            "this_module_does_not_exist_anywhere", "router", "/auth"
        ),  # fails, declared at the same prefix
    )
    r = build_v1_router(manifest)
    app = FastAPI()
    app.include_router(r)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # The working sibling's real route is untouched (401, not 404/503
        # -- require_bearer rejects the missing token, proving it's routed).
        resp_ok = await client.get("/api/v1/auth/me")
        assert resp_ok.status_code == 401, resp_ok.text
        # A path that would have belonged only to the failed sibling 404s
        # (no route matches) rather than getting an incorrect 503.
        resp_missing = await client.get("/api/v1/auth/nonexistent-path")
        assert resp_missing.status_code == 404, resp_missing.text


# ---------------------------------------------------------------------- #
# Regression pin — production manifest's success path is unchanged      #
# ---------------------------------------------------------------------- #


def test_production_manifest_has_no_duplicate_module_attr_entries() -> None:
    """Sanity: every (module, attr) pair in MODULE_MANIFEST is unique --
    a duplicate would double-mount the same router."""
    seen = [(spec.module, spec.attr) for spec in MODULE_MANIFEST]
    assert len(seen) == len(set(seen)), "duplicate (module, attr) entry in MODULE_MANIFEST"


def test_production_router_mounts_expected_route_count() -> None:
    """Prove the guarded loop changes NOTHING on the all-healthy success
    path -- without pinning a magic number that breaks on every PR that
    adds or removes a router (unrelated to what this test protects).

    Builds a second, UNGUARDED comparison router straight off
    ``MODULE_MANIFEST`` -- importing each module and ``include_router``-
    ing it with no try/except around either step. If any manifest entry
    were genuinely broken, this loop raises immediately (a loud
    failure), rather than silently letting a stubbed route count become
    the new "expected" baseline. The guarded production router's
    resolved route count must equal this unguarded rebuild's -- proving
    ``build_v1_router`` mounted zero 503 stubs and dropped zero routes.

    The equality check alone wouldn't catch a wholesale collapse (e.g.
    ``MODULE_MANIFEST`` accidentally emptied, both sides trivially at
    0) -- paired with a sane floor for that case.
    """
    from saebooks.api.v1 import router as production_router

    unguarded = APIRouter(prefix="/api/v1")
    imported: dict[str, Any] = {}
    for spec in MODULE_MANIFEST:
        module = imported.get(spec.module)
        if module is None:
            module = importlib.import_module(f"saebooks.api.v1.{spec.module}")
            imported[spec.module] = module
        unguarded.include_router(getattr(module, spec.attr))

    # One included sub-router per manifest entry on both sides -> the
    # guarded loop neither dropped an entry nor replaced one with a 503
    # stub. We count include-entries, NOT resolved ``.path`` routes:
    # ``router.routes`` for an included sub-router is a lazy include-
    # wrapper with no ``.path`` under the runtime FastAPI version, so
    # path-set introspection is unreliable -- endpoint reachability is
    # proven behaviorally in
    # ``test_production_router_mounts_real_endpoints_not_stubs``.
    assert len(unguarded.routes) == len(MODULE_MANIFEST)
    assert len(production_router.routes) == len(MODULE_MANIFEST), (
        f"guarded router mounted {len(production_router.routes)} sub-routers "
        f"but MODULE_MANIFEST has {len(MODULE_MANIFEST)} -- something was "
        "dropped or stubbed on the all-healthy success path"
    )
    assert len(MODULE_MANIFEST) >= 50, "MODULE_MANIFEST looks collapsed"


async def test_production_router_mounts_real_endpoints_not_stubs() -> None:
    """Behaviorally prove a representative sample of real endpoints
    actually mounted -- covers the kernel group, BOTH routers of each
    shared-prefix pair (auth login+signup, principal, license,
    integrations -- the check that proves the guarded loop didn't
    silently shadow the second sibling), the ``/admin`` parent + two
    children, an ordinary business router, and the two new M2
    module-registry endpoints.

    Routes are checked by actually requesting them through the ASGI app
    (the mechanism the rest of this suite routes through), NOT by
    introspecting ``router.routes`` -- which under the runtime FastAPI
    version is a list of lazy include-wrappers with no ``.path``. A
    mounted router answers with its real status (200 / 401 / 405 / ...);
    a *dropped* router yields 404 (no path match); a *stubbed* one yields
    the 503 ``{"status": "unavailable"}`` sentinel. So not-404 proves
    mounted, not-sentinel proves it is the real router. Every path below
    is either open or router-level ``require_bearer``-gated (401 before
    any feature check), so an unauthenticated GET is never a 404-feature-
    gate false negative.
    """
    from saebooks.api.v1 import router

    def _is_stub(resp: Any) -> bool:
        if resp.status_code != 503:
            return False
        try:
            body = resp.json()
        except Exception:
            return False
        return isinstance(body, dict) and body.get("status") == "unavailable"

    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in (
            # --- Kernel (open -> 200) ---------------------------------
            "/api/v1/healthz",
            "/api/v1/version",
            "/api/v1/license",
            # --- Shared-prefix pairs (BOTH siblings each) -------------
            "/api/v1/auth/login",  # login.router (POST-only -> 405)
            "/api/v1/auth/signup",  # signup.router (POST-only -> 405)
            "/api/v1/principal/tenants",  # principal_auth.router
            "/api/v1/principal/auth/webauthn/authenticate/begin",  # .auth_router
            "/api/v1/license/snapshot",  # license.router (bearer -> 401)
            "/api/v1/license/promo-stats",  # license._promo_router
            "/api/v1/integrations/stripe/customer/connect",  # integrations.router (bearer -> 401)
            "/api/v1/integrations/paperless/webhook",  # integrations.public_router
            # --- /admin parent + children ------------------------------
            "/api/v1/admin/audit-log",
            "/api/v1/admin/inspect/companies/00000000-0000-0000-0000-000000000000",
            "/api/v1/admin/tenants",
            # --- Ordinary business router ------------------------------
            "/api/v1/contacts",
            # --- M2 module registry (this PR) --------------------------
            "/api/v1/modules",
            "/api/v1/modules/usage",
        ):
            resp = await client.get(path)
            assert resp.status_code != 404, (
                f"{path!r} -> 404: its router appears dropped / not mounted "
                "on the guarded loop's success path"
            )
            assert not _is_stub(resp), (
                f"{path!r} resolved to the guarded-import 503 stub, not the "
                f"real router: {resp.text}"
            )

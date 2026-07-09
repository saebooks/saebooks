"""Internal-only API surface — not routed through the public edge.

Routers here mount on the FastAPI app OUTSIDE ``/api/v1`` and OUTSIDE the
public OpenAPI. They are called only by sibling containers over the docker
network (e.g. saebooks-web -> saebooks-api for ephemeral demo provisioning),
never by a browser through Caddy/Consul (which only routes the public web
container). Each endpoint additionally enforces a shared-secret guard as
defence-in-depth.
"""
from fastapi import APIRouter

from saebooks.api.internal.demo import router as demo_router
from saebooks.api.internal.numbering import router as numbering_router

# Umbrella router — main.py mounts this at /internal.
router = APIRouter(prefix="/internal")
router.include_router(demo_router)
router.include_router(numbering_router)

__all__ = ["router"]

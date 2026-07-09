# grpc-web support

SAE Books exposes the `SAEBooks` service over **three wire formats**
backed by **one proto schema** in `saebooks/proto/saebooks.proto`:

| Transport | Port | Implementation | Consumers |
| --- | --- | --- | --- |
| Connect HTTP+JSON / HTTP+proto | 18310 | `connecpy` ASGI mount in FastAPI (`saebooks/connect_app.py`) | Go CLI, MCP server, any Connect-Go/TS client, curl |
| gRPC (HTTP/2 binary) | 50051 | `grpcio.aio.server` in `saebooks/grpc_server.py` | mobile (planned), grpcurl |
| **grpc-web (HTTP/1.1)** | **18315** | **`grpcwebproxy` sidecar in front of `:50051`** | **browsers, `connect.WithGRPCWeb()` clients** |

All three share the same proto definitions, the same handler bodies,
and the same `BearerAuthInterceptor` semantics.

This document covers the grpc-web path specifically — the architectural
decisions, the wire format, how auth flows, and how to verify it.

---

## Why grpcwebproxy instead of a custom Python middleware?

`connecpy` 2.3 does not implement the grpc-web protocol. The two
options were:

1. **Custom Python ASGI adapter.** Decode the grpc-web envelope
   (`<flags:1><len:4><proto-bytes>`), strip the 5-byte header, re-issue
   the call as Connect HTTP+proto via the existing connecpy mount,
   re-frame the response as a grpc-web data frame + trailer frame.
   Cover `application/grpc-web+proto` and base64-encoded
   `application/grpc-web-text+proto`. Map Connect's JSON error bodies
   to grpc-web's `grpc-status` / `grpc-message` headers/trailers.
   Roughly 200 lines of protocol code.
2. **`grpcwebproxy` sidecar in front of `:50051`.** A static Go binary
   from `improbable-eng/grpc-web` (≈ 12 MB, statically linked, years
   of production use). Translates grpc-web HTTP/1.1 ↔ gRPC HTTP/2.
   Zero code. Zero ongoing maintenance.

We went with option 2. The sidecar adds one container to the deployment
stack; in exchange we drop a non-trivial chunk of protocol-translation
code that we'd otherwise own. The CLI already works over Connect
HTTP+proto so the grpc-web port exists primarily for browser clients
and any future tool that uses `connect.WithGRPCWeb()`.

If `connecpy` lands native grpc-web support upstream, the sidecar can
be retired without API changes — the wire format the clients see is
unchanged.

---

## Architecture

```
            ┌────────────────────────────────────────────┐
            │ saebooks-api-1 container                   │
            │                                            │
 :18310 ◄──►│ FastAPI ─┬─► REST /api/v1/*                │
            │          │                                 │
            │          └─► connecpy ASGI mount           │
            │              /saebooks.SAEBooks/*          │
            │              (Connect HTTP+proto/JSON)     │
            │                                            │
 :50051 ◄──►│ grpc.aio.server                            │
            │   BearerAuthInterceptor                    │
            │   SAEBooksServicer                         │
            └────────────────────────────────────────────┘
                              ▲
                              │ HTTP/2 gRPC
                              │
                  ┌───────────┴───────────┐
                  │ saebooks-grpcwebproxy │
 :18315 ◄────────►│   improbable-eng      │  HTTP/1.1 grpc-web
                  │   v0.15.0             │
                  └───────────────────────┘
```

The proxy listens on container port 8080 (host-bound to a
LAN-only address on port `18315`) and forwards every request to
`api:50051` on the shared `saebooks_default` docker network.

---

## Auth model

The proxy does **no auth of its own.** It passes the `Authorization`
header through to `:50051`, where `BearerAuthInterceptor` (in
`saebooks/grpc_server.py`) verifies it. The verification logic is
identical to the connecpy interceptor and to `api/v1/auth.require_bearer`:

1. JWT decode (existing token path) — claims stamped onto contextvars
2. `saebk_*` API token — bcrypt verify via `services.api_tokens`,
   `(user_id, tenant_id, company_id)` stamped onto contextvars
3. Static dev bearer (`SAEBOOKS_DEV_API_TOKEN`) — default tenant only
4. Otherwise `UNAUTHENTICATED` (grpc code 16)

`Heartbeat` is the only method exempt — uptime probes shouldn't need
a token.

After a successful verify the interceptor stamps three module-level
contextvars in `grpc_server.py` (`_current_user_id`,
`_current_tenant_id`, `_current_company_id`). The shared helper
`_first_company_id` reads them and binds `app.current_tenant` on the
session before any SELECT — without that, every RLS-scoped table
returns zero rows and handlers abort with "No active company in
database".

These contextvars are duplicated (not imported) from `connect_app.py`
to avoid a circular import — `connect_app` already pulls
`_presence_store` / `_lock_store` from `grpc_server` for the shared
in-memory state.

---

## Wire format

Standard grpc-web framing. Both `application/grpc-web+proto` and
`application/grpc-web-text+proto` (base64-wrapped) are accepted.

**Request** — a single envelope:

```
┌──────────┬──────────┬──────────────────┐
│ flags(1) │ length(4)│ proto bytes      │
└──────────┴──────────┴──────────────────┘
  flags = 0x00 (data)
  length = big-endian uint32
```

**Response** — data frame followed by trailer frame:

```
┌──────────┬──────────┬──────────────────┐
│ 0x00     │ length(4)│ proto bytes      │  data
└──────────┴──────────┴──────────────────┘
┌──────────┬──────────┬──────────────────┐
│ 0x80     │ length(4)│ "grpc-status:0\r\n│  trailer
│          │          │  grpc-message:\r\n"│
└──────────┴──────────┴──────────────────┘
```

**Error responses** — when the handler raises before producing a data
frame (e.g., `UNAUTHENTICATED` from the interceptor), `grpcwebproxy`
surfaces the status in HTTP response **headers** rather than a body
trailer:

```
HTTP/1.1 200 OK
content-type: application/grpc-web+proto
grpc-status: 16
grpc-message: missing bearer token
```

A correct grpc-web client checks both. The verification script below
parses headers first, body trailer as fallback.

---

## Verify

End-to-end test from any container on the `saebooks_default` network
(here, exec'd into `saebooks-api-1` for convenience):

```python
import asyncio, struct, httpx
from saebooks.grpc_gen import saebooks_pb2 as pb

TOKEN = "saebk_..."  # issued by POST /api/v1/api-tokens

def envelope(msg: bytes) -> bytes:
    return b"\x00" + struct.pack(">I", len(msg)) + msg

def parse(body: bytes):
    pos, msg, trailer = 0, None, {}
    while pos + 5 <= len(body):
        flags = body[pos]
        ln = struct.unpack(">I", body[pos+1:pos+5])[0]
        payload = body[pos+5:pos+5+ln]
        pos += 5 + ln
        if flags & 0x80:
            for line in payload.decode().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    trailer[k.strip().lower()] = v.strip()
        else:
            msg = payload
    return msg, trailer

async def main():
    body = envelope(
        pb.ListContactsRequest(page=pb.PageRequest(page_size=3)).SerializeToString()
    )
    r = await httpx.AsyncClient().post(
        "http://grpcwebproxy:8080/saebooks.SAEBooks/ListContacts",
        content=body,
        headers={
            "Content-Type": "application/grpc-web+proto",
            "Authorization": f"Bearer {TOKEN}",
        },
        timeout=8,
    )
    msg, trailer = parse(r.content)
    status = r.headers.get("grpc-status") or trailer.get("grpc-status")
    print("grpc-status:", status)
    if msg:
        resp = pb.ListContactsResponse()
        resp.ParseFromString(msg)
        for c in resp.contacts:
            print(f"  {c.id[:8]} {c.name}")

asyncio.run(main())
```

Expected outputs:

| Authorization | grpc-status |
| --- | --- |
| (none, `Heartbeat`) | `0` + body `status="ok"` |
| (none, any other method) | `16` "missing bearer token" |
| `Bearer saebk_<64 zeros>` | `16` "invalid api token" |
| `Bearer garbage` | `16` "invalid bearer token" |
| Valid `saebk_*` token | `0` + N contact rows |

---

## Deployment

The proxy lives in the host-local compose stack (deployment-specific
config, not part of this repo). Add this service block to your
`docker-compose.yml`:

```yaml
grpcwebproxy:
  build:
    context: ./grpcwebproxy
  image: saebooks-grpcwebproxy:0.15.0
  restart: unless-stopped
  ports:
  - <lan-ip>:18315:8080
  command:
  - --backend_addr=api:50051
  - --backend_tls=false
  - --run_tls_server=false
  - --server_http_debug_port=8080
  - --allow_all_origins
  - --use_websockets
  depends_on:
  - api
  networks:
  - default
```

Then, on the deployment host:

```bash
cd /path/to/saebooks && sudo docker compose up -d grpcwebproxy
```

The image build pulls the `v0.15.0` linux-x86_64 binary from the
official improbable-eng GitHub releases; no Go toolchain required on
the build host.

For public exposure (caddy + Cloudflare DNS for, e.g.,
`grpc.saebooks.com.au`), the proxy supports TLS termination via
`--run_tls_server=true --server_tls_cert_file=… --server_tls_key_file=…`,
but the common pattern is to keep TLS at Caddy and let the proxy
listen plaintext on the internal network.

---

## What's still on `:50051` only

Two RPCs are wired in `grpc_server.py` but **not** in `connect_app.py`,
so today only the raw gRPC + grpc-web paths can reach them:

- `WatchChanges` — server-streaming change feed (also ported to
  connecpy; both surfaces work)
- `WatchPresence` — server-streaming collaborative presence (also
  ported)
- `AcquireLock` / `ReleaseLock` — unary lock RPCs (also ported)

All four share their in-memory state (`_presence_store`, `_lock_store`,
`_presence_queues`) across grpcio and connecpy via direct module
imports, so a desktop client on raw gRPC and a browser on grpc-web
see each other's presence and contend on the same locks in real time.

---

## Provenance

The grpc-web work originally landed in commit `0bc36be` (which also
carried an unrelated cashbook change — both files got swept into a
single `git add -A` by another session). This doc is the missing
context that should have lived in a dedicated grpc-web commit
message. The commit history reads:

```
0bc36be feat(cashbook): single-entry invoices in cashbook mode
        # ALSO contains: grpcwebproxy/Dockerfile + README,
        #                BearerAuthInterceptor in grpc_server.py,
        #                _first_company_id tenant binding fix
```

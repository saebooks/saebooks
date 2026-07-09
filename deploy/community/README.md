# SAE Books — Community Edition bundle

A self-hosted, double-entry accounting stack you can run with one
`docker compose up`. This bundle is the reference topology for the
**community edition**.

## What runs

| Service | Image / build | Role | Exposed |
|---|---|---|---|
| `db` | `postgres:16-alpine` | The ledger. | private only |
| `api` | this repo (`Dockerfile`) | **The engine — a pure API service.** REST (`/api/v1`), MCP (`/mcp`), and gRPC/Connect. No HTML UI. | `8042→8000`, `50051` |
| `web` | `saebooks-web` (sibling repo) | The browser UI — the **only** bookkeeper. Talks to `api` over REST. | `8080` |
| `latex-api` | `saebooks-latex-api` (sibling repo) | XeLaTeX render service. Compiles invoice / statement PDFs on demand. | private only |

> **Why no UI in the engine?** As of #32 the engine's embedded HTML UI
> was retired. Presentation lives entirely in `saebooks-web`; the engine
> is a headless API. If you point a browser at the engine (`:8042`) it
> redirects to `/docs` (the OpenAPI explorer), not a dashboard.

## Quickstart

```bash
cd deploy/community
cp .env.example .env
# Edit .env and fill in every value marked REQUIRED.
docker compose up -d
```

Then:

- Web UI: <http://localhost:8080>
- REST API + docs: <http://localhost:8042/docs>
- gRPC / Connect: `localhost:50051`
- Liveness probe (unauthenticated): <http://localhost:8042/api/v1/healthz>

## Environment (the #32 ops rules)

### 1. Tokens fail-closed

Every shared secret between containers is declared with the
`${VAR:?message}` form in `docker-compose.yml`. If the variable is
unset or empty, `docker compose up` **aborts** with that message — it
will not boot a service with an open internal endpoint. This is
deliberate: an empty `RENDER_SERVICE_TOKEN` would otherwise leave the
PDF render endpoint unauthenticated on the private network.

Required (the stack refuses to start without them):

| Variable | Used by | Purpose |
|---|---|---|
| `POSTGRES_PASSWORD` | `db`, `api` | Database superuser password. |
| `RENDER_SERVICE_TOKEN` | `api`, `latex-api` | Shared secret. The engine sends it as `X-Render-Token`; `latex-api` rejects any render call that does not match. **Must be identical in both.** |
| `INTERNAL_API_TOKEN` | `api` | Gates the engine's `/internal/*` module hand-off endpoints (`X-Internal-Token`). |
| `SAEBOOKS_WEB_SECRET_KEY` | `web` | Signs the web UI's browser sessions. |

Optional (blank disables the feature): `SAEBOOKS_FIELD_ENCRYPTION_KEY`,
`SMTP_*` (outbound magic-link / invoice email), `SEED_COMPANY_*`
(first-boot company seed), `SAEBOOKS_LOG_JSON`.

### 2. Container-name-pinned URLs

Services address each other by **compose service name** on the private
default network, never by host port or `127.0.0.1`:

- Engine → Postgres: `db:5432` (inside `DATABASE_URL`)
- Engine → renderer: `http://latex-api:8080` (`RENDER_SERVICE_URL`)
- Web → engine: `http://api:8000` (`SAEBOOKS_API_URL`)

Only the `ports:` entries in the compose file cross the host boundary.
`latex-api` publishes no port at all — it is reachable only by the
engine over the private network.

## Optional modules

The compose file ships commented stubs for the **pre-accounting** and
**capture** module containers (built from `Dockerfile.preaccounting` and
`Dockerfile.capture` in this repo). Uncomment the service block, set its
fail-closed token in `.env`, and add the matching container-name-pinned
`*_BASE_URL` + `*_SERVICE_TOKEN` to the `api` service environment. See
the inline comments in `docker-compose.yml`.

## Notes

- The engine runs its own Alembic migrations on boot (fail-closed
  preflight); `db` must be healthy first, which the `depends_on`
  `condition: service_healthy` guarantees.
- `web` and `latex-api` build from **sibling checkouts** next to this
  repo (`../../../saebooks-web`, `../../../saebooks-latex-api`). Adjust
  the `build.context` paths, or swap to a pulled `image:`, to match your
  layout.
- For a single-tenant community install, keep `SAEBOOKS_EDITION=community`
  (the default). The engine warns (does not crash) if it ever finds more
  than one active company.

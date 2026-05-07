# SAE Books — SAP Bundle

Self-hosted accounting platform: saebooks-api + saebooks-web + Postgres 16,
one compose stack, three commands to launch.

## Prerequisites

- Docker Engine 24+ with the Compose plugin (`docker compose version`)
- The sibling repo `saebooks-web` checked out next to this repo:
  ```
  ~/projects/saebooks/       ← this repo
  ~/projects/saebooks-web/   ← must exist for the web build context
  ```

## Quickstart

```bash
cp .env.example .env
# Edit .env — set POSTGRES_PASSWORD and SAEBOOKS_WEB_SECRET_KEY at minimum
docker compose up -d
```

The web UI will be available at **http://localhost:8080** once the `api`
healthcheck passes (~45 seconds on first start while Alembic migrations run).

## Ports

| Port  | Service                    |
|-------|----------------------------|
| 8080  | Web UI (Jinja2 + HTMX)     |
| 8042  | REST API (`/api/v1/`)      |
| 50051 | gRPC (desktop client)      |

## Stopping and upgrading

```bash
# Stop without removing data
docker compose down

# Pull new images and restart
docker compose pull
docker compose up -d
```

Data is stored in the `saebooks_db` Docker volume. It is not affected by
`docker compose down`.

## Reverse proxy (Caddy example)

To expose the web UI at a domain behind Caddy:

```
books.example.com {
    reverse_proxy localhost:8080
}
```

Set `https_only=True` in `saebooks_web/main.py` (or raise a PR — it is
currently `False` for dev convenience) if you want cookies to be
`Secure`-flagged.

## Building images locally

From the saebooks repo root:

```bash
# API image
docker buildx build --builder saebooks \
  --platform linux/amd64,linux/arm64 \
  --tag saebooks-api:dev .

# Web image (from sibling repo)
cd ../saebooks-web
docker buildx build --builder saebooks \
  --platform linux/amd64,linux/arm64 \
  --tag saebooks-web:dev .
```

linux/riscv64 is Tier-2 (best-effort, no SLA). Add it to `--platform` if
needed; expect 3-5× longer build time due to QEMU emulation.

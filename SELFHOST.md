# Self-hosting SAE Books

A practical walkthrough for running SAE Books on your own hardware.
This is the alpha self-host story — opinionated, single-node, Docker
Compose. For HA / Kubernetes / multi-node, talk to us first.

> **Audience:** sysadmin or comfortable Linux user. You should know
> what `docker compose`, a reverse proxy, and an `.env` file are.

## What you get

Three containers:

| Service | Image | Port | Role |
|---|---|---|---|
| `db` | `postgres:16-alpine` | (internal) | Ledger storage |
| `api` | `saebooks/saebooks` | `8042` REST + `50051` gRPC | FastAPI ledger + JWT auth |
| `web` | `saebooks/saebooks-web` | `8080` | Jinja/HTMX UI |

The `api` is the licensed product. The web layer is a thin client over
the REST surface; you can swap it out, run multiple, or skip it
entirely if you only need the API.

## 1. Prerequisites

* Linux host (any distro that ships modern Docker — tested on Debian
  12, Ubuntu 22.04+, NixOS unstable).
* Docker Engine 24+ and the Compose v2 plugin
  (`docker compose version` should print v2.x).
* 2 GB RAM minimum, 4 GB recommended. The Postgres container is the
  heavyweight; the API and web layers are sub-200 MB each.
* Outbound HTTPS (for image pulls and, if you enable them, bank-feed
  and AI integrations).

## 2. Grab the bundle

The recommended path is the GitHub Release bundle — it's pinned to a
known image tag and the secrets file is annotated:

```bash
mkdir saebooks && cd saebooks

# Replace v0.1.2 with the latest release tag.
RELEASE=v0.1.2

curl -L -o docker-compose.yml \
  https://github.com/saebooks/saebooks/releases/download/$RELEASE/docker-compose.yml
curl -L -o .env.example \
  https://github.com/saebooks/saebooks/releases/download/$RELEASE/.env.example

cp .env.example .env
```

Alternatively, clone the repo:

```bash
git clone https://github.com/saebooks/saebooks.git
cd saebooks
cp .env.example .env
```

## 3. Configure secrets

Open `.env` and set, at minimum:

```dotenv
POSTGRES_PASSWORD=<openssl rand -base64 24>
SAEBOOKS_FIELD_ENCRYPTION_KEY=<openssl rand -base64 32>
SAEBOOKS_SECRET_KEY=<openssl rand -base64 32>
SAEBOOKS_WEB_SECRET_KEY=<openssl rand -base64 32>
SAEBOOKS_SQL_RO_PASSWORD=<openssl rand -base64 24 | tr -d '/+=' | head -c 32>
```

Why each one matters:

| Variable | What it protects |
|---|---|
| `POSTGRES_PASSWORD` | Ledger database superuser. |
| `SAEBOOKS_FIELD_ENCRYPTION_KEY` | At-rest encryption for sensitive columns (API tokens, OAuth refresh tokens, bank-feed creds). Lose this and those rows are unreadable. |
| `SAEBOOKS_SECRET_KEY` | Signs JWT bearer tokens. Rotating it logs every user out; losing it briefly invalidates outstanding sessions. |
| `SAEBOOKS_WEB_SECRET_KEY` | Signs the web UI's session cookies. |
| `SAEBOOKS_SQL_RO_PASSWORD` | Read-only Postgres role used by the in-app SQL console. Required for migration 0087 to apply. |

**Back these up to a password manager before going live.** Restoring a
database backup without `SAEBOOKS_FIELD_ENCRYPTION_KEY` is a partial
restore — encrypted columns will not decrypt.

Optional but useful settings further down `.env`:

* `SEED_COMPANY_*` — populates the first company on first boot.
* `SMTP_*` — outbound email (verification links, password resets,
  invoice emailing). Without these, the verification flow falls back to
  log-only.
* `SENTRY_DSN` — error reporting; leave empty for self-hosters who don't
  want telemetry.

## 4. Start the stack

```bash
docker compose up -d
docker compose logs -f api  # watch migrations apply
```

First boot runs Alembic migrations against an empty database. Expect
30–60 seconds; the `api` healthcheck will go green when migrations
finish. If it never goes green, the logs will tell you which migration
choked — usually a missing or empty required env var.

## 5. Bootstrap the first owner

The shipped database has zero users. Mint the first owner:

```bash
docker compose exec api \
  python -m saebooks.cli bootstrap-admin --email you@example.com
```

This:

1. Refuses to run if any user rows already exist (idempotency guard;
   override with `--force`).
2. Creates the Default tenant if migrations didn't seed it.
3. Creates a single user with role `owner`, email pre-verified.
4. Mints a 30-day JWT bearer token and prints it to stdout.

Copy the printed token — that's your first credential. Use it as a
`Authorization: Bearer …` header against the REST API, or paste it into
the desktop client's first-run wizard. Set a password through
`/auth/change-password` once you can log in via the UI; the token will
keep working until its TTL expires.

A typical session right after bootstrap:

```bash
TOKEN=eyJhbGciOi…   # from bootstrap-admin output
curl -H "Authorization: Bearer $TOKEN" http://localhost:8042/api/v1/companies
```

## 6. Open the UI

Browse to <http://localhost:8080>. Log in with the email from step 5;
the web layer detects the bearer-token session and lets you in.

## 7. Put it behind a reverse proxy (recommended)

Don't expose `:8080` to the public internet without TLS. Caddy is the
shortest path — drop this in `/etc/caddy/Caddyfile`:

```caddy
books.example.com {
  reverse_proxy localhost:8080
}

books-api.example.com {
  reverse_proxy localhost:8042
}
```

For nginx + Let's Encrypt, see the
[`docs/reverse-proxy/`](./docs/reverse-proxy/) examples (coming with
v0.2). A single hostname is fine if you only need the web UI; expose
the REST API only if you intend to use the desktop client or third-party
integrations.

## 8. Backups

A nightly Postgres dump + the `.env` file is enough to rebuild from
scratch:

```bash
# /etc/cron.daily/saebooks-backup
docker compose exec -T db pg_dump -U saebooks saebooks \
  | gzip > /var/backups/saebooks-$(date +%F).sql.gz

cp .env /var/backups/saebooks-env-$(date +%F)
```

Restore is the inverse: `gunzip < dump.sql.gz | docker compose exec -T
db psql -U saebooks saebooks` against a clean stack with the same
`SAEBOOKS_FIELD_ENCRYPTION_KEY`.

## 9. Upgrades

```bash
# Edit docker-compose.yml or .env to bump SAEBOOKS_TAG=v0.1.3
docker compose pull
docker compose up -d
docker compose logs -f api  # watch migrations
```

Migrations are forward-only — there is no automatic downgrade. Always
take a backup before upgrading. Skip-version upgrades (e.g. v0.1.0 →
v0.1.3) work, but read the [CHANGELOG](./CHANGELOG.md) first; some
tags include schema migrations that take noticeable time on large
databases.

## 10. Hardening checklist

Before you put real data in:

- [ ] Reverse proxy with TLS — never expose port 8080/8042 directly.
- [ ] Firewall blocks inbound on the host except the proxy ports.
- [ ] Secrets in `.env` backed up to a password manager.
- [ ] Postgres backups running and verified (restore into a scratch
      container monthly).
- [ ] `SAEBOOKS_LOG_JSON=true` if you ship logs to a central collector.
- [ ] `SAEBOOKS_SECRET_KEY` is at least 32 bytes of entropy and is not
      shared with any other service.
- [ ] `SENTRY_DSN` left empty unless you've reviewed what it sends.

## Troubleshooting

**`bootstrap-admin` refuses with "users already exist"** — that's the
idempotency guard. If you've forgotten the original owner's password,
use the password-reset flow rather than `--force`.

**`bootstrap-admin` exits with "SAEBOOKS_SECRET_KEY is not set"** —
the printed token would be unverifiable. Set the env var, restart the
api container (`docker compose up -d api`), then rerun the command.

**API healthcheck never goes green** — `docker compose logs api`. The
most common cause is a missing required `.env` value: the compose file
uses `${VAR:?…}` syntax which exits 1 with a clear message.

**Web UI shows a 502** — the api container isn't healthy yet, or the
web container started before the api. Wait 30 s and retry; the web
healthcheck will recover on its own.

**Tokens stop working after a restart** — `SAEBOOKS_SECRET_KEY` is
empty. With no value set, the API generates an ephemeral key per
process and every restart invalidates every outstanding token. Set the
env var.

## Going further

* Multi-company, fixed-asset register, advanced reporting →
  `SAEBOOKS_EDITION=business` (requires a licence key).
* Bank-feed ingestion, STP lodgement, AI document extraction →
  `SAEBOOKS_EDITION=pro`.
* Source build / hacking on the code → see
  [`deploy/sap/docker-compose.yml`](./deploy/sap/docker-compose.yml).
* Roadmap, scope, what we will and won't build →
  [CHARTER.md](./CHARTER.md).

Questions, bug reports, or war stories: open a thread on
[github.com/saebooks/saebooks/issues](https://github.com/saebooks/saebooks/issues)
or email <hello@saee.com.au>.

# saebooks-group — Intercompany Relay Broker (money-free)

> REVIEW BRANCH (`feat/ic-remote-relay-live`). **Do NOT `docker compose up`**
> until Richard signs off the go-live. This is the first cross-tenant WRITE path.

## What it is

The broker for the cross-DB intercompany REMOTE relay (plan
`~/.claude/plans/saebooks-remote-relay-plan.md`, §2). A tiny, separate stack
(`relay` FastAPI + `db` Postgres `saebooks_group`) that:

- registers per-edge **pairs** (public keys + token *hashes* — never private
  keys, never cleartext, never money);
- logs **relay** deliveries (routing metadata + signature fingerprint);
- verifies the originator's Ed25519 signature and forwards the SAME signed
  envelope to the partner's `/ic/accept`.

## The "never holds money" invariant (enforced 3 ways)

1. its schema has **no** GL table (no `accounts`, `journal_entries`,
   `journal_lines`, `ic_txn`, `ic_legs`) — only `pair_registry` + `relay_log`;
2. its code imports **no** posting service (`saebooks.services.journal` /
   `intercompany`) — it reuses only the crypto-pure `ic_relay.signing`;
3. CI asserts both (`tests/test_broker_money_free.py`).

## Phases

- **3b (this branch):** stack + app + own alembic (`0001_broker_init`) +
  `POST /ic/pairs`, `GET /ic/pairs`, `GET /ic/relay-log`. `POST /ic/relay`
  returns **501** (forwarding off via `SAEBOOKS_GROUP_RELAY_FORWARDING_ENABLED`).
- **3c (this branch, flag-gated):** `/ic/relay` forwards live when the broker's
  `SAEBOOKS_GROUP_RELAY_FORWARDING_ENABLED=true` AND each tenant's
  `SAEBOOKS_IC_REMOTE_RELAY_ENABLED=true`. Both default **false**.

## Network

Internal docker network only (`ic_relay`, `external: true`). **No public edge.**
Cross-WAN relay (Estonia filiaal) would later ride Tailscale, not Cloudflare.

## Go-live (NOT done on this branch)

1. `bw-secret` → `GROUP_DB_PASSWORD` into `.env`.
2. Create the shared `ic_relay` docker network; join the tenant api stacks to it.
3. `docker compose build && docker compose up -d` (DB migrates on boot).
4. Wire per-edge token cleartext into the broker secret store + dispatcher
   (`_resolve_dst_token` / `_ProdBrokerFactory.resolve_token` seams).
5. Flip `SAEBOOKS_GROUP_RELAY_FORWARDING_ENABLED=true` on the broker and
   `SAEBOOKS_IC_REMOTE_RELAY_ENABLED=true` on the named tenant stacks.

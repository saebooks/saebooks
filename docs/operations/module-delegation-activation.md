# Delegated-module activation (capture / preaccounting / platform)

Status: **containers defined, delegation OFF** (branch
`feat/m2-delegated-containers`, M2 wave 2b, #32 P0c, 2026-07-10).

## What this commit does NOT do

`docker-compose.yml` now has `capture`, `preaccounting`, and `platform`
service definitions, but:

- They are gated behind the Compose **`modules` profile**. A plain
  `docker compose up` starts only `db` + `app` — byte-for-byte the same
  as before this wave. Verified with `docker compose config --services`.
- Even `docker compose --profile modules up` does **not** turn on
  delegation. The engine (`app`) has `CAPTURE_BASE_URL` /
  `PREACCOUNTING_BASE_URL` / `PLATFORM_BASE_URL` left unset, and
  `saebooks/config.py` treats an empty base URL as "run this module
  in-process" — the default, zero-behaviour-change path. Every module
  container that does start under the profile stays reachable but idle:
  its own inbound token (`CAPTURE_TOKEN` / `PREACCOUNTING_TOKEN` /
  `PLATFORM_TOKEN`) is also unset by default, which fail-closes the
  module's `/module/<name>/*` surface with a 503
  (`capture_app/deps.py`, `preaccounting_app/deps.py`,
  `platform_app/deps.py` — "module disabled: X_TOKEN is not configured").

Turning delegation on is a **deliberate, separate, Richard-controlled
step**. It is not part of a routine deploy and must not be flipped as a
side effect of anything else.

## Two distinct secret mechanisms — do not conflate

1. **Per-module request-auth token pair.** The engine sends
   `X_SERVICE_TOKEN` as a header (`X-Capture-Token` /
   `X-PreAccounting-Token` / `X-Platform-Token`); the module compares it
   constant-time against its own `X_TOKEN`. The two must be the SAME
   value across the deploy. Gates every module route.
2. **`SAEBOOKS_SECRET_KEY` (platform module only).** The platform module
   mints JWTs (login / webauthn-assert / principal-login ceremonies); the
   engine verifies them. Both sign with HS256 over
   `SAEBOOKS_SECRET_KEY`, so the two containers must hold the IDENTICAL
   value. This is checked by an engine-startup preflight —
   `saebooks/services/platform_client.py:verify_key_parity_or_disable()`
   — which asks the module to mint a probe JWT and verifies it under the
   engine's own key. On any mismatch or unreachable module it
   **auto-disables delegation** (fails open to in-process, never
   fail-broken) and logs a loud error. Sharing one `.env` file across
   `app` and `platform`, as the current compose does, satisfies this
   automatically once activated — no separate wiring needed for
   mechanism 2 beyond that shared `.env`.

## Exact activation steps (future gated deploy)

1. Pick one shared secret per module and set it on BOTH sides:

   | Module         | Module container env | Engine (`app`) env               |
   |----------------|-----------------------|-----------------------------------|
   | capture        | `CAPTURE_TOKEN`       | `CAPTURE_SERVICE_TOKEN` (same value) |
   | preaccounting  | `PREACCOUNTING_TOKEN` | `PREACCOUNTING_SERVICE_TOKEN` (same value) |
   | platform       | `PLATFORM_TOKEN`      | `PLATFORM_SERVICE_TOKEN` (same value) |

   Generate each with e.g. `openssl rand -base64 24 | tr -d '/+=' | head -c 32`.

2. Confirm `SAEBOOKS_SECRET_KEY` is set and identical on `app` and
   `platform` (already true if both read the same `.env`).

3. Point the engine at each module's in-cluster service name (leave the
   module containers' own `*_BASE_URL` unset — setting one would make a
   module recurse into its own delegation path):

   ```
   app: CAPTURE_BASE_URL=http://capture:8080
   app: PREACCOUNTING_BASE_URL=http://preaccounting:8080
   app: PLATFORM_BASE_URL=http://platform:8080
   ```

4. `docker compose --profile modules up -d` to start the module
   containers, then restart `app` so it re-reads the new
   `*_BASE_URL` / `*_SERVICE_TOKEN` values and runs the platform
   key-parity preflight.

5. Verify: check `app` startup logs for "platform keycheck OK" (if
   platform was activated) and exercise one delegated call per module to
   confirm a 2xx round-trip, not a fallback to in-process.

Each of steps 1–3 is fully reversible: unset the `*_BASE_URL` var on
`app` and the in-process path returns immediately, no restart of the
module container required.

## Known gap for the live-deploy step (not fixed here)

`app` bind-mounts `./saebooks` (live source) in `docker-compose.yml`,
but the `capture` / `preaccounting` / `platform` images bake the
`saebooks` + module-app source in at **build time** (`COPY` in each
Dockerfile, no bind mount). At the gated live-deploy, the module images
must be rebuilt whenever engine code they share changes, or the module
containers will silently run stale code against the same database the
engine writes to. This wave defines the containers only; keeping
module-image builds in lockstep with engine code is a live-deploy
concern, not solved by this commit.

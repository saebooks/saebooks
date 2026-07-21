.PHONY: up down build migrate revision seed test lint typecheck shell logs psql backup restore-test conformance

# ATO conformance harness — everything runnable WITHOUT an SSID, host-side
# (no docker stack; the tree is pure). Always-on: TPAR BDE golden files.
# Env-gated on the DSP pack directory (skips cleanly when absent): AS.0004 /
# PAYEVNT.0004 / TPAR.0003 official-scenario round-trips + EVTE keystore
# signing round-trips. The still-blocked surface (SSID, DSPPT-52161) is
# reported by the lodge-server's tests/test_activation_readiness.py, not here.
SBR_CONFORMANCE_DIR ?= $(HOME)/scratch/ato-sbr
LODGE_DIR ?= $(HOME)/projects/lodge-ebms3
conformance:
	@if [ -d "$(SBR_CONFORMANCE_DIR)" ]; then \
		echo "conformance packs: $(SBR_CONFORMANCE_DIR)"; \
	else \
		echo "⚠ conformance packs NOT found at $(SBR_CONFORMANCE_DIR) — official-scenario round-trips and EVTE keystore tests will SKIP (golden files still run)"; \
	fi
	SAEBOOKS_ENV=test SBR_CONFORMANCE_DIR="$(SBR_CONFORMANCE_DIR)" \
		uv run --extra dev pytest tests_conformance/ -rs
	@if [ -d "$(LODGE_DIR)" ]; then \
		echo ""; echo "lodge-server suite ($(LODGE_DIR)):"; \
		cd "$(LODGE_DIR)" && SBR_CONFORMANCE_DIR="$(SBR_CONFORMANCE_DIR)" \
			uv run --extra dev pytest tests/ -q -rs; \
	else \
		echo "⚠ lodge-server checkout not found at $(LODGE_DIR) — its envelope/signing/readiness suite was NOT run"; \
	fi
	@echo ""
	@echo "Blocked on SSID (DSPPT-52161): live EVTE canary + activation-gated"
	@echo "readiness tests — see lodge-ebms3 tests/test_activation_readiness.py"
	@echo "and ~/records/saebooks/bas-activation-runbook-2026-07-21.md"

COMPOSE := docker compose
APP := $(COMPOSE) exec app

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

build:
	$(COMPOSE) build

migrate:
	$(APP) alembic upgrade head

revision:
	$(APP) alembic revision -m "$(m)"

seed:
	$(APP) python -m saebooks.seed.load_au_coa

test:
	$(APP) pytest -q --asyncio-mode=auto

lint:
	$(APP) ruff check .

typecheck:
	$(APP) mypy saebooks

shell:
	$(APP) bash

logs:
	$(COMPOSE) logs -f --tail=100

psql:
	$(COMPOSE) exec db psql -U saebooks -d saebooks

# Run a pg_dump backup right now (normally the systemd timer does this nightly).
backup:
	./scripts/backup.sh

# Restore the latest dump into a scratch DB and verify row counts.
restore-test:
	./scripts/restore-test.sh

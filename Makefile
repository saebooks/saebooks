.PHONY: up down build migrate revision seed test lint typecheck shell logs psql

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
	$(APP) pytest -q

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

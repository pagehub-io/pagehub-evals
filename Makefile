# Makefile for pagehub-evals

.PHONY: help up down logs test test-api lint fmt type-check install

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

PAGEHUB_EVALS_TEST_DATABASE_URL ?= postgres://postgres:postgres@127.0.0.1:5533/pagehub_evals

help:
	@echo "pagehub-evals - Development Commands"
	@echo ""
	@echo "  up         Start dev stack (db + api) via Docker Compose"
	@echo "  down       Stop dev stack"
	@echo "  logs       Tail Docker logs"
	@echo "  test       Run backend pytest suite (api/tests/)"
	@echo "  test-api   Alias for test"
	@echo "  lint       Run ruff over api/"
	@echo "  fmt        Run ruff --fix over api/"
	@echo "  type-check Run TypeScript --noEmit over mobile/"
	@echo "  install    Create venv and install python deps"

up:
	@echo "Starting development environment..."
	@if [ ! -d "$(VENV)" ]; then \
		echo "Creating virtual environment..."; \
		python3 -m venv $(VENV); \
		$(PIP) install --upgrade pip -q; \
		$(PIP) install -r requirements.txt -q; \
		echo "  -> Virtual environment created"; \
	fi
	@docker compose up -d
	@echo ""
	@echo "Services:"
	@echo "  Database:  postgres://postgres:postgres@127.0.0.1:5533/pagehub_evals"
	@echo "  API:       http://localhost:8002"
	@echo "  Docs:      http://localhost:8002/docs"

down:
	@docker compose down

logs:
	@docker compose logs -f

test test-api:
	@if [ ! -f "$(VENV)/bin/pytest" ]; then \
		python3 -m venv $(VENV); \
		$(PIP) install --upgrade pip -q; \
		$(PIP) install -r requirements.txt -q; \
		$(PIP) install pytest pytest-asyncio pytest-cov -q; \
	fi
	@PAGEHUB_EVALS_TEST_DATABASE_URL=$(PAGEHUB_EVALS_TEST_DATABASE_URL) \
		$(VENV)/bin/pytest api/tests/ -v --tb=short

lint:
	@if [ ! -f "$(VENV)/bin/ruff" ]; then $(PIP) install ruff -q; fi
	@$(VENV)/bin/ruff check api/

fmt:
	@if [ ! -f "$(VENV)/bin/ruff" ]; then $(PIP) install ruff -q; fi
	@$(VENV)/bin/ruff check --fix api/

type-check:
	@cd mobile && npx tsc --noEmit

install:
	@python3 -m venv $(VENV)
	@$(PIP) install --upgrade pip -q
	@$(PIP) install -r requirements.txt -q
	@$(PIP) install pytest pytest-asyncio pytest-cov ruff -q
	@echo "Installed deps into $(VENV)"

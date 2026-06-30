# ════════════════════════════════════════════════════════════════════════
#  AegisOps — developer Makefile.
#  Requires GNU make (Windows: run under Git Bash / WSL, or `choco install make`).
# ════════════════════════════════════════════════════════════════════════
-include .env
export

API_HOST ?= 0.0.0.0
API_PORT ?= 8000
COMPOSE  ?= docker compose

.DEFAULT_GOAL := help
.PHONY: help up up-full down logs ps migrate seed dev dev-api dev-web \
        install build test test-cov lint fmt e2e reset

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# ── Stack ────────────────────────────────────────────────────────────────
up: ## Start backing services (PG, Redis, Neo4j, Keycloak, Langfuse, OTel, Prometheus, Grafana)
	$(COMPOSE) up -d
	$(COMPOSE) ps

up-full: ## Start everything incl. containerized api + frontend
	$(COMPOSE) --profile full up -d --build
	$(COMPOSE) ps

down: ## Stop services (keep volumes/data)
	$(COMPOSE) down

logs: ## Tail service logs
	$(COMPOSE) logs -f

ps: ## Show service status
	$(COMPOSE) ps

# ── Setup / build ────────────────────────────────────────────────────────
install: ## Install backend + frontend dependencies
	cd backend && pip install -e ".[dev]"
	cd frontend && npm install

build: ## Build api + frontend Docker images
	$(COMPOSE) --profile full build

# ── Data ─────────────────────────────────────────────────────────────────
migrate: ## Run Alembic migrations + Neo4j schema
	cd backend && alembic upgrade head
	cd backend && python -m app.graph_db.schema

seed: ## Load real initial data
	cd backend && python -m seed.seed

# ── Run (host) ───────────────────────────────────────────────────────────
dev: ## Run backend + frontend together (Ctrl-C stops both)
	@echo "Starting API (:$(API_PORT)) and frontend (:3000)…"
	@( cd backend && uvicorn app.main:app --reload --host $(API_HOST) --port $(API_PORT) ) & \
	 ( cd frontend && npm run dev ) & \
	 wait

dev-api: ## Run backend only (uvicorn :8000)
	cd backend && uvicorn app.main:app --reload --host $(API_HOST) --port $(API_PORT)

dev-web: ## Run frontend only (next :3000)
	cd frontend && npm run dev

# ── Quality ──────────────────────────────────────────────────────────────
test: ## Backend pytest + frontend vitest
	cd backend && pytest -q
	cd frontend && npm test

test-cov: ## Backend tests with coverage
	cd backend && pytest --cov=app --cov-report=term-missing

lint: ## Lint backend (ruff) + frontend (eslint)
	cd backend && ruff check .
	cd frontend && npm run lint

fmt: ## Format/auto-fix backend
	cd backend && ruff format . && ruff check --fix .

e2e: ## Playwright E2E (needs the app running via `make dev`)
	cd frontend && npx playwright test

# ── Danger ───────────────────────────────────────────────────────────────
reset: ## Stop and DELETE all volumes/data (destructive)
	$(COMPOSE) down -v
	@echo "All AegisOps volumes removed."

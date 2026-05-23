.PHONY: help \
        venv install dev run test lint fmt typecheck \
        migrate revision \
        dev-db-up dev-db-down dev-db-reset dev-db-status \
        compose-up compose-down \
        k8s-up k8s-down k8s-status k8s-forward k8s-smoke k8s-logs \
        docker-build docker-push smoke

PY ?= python3.12
VENV := .venv
PIP := $(VENV)/bin/pip
PYTHON := $(VENV)/bin/python

IMAGE ?= ghcr.io/bugbytz/m4l-telemetry-api
TAG   ?= dev

help:  ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[1;36m%-22s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Python / app
# ---------------------------------------------------------------------------
$(VENV)/bin/activate:
	$(PY) -m venv $(VENV)

venv: $(VENV)/bin/activate ## Create local virtualenv

install: venv ## Install runtime deps
	$(PIP) install -U pip
	$(PIP) install -r requirements.txt

dev: venv ## Install runtime + dev deps
	$(PIP) install -U pip
	$(PIP) install -r requirements-dev.txt

run: ## Run the API with auto-reload
	$(VENV)/bin/uvicorn app.main:app --reload --port 8080

test: ## Run the contract tests (no DB required)
	$(VENV)/bin/pytest -q

lint: ## Ruff lint
	$(VENV)/bin/ruff check .

fmt: ## Ruff autoformat + import sort
	$(VENV)/bin/ruff format .
	$(VENV)/bin/ruff check . --fix

typecheck: ## Mypy
	$(VENV)/bin/mypy app

migrate: ## Apply migrations against $$TELEMETRY_DATABASE_URL
	$(VENV)/bin/alembic upgrade head

revision: ## make revision m="describe the change"
	$(VENV)/bin/alembic revision --autogenerate -m "$(m)"

# ---------------------------------------------------------------------------
# Local Postgres via Homebrew (no Kubernetes)
# ---------------------------------------------------------------------------
dev-db-up: ## Install (if needed) + start Postgres via Homebrew
	@scripts/dev-db.sh up

dev-db-down: ## Stop the launchd Postgres service (data preserved)
	@scripts/dev-db.sh down

dev-db-reset: ## Drop + recreate the telemetry database
	@scripts/dev-db.sh reset

dev-db-status: ## Show Homebrew Postgres status
	@scripts/dev-db.sh status

# ---------------------------------------------------------------------------
# Container parity (docker-compose) -- requires docker
# ---------------------------------------------------------------------------
compose-up: ## docker-compose up (postgres + migrate + api on :8080)
	docker compose up --build

compose-down: ## docker-compose down -v
	docker compose down -v

# ---------------------------------------------------------------------------
# Kubernetes (kind + CloudNativePG + MinIO)
# ---------------------------------------------------------------------------
k8s-up: ## Bring up the full stack on a local kind cluster (~3-5 min cold)
	@scripts/local-bootstrap.sh up

k8s-down: ## Delete the local kind cluster
	@scripts/local-bootstrap.sh down

k8s-status: ## Show pods/svcs in the telemetry namespace
	@scripts/local-bootstrap.sh status

k8s-forward: ## kubectl port-forward the API on :8080 (Ctrl-C to stop)
	@scripts/local-bootstrap.sh forward

k8s-smoke: ## POST sample.ndjson at the running API (must be port-forwarded)
	@scripts/local-bootstrap.sh smoke

k8s-logs: ## Tail API logs
	kubectl -n telemetry logs -f -l app.kubernetes.io/name=m4l-telemetry-api

# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------
docker-build: ## Build the runtime image
	docker build -t $(IMAGE):$(TAG) .

docker-push: docker-build ## Push to your registry
	docker push $(IMAGE):$(TAG)

smoke: ## Curl the sample ndjson at a running API on :8080
	curl -sS -X POST \
	    -H 'Content-Type: application/x-ndjson' \
	    --data-binary @tests/fixtures/sample.ndjson \
	    http://localhost:8080/v1/events | python3 -m json.tool

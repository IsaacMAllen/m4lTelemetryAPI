.PHONY: help \
        venv install dev run test lint fmt typecheck \
        migrate revision \
        dev-db-up dev-db-down dev-db-reset dev-db-status \
        compose-up compose-down \
        k8s-up k8s-down k8s-status k8s-forward k8s-forward-bg k8s-forward-stop k8s-forward-status k8s-smoke k8s-logs \
        psql events-count events-tail events-watch events-stats events-truncate \
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

k8s-forward: ## kubectl port-forward the API on :8080 (foreground, Ctrl-C to stop)
	@scripts/local-bootstrap.sh forward

PF_PID  := /tmp/m4l-portfwd.pid
PF_LOG  := /tmp/m4l-portfwd.log

k8s-forward-bg: ## Start port-forward in the background (survives shell exit)
	@if [ -f $(PF_PID) ] && kill -0 $$(cat $(PF_PID)) 2>/dev/null; then \
	    echo "port-forward already running (pid $$(cat $(PF_PID)))"; \
	else \
	    nohup kubectl -n telemetry port-forward svc/m4l-telemetry-api 8080:80 \
	        > $(PF_LOG) 2>&1 & echo $$! > $(PF_PID); \
	    sleep 2; \
	    if kill -0 $$(cat $(PF_PID)) 2>/dev/null; then \
	        echo "port-forward started (pid $$(cat $(PF_PID)), log $(PF_LOG))"; \
	        echo "endpoint: http://localhost:8080/v1/events"; \
	    else \
	        echo "FAILED to start port-forward; see $(PF_LOG)"; exit 1; \
	    fi; \
	fi

k8s-forward-stop: ## Stop the backgrounded port-forward
	@if [ -f $(PF_PID) ]; then \
	    kill $$(cat $(PF_PID)) 2>/dev/null && echo "stopped pid $$(cat $(PF_PID))" || echo "not running"; \
	    rm -f $(PF_PID); \
	else \
	    echo "no $(PF_PID); nothing to stop"; \
	fi

k8s-forward-status: ## Is the backgrounded port-forward alive?
	@if [ -f $(PF_PID) ] && kill -0 $$(cat $(PF_PID)) 2>/dev/null; then \
	    echo "RUNNING (pid $$(cat $(PF_PID)))"; \
	    curl -sS -m 2 -o /dev/null -w "  /healthz -> HTTP %{http_code}\n" http://localhost:8080/healthz || echo "  /healthz unreachable"; \
	else \
	    echo "NOT RUNNING.  Start with: make k8s-forward-bg"; \
	fi

k8s-smoke: ## POST sample.ndjson at the running API (must be port-forwarded)
	@scripts/local-bootstrap.sh smoke

k8s-logs: ## Tail API logs
	kubectl -n telemetry logs -f -l app.kubernetes.io/name=m4l-telemetry-api

# ---------------------------------------------------------------------------
# Database inspection (talks to the in-cluster Postgres via kubectl exec)
# ---------------------------------------------------------------------------
# Pull the password from CNPG's auto-generated Secret and run psql inside
# the primary pod.  The variable expansion `$$( ... )` defers to make-time
# rather than running at parse time.
PG_POD := m4l-telemetry-pg-1
PG_NS  := telemetry
PG_EXEC = kubectl -n $(PG_NS) exec -i $(PG_POD) -c postgres -- \
    env PGPASSWORD=$$(kubectl -n $(PG_NS) get secret m4l-telemetry-pg-app \
        -o jsonpath='{.data.password}' | base64 -d) \
    psql -h localhost -U telemetry -d telemetry

psql: ## Open an interactive psql shell against the cluster Postgres
	@kubectl -n $(PG_NS) exec -it $(PG_POD) -c postgres -- \
	    env PGPASSWORD=$$(kubectl -n $(PG_NS) get secret m4l-telemetry-pg-app \
	        -o jsonpath='{.data.password}' | base64 -d) \
	    psql -h localhost -U telemetry -d telemetry

events-count: ## Print total events
	@$(PG_EXEC) -tAc "SELECT count(*) FROM events;"

events-tail: ## Show the 20 most recent events (compact)
	@$(PG_EXEC) -c "\x off" -c "SELECT to_char(ts, 'YYYY-MM-DD HH24:MI:SS') AS ts, kind::text, level::text, vendor, device_name, device_version, name, COALESCE(message,'') AS message, props FROM events ORDER BY ts DESC LIMIT 20;"

events-watch: ## Live-tail new events (refreshes every 2s; Ctrl-C to stop)
	@trap 'exit 0' INT; \
	while true; do \
	    clear; \
	    printf "\033[1;36m[events-watch]\033[0m  refreshing every 2s  \033[2m(Ctrl-C to stop)\033[0m\n"; \
	    date '+%Y-%m-%d %H:%M:%S'; \
	    echo; \
	    $(PG_EXEC) -c "\x off" \
	        -c "SELECT to_char(received_at, 'HH24:MI:SS') AS recv, kind::text, level::text, vendor, device_name, name, COALESCE(message,'') AS message FROM events ORDER BY received_at DESC LIMIT 15;" \
	        2>/dev/null; \
	    sleep 2; \
	done

events-stats: ## Event counts grouped by kind/level/device
	@$(PG_EXEC) -c "SELECT kind::text, level::text, vendor, device_name, count(*) FROM events GROUP BY 1,2,3,4 ORDER BY 5 DESC;"

events-truncate: ## DELETE all events (useful for resetting between smoke tests)
	@printf "About to DELETE all events.  Press Enter to confirm, Ctrl-C to abort: " && read _ \
	    && $(PG_EXEC) -c "TRUNCATE events;" \
	    && echo "events table cleared"

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

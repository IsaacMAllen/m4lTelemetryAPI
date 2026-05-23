#!/usr/bin/env bash
# scripts/local-bootstrap.sh -- bring up the entire stack on a Mac
# from zero: OrbStack -> kind -> CNPG -> MinIO -> Postgres -> API.
#
# Usage:
#   scripts/local-bootstrap.sh up        # default
#   scripts/local-bootstrap.sh down      # delete the kind cluster
#   scripts/local-bootstrap.sh forward   # kubectl port-forward the API on :8080
#   scripts/local-bootstrap.sh smoke     # send sample.ndjson at the running API
#   scripts/local-bootstrap.sh status    # show what's running
#
# Idempotent: re-running `up` reconciles state; existing resources are kept.
#
# Why these tools
#   - OrbStack: best macOS container runtime (free for personal use).
#     Falls back to Colima if the user prefers FOSS-only.
#   - kind: cluster-in-Docker, mature, well-supported.
#   - CloudNativePG: see ARCHITECTURE.md.
#   - MinIO: in-cluster S3-compatible backup target.

set -euo pipefail

readonly CLUSTER_NAME="m4l-telemetry"
readonly NAMESPACE="telemetry"
readonly CNPG_VERSION="1.24.0"
readonly KIND_NODE_IMAGE="kindest/node:v1.31.0"
readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '\033[1;34m[bootstrap]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bootstrap]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[bootstrap]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Step 1 -- container runtime
# ---------------------------------------------------------------------------
ensure_runtime() {
  if docker info >/dev/null 2>&1; then
    ok "container runtime is up: $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown)"
    return
  fi

  log "no container runtime detected; installing OrbStack via Homebrew (free for personal use)"
  if ! command -v brew >/dev/null 2>&1; then
    fail "Homebrew is required.  Install from https://brew.sh and re-run."
  fi

  if ! brew list --cask 2>/dev/null | grep -qx orbstack; then
    brew install --cask orbstack
  fi

  log "starting OrbStack and waiting for the Docker socket..."
  open -ga OrbStack
  for _ in {1..60}; do
    if docker info >/dev/null 2>&1; then
      ok "OrbStack is up"
      return
    fi
    sleep 2
  done
  fail "OrbStack did not finish starting in 2 minutes.  Open it manually and re-run."
}

# ---------------------------------------------------------------------------
# Step 2 -- CLI tools
# ---------------------------------------------------------------------------
ensure_tool() {
  local tool=$1 formula=$2
  if command -v "$tool" >/dev/null 2>&1; then return; fi
  log "installing $tool ($formula) via Homebrew..."
  brew install "$formula"
}

ensure_tools() {
  ensure_tool kubectl kubectl
  ensure_tool kind    kind
}

# ---------------------------------------------------------------------------
# Step 3 -- kind cluster
# ---------------------------------------------------------------------------
ensure_cluster() {
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
    ok "kind cluster '$CLUSTER_NAME' already exists"
  else
    log "creating kind cluster '$CLUSTER_NAME'..."
    kind create cluster \
      --name "$CLUSTER_NAME" \
      --image "$KIND_NODE_IMAGE" \
      --wait 2m
  fi
  kubectl config use-context "kind-$CLUSTER_NAME" >/dev/null
  kubectl cluster-info --context "kind-$CLUSTER_NAME" >/dev/null
}

# ---------------------------------------------------------------------------
# Step 4 -- CloudNativePG operator
# ---------------------------------------------------------------------------
ensure_cnpg() {
  if kubectl get deploy -n cnpg-system cnpg-controller-manager >/dev/null 2>&1; then
    ok "CNPG operator already installed"
  else
    log "installing CloudNativePG ${CNPG_VERSION}..."
    kubectl apply --server-side -f \
      "https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-${CNPG_VERSION%.*}/releases/cnpg-${CNPG_VERSION}.yaml"
  fi
  log "waiting for the operator to be ready..."
  kubectl -n cnpg-system rollout status deploy/cnpg-controller-manager --timeout=180s
}

# ---------------------------------------------------------------------------
# Step 5 -- namespace + MinIO + bucket
# ---------------------------------------------------------------------------
ensure_namespace_and_minio() {
  kubectl apply -f "$REPO_ROOT/k8s/namespace.yaml"
  kubectl apply -f "$REPO_ROOT/k8s/minio/secret.yaml"
  kubectl apply -f "$REPO_ROOT/k8s/minio/deployment.yaml"
  kubectl apply -f "$REPO_ROOT/k8s/minio/service.yaml"
  log "waiting for MinIO to come up..."
  kubectl -n "$NAMESPACE" rollout status deploy/minio --timeout=180s
  kubectl apply -f "$REPO_ROOT/k8s/minio/bucket-init-job.yaml"
  kubectl -n "$NAMESPACE" wait --for=condition=complete \
    job/minio-bucket-init --timeout=120s
  ok "MinIO ready (s3 endpoint: minio.telemetry.svc.cluster.local:9000)"
}

# ---------------------------------------------------------------------------
# Step 6 -- CNPG Cluster + scheduled backup
# ---------------------------------------------------------------------------
ensure_postgres() {
  kubectl apply -f "$REPO_ROOT/k8s/postgres/cluster.yaml"
  log "waiting for the Postgres cluster to bootstrap (this is the slowest step)..."
  # CNPG sets a `cnpg.io/instanceRole=primary` label and a corresponding
  # `Cluster.Status.ReadyInstances` count.  We just wait on the primary pod.
  kubectl -n "$NAMESPACE" wait --for=condition=Ready \
    pod -l cnpg.io/cluster=m4l-telemetry-pg,cnpg.io/instanceRole=primary \
    --timeout=300s
  kubectl apply -f "$REPO_ROOT/k8s/postgres/scheduled-backup.yaml"
  ok "Postgres ready"
}

# ---------------------------------------------------------------------------
# Step 7 -- API image (build + load into kind)
# ---------------------------------------------------------------------------
build_and_load_api() {
  local image="m4l-telemetry-api:dev"
  log "building API image ($image)..."
  ( cd "$REPO_ROOT" && docker build -t "$image" . )
  log "loading image into kind cluster..."
  kind load docker-image "$image" --name "$CLUSTER_NAME"
  echo "$image"
}

apply_api() {
  local image=$1
  # Patch image: the committed deployment.yaml uses the GHCR coordinate;
  # for local kind we substitute the locally-built dev tag.  We pipe
  # through `kubectl set image` after apply so the source manifest stays
  # untouched.
  kubectl apply -f "$REPO_ROOT/k8s/secret.example.yaml"   # tokens (empty)
  kubectl apply -f "$REPO_ROOT/k8s/deployment.yaml"
  kubectl -n "$NAMESPACE" set image deploy/m4l-telemetry-api "api=$image"
  kubectl apply -f "$REPO_ROOT/k8s/service.yaml"
  kubectl apply -f "$REPO_ROOT/k8s/migrate-job.yaml"
  kubectl -n "$NAMESPACE" set image job/m4l-telemetry-migrate "alembic=$image" \
    || true   # if Job already completed, we'll re-create it below

  log "running migration..."
  # Delete any prior completed Job before applying so we don't conflict.
  kubectl -n "$NAMESPACE" delete job m4l-telemetry-migrate --ignore-not-found
  kubectl apply -f "$REPO_ROOT/k8s/migrate-job.yaml"
  kubectl -n "$NAMESPACE" set image job/m4l-telemetry-migrate "alembic=$image"
  kubectl -n "$NAMESPACE" wait --for=condition=complete \
    job/m4l-telemetry-migrate --timeout=180s

  log "waiting for the API to be ready..."
  kubectl -n "$NAMESPACE" rollout status deploy/m4l-telemetry-api --timeout=180s
  ok "API ready"
}

# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------
cmd_up() {
  ensure_runtime
  ensure_tools
  ensure_cluster
  ensure_cnpg
  ensure_namespace_and_minio
  ensure_postgres
  local image
  image="$(build_and_load_api)"
  apply_api "$image"

  cat <<EOF

$(ok "stack is up")

  cluster:    kind-${CLUSTER_NAME}
  api:        kubectl -n ${NAMESPACE} svc/m4l-telemetry-api
  postgres:   kubectl -n ${NAMESPACE} svc/m4l-telemetry-pg-rw
  minio:      kubectl -n ${NAMESPACE} svc/minio (s3 :9000, console :9001)

Next:

  scripts/local-bootstrap.sh forward   # port-forward the API on :8080
  scripts/local-bootstrap.sh smoke     # POST tests/fixtures/sample.ndjson

EOF
}

cmd_down() {
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
    log "deleting kind cluster '$CLUSTER_NAME'..."
    kind delete cluster --name "$CLUSTER_NAME"
  else
    warn "no kind cluster named '$CLUSTER_NAME' to delete"
  fi
}

cmd_forward() {
  log "forwarding 8080 -> svc/m4l-telemetry-api:80 (Ctrl-C to stop)"
  kubectl -n "$NAMESPACE" port-forward svc/m4l-telemetry-api 8080:80
}

cmd_smoke() {
  if ! curl -sS -o /dev/null -w '%{http_code}' http://localhost:8080/healthz | grep -q 200; then
    fail "API not reachable on http://localhost:8080.  Run: scripts/local-bootstrap.sh forward (in another terminal)"
  fi
  log "POSTing tests/fixtures/sample.ndjson..."
  curl -sS -X POST \
    -H 'Content-Type: application/x-ndjson' \
    --data-binary "@${REPO_ROOT}/tests/fixtures/sample.ndjson" \
    http://localhost:8080/v1/events
  echo
}

cmd_status() {
  if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
    warn "kind cluster '$CLUSTER_NAME' does not exist"
    return
  fi
  kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get all
}

main() {
  case "${1:-up}" in
    up)      cmd_up;;
    down)    cmd_down;;
    forward) cmd_forward;;
    smoke)   cmd_smoke;;
    status)  cmd_status;;
    *)       fail "usage: $0 {up|down|forward|smoke|status}";;
  esac
}

main "$@"

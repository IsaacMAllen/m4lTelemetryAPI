#!/usr/bin/env bash
# Local development Postgres via Homebrew.  Use this if you want to hack on
# the API without spinning up a Kubernetes cluster.
#
#   scripts/dev-db.sh up      # install + start + create role/db
#   scripts/dev-db.sh down    # stop the launchd service (data preserved)
#   scripts/dev-db.sh reset   # drop + recreate the telemetry db
#   scripts/dev-db.sh status  # is it running?
#
# After `up` you can:
#   export TELEMETRY_DATABASE_URL=postgresql+asyncpg://telemetry:telemetry@localhost:5432/telemetry
#   make migrate
#   make run
#
# Why a script and not docker-compose: you don't have Docker installed, and
# Homebrew's postgres is the lightest, fastest path on a Mac. We pin to
# postgresql@16 to match what CloudNativePG uses in production.

set -euo pipefail

readonly FORMULA="postgresql@16"
readonly DB_NAME="telemetry"
readonly DB_ROLE="telemetry"
readonly DB_PASSWORD="telemetry"

log() { printf '\033[1;34m[dev-db]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[dev-db]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[dev-db]\033[0m %s\n' "$*" >&2; exit 1; }

require_brew() {
  command -v brew >/dev/null 2>&1 \
    || fail "Homebrew is not installed. See https://brew.sh"
}

ensure_installed() {
  if ! brew list --formula 2>/dev/null | grep -qx "$FORMULA"; then
    log "installing $FORMULA via Homebrew (one-time, ~30s)..."
    brew install "$FORMULA"
  fi
  # Make psql / pg_ctl / etc. available in this shell without needing the
  # user to fiddle with PATH.
  local prefix
  prefix="$(brew --prefix "$FORMULA")"
  export PATH="$prefix/bin:$PATH"
}

start_service() {
  if brew services list | awk -v f="$FORMULA" '$1==f && $2=="started" {found=1} END{exit !found}'; then
    log "$FORMULA already running"
  else
    log "starting $FORMULA via launchd..."
    brew services start "$FORMULA" >/dev/null
    # Wait for the socket to be accepting connections.
    for _ in {1..30}; do
      if pg_isready -h localhost -q; then return; fi
      sleep 0.5
    done
    fail "Postgres did not become ready within 15s"
  fi
}

stop_service() {
  log "stopping $FORMULA..."
  brew services stop "$FORMULA" >/dev/null
}

ensure_role_and_db() {
  # Use the bootstrap superuser — Homebrew's postgres@16 creates a role with
  # the current $USER name as a superuser, with no password and trust auth
  # on localhost.  We use that to create our application role.
  local me; me="$(whoami)"

  if ! psql -U "$me" -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_ROLE'" | grep -q 1; then
    log "creating role '$DB_ROLE'..."
    psql -U "$me" -d postgres -v ON_ERROR_STOP=1 \
      -c "CREATE ROLE $DB_ROLE LOGIN PASSWORD '$DB_PASSWORD';"
  fi

  if ! psql -U "$me" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    log "creating database '$DB_NAME' owned by '$DB_ROLE'..."
    psql -U "$me" -d postgres -v ON_ERROR_STOP=1 \
      -c "CREATE DATABASE $DB_NAME OWNER $DB_ROLE;"
  fi
}

print_dsn() {
  log ""
  log "Postgres is up.  Use this DSN:"
  printf "\n  \033[1;32mexport TELEMETRY_DATABASE_URL=postgresql+asyncpg://%s:%s@localhost:5432/%s\033[0m\n\n" \
    "$DB_ROLE" "$DB_PASSWORD" "$DB_NAME"
  log "Then: make migrate && make run"
}

cmd_up() {
  require_brew
  ensure_installed
  start_service
  ensure_role_and_db
  print_dsn
}

cmd_down() {
  require_brew
  if brew list --formula 2>/dev/null | grep -qx "$FORMULA"; then
    stop_service
  else
    warn "$FORMULA is not installed; nothing to stop"
  fi
}

cmd_reset() {
  require_brew
  ensure_installed
  start_service
  local me; me="$(whoami)"
  warn "dropping database '$DB_NAME' (will be recreated)"
  psql -U "$me" -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS $DB_NAME;" \
    -c "CREATE DATABASE $DB_NAME OWNER $DB_ROLE;"
  log "reset complete"
}

cmd_status() {
  require_brew
  if brew list --formula 2>/dev/null | grep -qx "$FORMULA"; then
    brew services list | awk -v f="$FORMULA" '$1==f {print}'
  else
    warn "$FORMULA not installed; run: scripts/dev-db.sh up"
  fi
}

main() {
  case "${1:-up}" in
    up)     cmd_up;;
    down)   cmd_down;;
    reset)  cmd_reset;;
    status) cmd_status;;
    *)      fail "usage: $0 {up|down|reset|status}";;
  esac
}

main "$@"

# m4l-telemetry-api

Receiver service for events emitted by the [`bz.telemetry`](../live_save_ext_devkit/source/projects/bz.telemetry/) Max for Live external.

It accepts batched newline-delimited JSON over HTTPS, validates each event
against the wire contract, and persists everything into a dedicated Postgres
instance.

```
                       ┌──────────────────────────┐
   bz.telemetry ──────▶│  POST /v1/events         │──┐
   (Max device)        │  ndjson, Bearer auth     │  │
                       └──────────────────────────┘  │
                                                     ▼
                                          ┌────────────────────┐
                                          │  Postgres (CNPG)   │──▶ MinIO / S3
                                          │  events table      │   (WAL + base
                                          └────────────────────┘    backups)
```

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the load-bearing decisions
(why dedicated Postgres, why CloudNativePG, why MinIO, etc.).

## Layout

```
app/                   FastAPI app (routes, models, schemas, ndjson parser, auth)
alembic/               Schema migrations
tests/                 Wire-contract tests + sample.ndjson fixture
scripts/
  dev-db.sh            Homebrew Postgres bootstrap (no-cluster local dev)
  local-bootstrap.sh   Full Mac k8s stack (OrbStack → kind → CNPG → MinIO → API)
k8s/
  cnpg/                CloudNativePG operator install instructions
  postgres/            Cluster + ScheduledBackup
  minio/               Deployment + Service + bucket-init Job
  deployment.yaml      API
  migrate-job.yaml     One-shot Alembic migration
  service.yaml         ClusterIP for the API
  ingress.yaml         Public ingress (cloud only)
  hpa.yaml             HorizontalPodAutoscaler
  pdb.yaml             PodDisruptionBudget
  networkpolicy.yaml   Deny-by-default with explicit allows
  secret.example.yaml  Template for ingest-tokens (DB creds come from CNPG)
Dockerfile             Multi-stage, non-root, distroless-ish runtime
docker-compose.yml     Optional: local stack via Docker (postgres + migrate + api)
Makefile               make help
```

## Pick a workflow

| You want to... | Use |
|---|---|
| Hack on the API quickly, no containers | **Path A** — Homebrew Postgres |
| Try the whole production-shaped stack on your Mac | **Path B** — kind + CNPG |
| You already have Docker installed | `make compose-up` |
| Deploy to a real cluster | **Path C** — production k8s |

---

## Path A — Local API + Homebrew Postgres (fastest iteration)

```bash
make dev-db-up       # installs postgresql@16 via brew, starts launchd, creates role/db
                     # prints the DSN to export

export TELEMETRY_DATABASE_URL=postgresql+asyncpg://telemetry:telemetry@localhost:5432/telemetry
make dev             # creates .venv, installs deps
make migrate         # alembic upgrade head
make run             # uvicorn on :8080
make smoke           # POST tests/fixtures/sample.ndjson  ->  {"accepted":4,...}
```

Tear-down:

```bash
make dev-db-down     # stops the launchd service; data preserved
make dev-db-reset    # drops + recreates the database (fresh state)
```

## Path B — Local Kubernetes (production-shaped)

One command brings up the full stack on a local kind cluster. It will install
**OrbStack** (free for personal use, the best macOS Docker experience),
**kubectl**, **kind**, the **CloudNativePG operator**, **MinIO**, the
**Postgres cluster**, and the **API**:

```bash
make k8s-up          # ~3-5 min cold; idempotent on reruns
make k8s-forward     # in another terminal: port-forward :8080
make k8s-smoke       # POST sample.ndjson
```

Inspect:

```bash
make k8s-status      # all resources in the telemetry namespace
make k8s-logs        # tail API logs
kubectl -n telemetry get cluster m4l-telemetry-pg   # CNPG status
```

Tear-down:

```bash
make k8s-down        # deletes the kind cluster (and all data)
```

## Path C — Real Kubernetes cluster

The `local-bootstrap.sh` script is the source of truth for the apply order;
in a real cluster you'd do the equivalent steps with your own image registry
and ingress.

1. **Install the CNPG operator** (once per cluster). See [`k8s/cnpg/README.md`](k8s/cnpg/README.md).
2. **Build + push the API image.**

   ```bash
   IMAGE=ghcr.io/yourorg/m4l-telemetry-api TAG=$(git rev-parse --short HEAD) make docker-push
   ```

   Update the `image:` field in `k8s/deployment.yaml` and `k8s/migrate-job.yaml`.
3. **Apply MinIO** (or skip and point CNPG at S3 / R2 — edit
   `k8s/postgres/cluster.yaml` `barmanObjectStore` accordingly).
4. **Apply the Postgres `Cluster`** and wait for it to come up. CNPG creates
   the secret `m4l-telemetry-pg-app` automatically.
5. **Create the ingest-tokens Secret.**

   ```bash
   kubectl -n telemetry create secret generic m4l-telemetry-api-tokens \
     --from-literal=ingest-tokens='token-for-livesaver,token-for-other-device'
   ```
6. **Run the migration job, then roll the API.**
7. **Apply the Ingress** with your real hostname and `cert-manager` cluster-issuer.

## Wire contract

`POST /v1/events`

| Field | Required |
|---|---|
| `Content-Type: application/x-ndjson` (or `application/json`) | yes |
| `Authorization: Bearer <token>` | only if `TELEMETRY_INGEST_TOKENS` is set |

Body: one event per line. Per-event JSON shape (matches `bz.telemetry`'s
[`telemetry_core.cpp`](../live_save_ext_devkit/source/projects/bz.telemetry/telemetry_core.cpp) verbatim):

```json
{
  "vendor":         "bugbytz",
  "device_name":    "livesaver",
  "device_version": "2.0.4",
  "device_id":      "uuid-v4",
  "session_id":     "uuid-v4",
  "user_id":        "",
  "platform":       "macOS 14.5.0",
  "max_version":    "Live 12.0.10",

  "type":    "event | error | metric | crash",
  "level":   "info | warning | error | fatal",
  "name":    "device_loaded",
  "ts":      "2026-05-23T15:00:00.000Z",
  "ts_ms":   1779994800000,
  "message": "optional human-readable string",
  "value":   12.3,           // metric only
  "unit":    "ms",           // metric only
  "props":   { "k": "v" }    // optional bag of strings
}
```

Response:

```json
{ "accepted": 32, "rejected": 0, "errors": [] }
```

Status codes match what `bz.telemetry`'s retry logic expects:

| Status | Meaning | Client behaviour |
|---|---|---|
| `200` | accepted (possibly with soft errors) | files deleted from queue |
| `401` `403` | bad / missing token | uploader pauses, surfaces last_error |
| `413` | body or event count too large | batch dropped |
| `422` | body unparseable | batch dropped |
| `5xx` | transient | exponential retry next flush |

## Wiring `bz.telemetry` to it

In a Max patcher:

```
[endpoint https://telemetry.bugbytz.com/v1/events(    →  [bz.telemetry @vendor bugbytz @device livesaver @version 2.0.4]
[token   <whatever-you-put-in-TELEMETRY_INGEST_TOKENS(   ↗
```

For the local kind workflow, **use `127.0.0.1` (not `localhost`)**:

```
[endpoint http://127.0.0.1:8080/v1/events(            →  [bz.telemetry @vendor bugbytz @device livesaver @version 2.0.4]
```

> Why 127.0.0.1 instead of localhost?
> Max's host process enforces App Transport Security (ATS), which blocks
> plain http:// requests. macOS *supposedly* exempts the literal hostname
> `localhost`, but in practice the exemption is unreliable inside hosted
> plugin runtimes — NSURLSession returns `NSURLErrorCannotFindHost`
> ("A server with the specified hostname could not be found") even when
> the local API is healthy. The numeric IP avoids the ATS host-name path
> entirely. For production, use HTTPS and ATS is a non-issue.

(no token needed; the Path B stack ships with auth disabled by default).

## Tests

```bash
make test            # contract + parser unit tests, no DB needed
make k8s-up && make k8s-forward && make k8s-smoke   # end-to-end
```

## Observability

- `/metrics` exposes Prometheus counters + histograms per route (low cardinality).
- `/healthz` is a pure liveness probe; `/readyz` runs `SELECT 1`.
- Logs are JSON to stdout (`python-json-logger`).
- CNPG itself exposes Postgres metrics via `enablePodMonitor: true` (any
  Prometheus operator picks them up automatically).

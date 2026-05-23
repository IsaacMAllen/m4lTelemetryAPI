# Architecture decisions

This document records the load-bearing choices for `m4l-telemetry-api`.
Each section is short and answers "why this and not that".

## 1. Dedicated Postgres for telemetry, not "shared"

The original ask was a "shared Postgres in the kube cluster". I pushed back.

Telemetry workloads have different characteristics from typical app workloads:

- **Write-heavy, append-only.** Once an event is inserted we never `UPDATE`
  or `DELETE` it. VACUUM is unusually quiet, but bulk INSERTs can be bursty.
- **Bursty.** A thousand users opening Live in the same minute = a thundering
  herd of connection setup + INSERT storms.
- **Different reliability tier.** Losing a few minutes of telemetry is fine
  (the device-side queue retries). Losing a few minutes of *user* data is not.

Co-locating telemetry with user-facing app data risks:

- Write storms degrading other apps' p99 latencies.
- JSONB bloat in `events.props` competing with other tables for shared
  buffers / autovacuum cycles.
- Schema migration coordination across teams.

So: a dedicated Postgres instance, sized small (200m CPU / 256Mi RAM is
plenty at our volume), trivially upgradable later.

## 2. CloudNativePG, not a hand-rolled StatefulSet

We deploy Postgres via [CloudNativePG](https://cloudnative-pg.io/), the
de-facto Kubernetes-native Postgres operator (CNCF Sandbox project).

Reasons:

- **One CR for the whole DB.** A single `Cluster` resource defines
  instances, storage, backup schedule, monitoring, and the bootstrap
  database. Compare to a hand-rolled StatefulSet + initContainers + Secret
  + Service + headless-Service + a backup CronJob.
- **HA upgrade path is trivial.** `instances: 1` → `instances: 3` and
  CNPG provisions standbys with streaming replication. We can start small
  and grow.
- **Backups + PITR.** CNPG handles WAL archiving + base backups to any
  S3-compatible object store. Configured here to write to in-cluster MinIO;
  swap the endpoint to AWS S3 / R2 with one yaml edit.
- **Rolling upgrades.** Upgrading Postgres minor versions is a one-line
  bump in `imageName`; CNPG drains, restarts, and rejoins.

The only meaningful con: another operator running in the cluster. At ~50m
CPU / 64Mi RAM idle, it pays for itself the first time you'd otherwise have
to debug a custom backup CronJob at 02:00.

## 3. In-cluster MinIO for backups (initially)

Because you don't yet have S3 / R2 / GCS, we deploy MinIO inside the cluster
to act as the backup target. MinIO is S3-compatible, so when you eventually
move to a managed object store the change is one YAML field
(`endpointURL` on the `ObjectStore`).

For production this is acceptable for "I want backups to *something*"
durability, but the long-term goal should be:

- MinIO → AWS S3 / Cloudflare R2 / Backblaze B2 (any S3-compatible)
- MinIO PV → snapshotted by your CSI driver

So MinIO is treated as **disposable infrastructure**: if the MinIO PV dies
we lose backups, but the live primary keeps serving and a new MinIO with
fresh backups picks up from the next WAL switch. Don't put anything else
on this MinIO.

## 4. Local-dev Postgres is Homebrew, not docker-compose

The repo still ships a `docker-compose.yml`, but the recommended local-dev
path on macOS is now `make dev-db-up` which uses Homebrew's
`postgresql@16` formula:

- Native — no VM / container runtime overhead.
- One command (`brew services start postgresql@16`) to start it as a
  launchd service.
- The schema is identical to production (same `alembic upgrade head`).

Docker Compose is kept for users who prefer container parity, and for
CI runners that already have Docker.

## 5. Schema: one fat `events` table with JSONB

See `app/models.py` and the README's "Schema design rationale". TL;DR:

- Single table, one row per ingested event.
- Server-side `id`, `received_at`. Client-side `ts`, `ts_ms`.
- `kind` + `level` are native enums (cheap filtering).
- `props` is JSONB with a GIN index for ad-hoc filtering on arbitrary keys.
- Composite indexes for the only query patterns we care about today:
  - per-(vendor, device, time) drilldowns,
  - global "what's broken" by (kind, level, time).

When sustained ingest crosses ~1 k events/sec we should partition by
`ts` (`PARTITION BY RANGE` with monthly partitions and an automated rotator).
Today that's premature.

## 6. Connection details: components, not a baked DSN

CNPG generates a Secret named `<cluster-name>-app` containing
`username`, `password`, `host`, `port`, `dbname` — but **not** an asyncpg
DSN. So `app/config.py` accepts either:

- `TELEMETRY_DATABASE_URL` (full DSN, used by Homebrew local dev), OR
- `TELEMETRY_DB_HOST` + `_PORT` + `_USER` + `_PASSWORD` + `_NAME`,
  used by the k8s Deployment which `valueFrom`s the CNPG secret.

This avoids running an init container to assemble the DSN, and avoids
storing a redundant URL-form secret that would fall out of sync if CNPG
rotates the password.

## 7. Auth at the edge, not in the app (for now)

The API checks a static bearer token (`TELEMETRY_INGEST_TOKENS`). That's
deliberately the minimum: bz.telemetry's threat model is "an attacker
spamming our endpoint with garbage", not "an attacker who already has a
device-side build artifact". Per-device tokens, mTLS, or signed payloads
are reasonable upgrades — they belong at the ingress layer
(nginx-ingress / Cilium / Istio) so we can rotate them without redeploying
the API.

## 8. Observability: Prometheus + JSON logs

- `/metrics` exposes per-route counters + histograms with low cardinality.
- Logs are JSON to stdout (`python-json-logger`). Any cluster log pipeline
  (Loki / EFK / Datadog Agent) ingests them with no app changes.
- `/healthz` (liveness) is a no-op return-200; `/readyz` runs `SELECT 1`
  so a DB outage takes the pod *out of the Service* — devices keep their
  events queued on disk and zero data is lost.

## 9. What we explicitly did NOT build

- **No retention policy yet.** The first time the DB hits 80% of the PV
  we'll add a CronJob that `DELETE`s events older than N days, or a
  partition rotator. Today the volume is too low to bother.
- **No deduplication.** bz.telemetry will retry an entire batch on any
  non-2xx response, which means duplicates are possible if a 200 response
  is lost in transit (extremely rare). We accept the duplicate; the cost
  of storing it is a few hundred bytes.
- **No schema versioning of `props`.** Forward-compat is handled by
  `model_config = ConfigDict(extra="ignore")` in `IngestEvent` —
  newer devices may include fields we don't yet store, and they're simply
  dropped.

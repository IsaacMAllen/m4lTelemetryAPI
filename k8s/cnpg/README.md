# CloudNativePG operator

The operator runs cluster-wide and watches for `Cluster`, `Backup`,
`ScheduledBackup`, and `ObjectStore` CRs. Install it once per cluster.

## Install

Pinned to a specific minor release. Bump the version when you bump it
elsewhere in the repo (see ARCHITECTURE.md).

```bash
# 1.29.x is the current stable at the time this was written.  Older releases
# (<= 1.24) ship CRDs with validation schemas that segfault kubectl 1.30+ on
# server-side apply -- always pin to a recent CNPG release.
kubectl apply --server-side -f \
  https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.29/releases/cnpg-1.29.1.yaml
```

This creates the `cnpg-system` namespace, installs the CRDs, and starts a
single operator pod. It will not affect any existing Postgres workloads
in the cluster.

## Verify

```bash
kubectl -n cnpg-system rollout status deploy/cnpg-controller-manager --timeout=120s
kubectl get crd | grep cnpg.io
```

You should see a handful of CRDs (`clusters.postgresql.cnpg.io`,
`backups.postgresql.cnpg.io`, etc.).

## Uninstall

```bash
kubectl delete -f \
  https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.29/releases/cnpg-1.29.1.yaml
```

> **Warning** Uninstalling the operator does NOT delete `Cluster` resources
> or their data; PVCs survive. To fully clean up: `kubectl -n telemetry
> delete cluster m4l-telemetry-pg --wait=true` first, then uninstall.

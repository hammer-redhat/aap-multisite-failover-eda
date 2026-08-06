# AAP Multisite Observability Dashboard — Design Spec

**Issue:** [#16 — Create Dashboard for AAP Admin Management and Observability](https://github.com/hammer-redhat/aap-multisite-failover-eda/issues/16)
**Date:** 2026-08-06
**Branch:** aap-26-failover

---

## Overview

Add a Grafana observability dashboard giving AAP administrators a real-time, side-by-side view of both sites' health, DB role (active/passive), pod status, and resource utilization. Alerting rules fire on split-brain, no-primary, and check-failure conditions.

The solution extends the existing `postgres_check` container with a Prometheus metrics mode and adds OCP User Workload Monitoring manifests per cluster. A centralized, pre-existing Grafana instance on a separate host connects to both clusters' Thanos Querier endpoints as datasources.

---

## Architecture

```
 Site 1 (OCP Cluster)              Site 2 (OCP Cluster)       External Host
┌──────────────────────┐           ┌──────────────────────┐   ┌─────────────────┐
│  CronJob (unchanged) │           │  CronJob (unchanged) │   │  Grafana        │
│  aap-site-metrics    │           │  aap-site-metrics    │   │  ├─ DS: Site 1  │
│  Deployment :8080    │           │  Deployment :8080    │   │  │  Thanos QR   │
│  ServiceMonitor      │           │  ServiceMonitor      │   │  └─ DS: Site 2  │
│  PrometheusRule      │           │  PrometheusRule      │   │     Thanos QR   │
│  Thanos Querier ─────┼───────────┼──────────────────────┼──▶│  Dashboard JSON │
└──────────────────────┘           └──────────────────────┘   └─────────────────┘
```

- The existing CronJob (`postgres_check`, `MODE=cron`) is **untouched** — it continues POSTing `in_recovery` to EDA every minute.
- A new `aap-site-metrics` **Deployment** runs the same image with `MODE=metrics`. It loops every 60s, checks `pg_is_in_recovery()`, and serves Prometheus text format on `:8080/metrics`. It does not POST to EDA.
- **User Workload Monitoring** on each cluster scrapes the Deployment via a `ServiceMonitor`.
- The centralized **Grafana** instance has two `prometheus` datasources — one per cluster's Thanos Querier — and uses Grafana's mixed-datasource feature to render both sites side-by-side in a single dashboard.
- **No Grafana Operator** is required on either OCP cluster.

---

## Component: postgres_check Extension

### Behavior

`MODE` environment variable controls behavior:

| `MODE` | Behavior | Deployment type |
|--------|----------|-----------------|
| `cron` (default) | Run once, POST to EDA webhook, exit | CronJob (unchanged) |
| `metrics` | Loop every 60s, serve `/metrics` on `:8080`, never exit | Deployment (new) |

### Prometheus Metrics

All metrics carry a `site` label (e.g. `site1` / `site2`) set via the `SITE_LABEL` env var.

| Metric | Type | Description |
|--------|------|-------------|
| `aap_site_in_recovery` | Gauge | `1` = standby/passive, `0` = primary/active |
| `aap_pg_check_success` | Gauge | `1` = last check succeeded, `0` = failed (connection error) |
| `aap_pg_check_last_run_timestamp_seconds` | Gauge | Unix timestamp of last completed check |
| `aap_pg_check_duration_seconds` | Gauge | Wall-clock time of last DB query in seconds |

### Code changes

- `files/postgres_checks/postgres_check_example.py` — add `run_metrics_server()` function using `prometheus_client.start_http_server(8080)` and a `while True` check loop; branch on `MODE` env var in `main()`.
- `files/postgres_checks/requirements.txt` — add `prometheus_client`.
- `files/postgres_checks/Dockerfile` — add `EXPOSE 8080`.

---

## Component: OCP Manifests

Applied to both clusters (parameterized by namespace and site label). All manifests live under `files/monitoring/`.

### `metrics-deployment.yml`

- Image: same as CronJob (`quay.io/chrhamme/postgres-check:vX`)
- Env: `MODE=metrics`, `SITE_LABEL=site1` (or `site2`), same `postgres-check-db` and `postgres-check-webhook` Secret refs (webhook vars unused in metrics mode)
- Port `8080` named `metrics`
- `replicas: 1`, liveness probe on `GET /metrics` (port 8080)

### `metrics-service.yml`

- `ClusterIP` Service targeting `app: aap-site-metrics`
- Port `8080` named `metrics`

### `service-monitor.yml`

- Targets the `metrics` Service port
- `namespaceSelector` scoped to the AAP namespace
- Relabels to add `site` label matching `SITE_LABEL` so Grafana queries can filter by site

### `prometheus-rule.yml`

Per-site alerts that each cluster's Prometheus can evaluate independently (it only sees its own metrics):

| Alert | Expression | For | Severity |
|-------|------------|-----|----------|
| `AAPSiteCheckFailing` | `aap_pg_check_success == 0` | 2m | warning |
| `AAPFailoverDetected` | `changes(aap_site_in_recovery[5m]) > 0` | 0s | info |

Cross-site alerts (`AAPSiteSplitBrain`, `AAPNoPrimary`) require visibility into both sites simultaneously and cannot be expressed as per-cluster `PrometheusRule` objects. These are implemented as **Grafana alerting rules** against the mixed datasource, evaluated in the centralized Grafana instance:

| Alert | Expression | Severity |
|-------|------------|----------|
| `AAPSiteSplitBrain` | Site 1 `aap_site_in_recovery == 0` AND Site 2 `aap_site_in_recovery == 0` | critical |
| `AAPNoPrimary` | Site 1 `aap_site_in_recovery == 1` AND Site 2 `aap_site_in_recovery == 1` | critical |

---

## Component: Grafana Provisioning

### Datasources

Two Prometheus datasources provisioned via `POST /api/datasources`:

| Name | URL | Auth |
|------|-----|------|
| `AAP Site 1` | `https://<thanos_querier_site_one>:9091` | Bearer token (OCP SA with `cluster-monitoring-view`) |
| `AAP Site 2` | `https://<thanos_querier_site_two>:9091` | Bearer token (OCP SA with `cluster-monitoring-view`) |

### Dashboard Layout

Five rows, panels use mixed datasource (each panel explicitly selects Site 1 or Site 2 datasource):

| Row | Panels |
|-----|--------|
| **Site Status** | Site 1 active/passive stat, Site 2 active/passive stat, last check timestamps |
| **DB Health** | `in_recovery` gauge per site, check success indicator, check duration |
| **Failover Events** | Time-series of `aap_site_in_recovery` — shows role-flip points visually |
| **Pod Health** | AAP pod ready count via `kube_pod_status_ready` filtered to AAP namespace, per site |
| **Resource Utilization** | CPU + memory for AAP pods per site |

Dashboard JSON stored at `files/grafana/dashboard.json` and provisioned via `POST /api/dashboards/import`.

### Grafana Provisioning Files

| File | Purpose |
|------|---------|
| `files/grafana/datasources.yml` | Grafana datasource provisioning YAML (for file-based provisioning, reference only) |
| `files/grafana/dashboard.json` | Full Grafana dashboard JSON |

---

## Component: Ansible Playbook

`playbooks/deploy_dashboard/deploy_dashboard.yml` — orchestrates full deployment:

1. **`tasks/apply_ocp_manifests.yml`** — applies `metrics-deployment.yml`, `metrics-service.yml`, `service-monitor.yml`, `prometheus-rule.yml` to Site 1 namespace, then Site 2 namespace, using `redhat.openshift.k8s`. Requires OCP credentials for each cluster.
2. **`tasks/provision_datasources.yml`** — POSTs each datasource definition to `{{ grafana_url }}/api/datasources` using `ansible.builtin.uri` with the Grafana API token.
3. **`tasks/provision_dashboard.yml`** — POSTs `files/grafana/dashboard.json` to `{{ grafana_url }}/api/dashboards/import`.

### New vars (`vars/main.yml.example` additions)

```yaml
# Grafana
grafana_url: "https://grafana.example.com"
grafana_api_token: ""

# Thanos Querier endpoints (per site)
thanos_querier_site_one: ""
thanos_querier_site_two: ""
thanos_bearer_token_site_one: ""
thanos_bearer_token_site_two: ""

# Site labels (used in metrics and ServiceMonitor)
site_label_one: "site1"
site_label_two: "site2"
```

---

## File Structure

```
files/
  postgres_checks/
    postgres_check_example.py    ← modified
    requirements.txt             ← modified (add prometheus_client)
    Dockerfile                   ← modified (EXPOSE 8080)
  monitoring/
    metrics-deployment.yml       ← new
    metrics-service.yml          ← new
    service-monitor.yml          ← new
    prometheus-rule.yml          ← new
  grafana/
    datasources.yml              ← new
    dashboard.json               ← new

playbooks/
  deploy_dashboard/
    deploy_dashboard.yml         ← new
    tasks/
      apply_ocp_manifests.yml    ← new
      provision_datasources.yml  ← new
      provision_dashboard.yml    ← new

vars/
  main.yml.example               ← modified
```

**Unchanged:** all existing rulebooks, playbooks, CronJob manifests, and `openshift-deployment.example.yaml`.

---

## Testing

- Unit tests for `run_metrics_server()` and the metrics loop in `tests/` (mock `psycopg2`, assert Prometheus registry values)
- Manual verification: `kubectl port-forward` the `aap-site-metrics` Deployment and `curl localhost:8080/metrics`
- Grafana: confirm both datasources return data via Explore panel before importing dashboard

# AAP Multisite Observability Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the `postgres_check` container with a Prometheus metrics mode, add OCP User Workload Monitoring manifests per cluster, and provision a centralized Grafana dashboard giving AAP admins a side-by-side view of both sites' health and failover state.

**Architecture:** A new `aap-site-metrics` Deployment (same image as the existing CronJob, `MODE=metrics`) loops every 60 s, checks `pg_is_in_recovery()`, and serves Prometheus text on `:8080`. A `ServiceMonitor` per cluster tells OCP User Workload Monitoring to scrape it. A pre-existing Grafana instance connects to both clusters' Thanos Querier endpoints as separate datasources and renders both sites side-by-side using Grafana's mixed-datasource feature.

**Tech Stack:** Python 3.12, `prometheus_client`, `psycopg2-binary`, `requests`; OCP 4.16/4.17 User Workload Monitoring; Grafana 10.x; Ansible (`kubernetes.core.k8s`, `ansible.builtin.uri`).

## Global Constraints

- Existing CronJob and its manifests (`files/postgres_checks/openshift-deployment.example.yaml`) are **never modified**.
- All Prometheus metric names are prefixed `aap_`.
- All new OCP manifests live under `files/monitoring/`; all Grafana files under `files/grafana/`.
- All new Ansible tasks live under `playbooks/deploy_dashboard/tasks/`.
- Manifest files use Jinja2 variables (`{{ var_name }}`) resolved at apply time via `lookup('ansible.builtin.template', ...)`.
- `SITE_LABEL` env var provides the `site` label value for all Prometheus metrics (e.g., `site1`, `site2`).
- `MODE` env var selects behavior: `cron` (default, unchanged) or `metrics` (new long-running mode).
- Python minimum: 3.12 (matches Dockerfile base).
- `prometheus_client>=0.20.0` added to `requirements.txt`.

---

### Task 1: Extend `postgres_check` with `MODE=metrics`

**Files:**
- Modify: `files/postgres_checks/postgres_check_example.py`
- Modify: `files/postgres_checks/requirements.txt`
- Modify: `files/postgres_checks/Dockerfile`
- Create: `tests/test_postgres_check_metrics.py`

**Interfaces:**
- Produces: `_run_single_check(db_config: dict) -> tuple[bool | None, bool, float, str | None]` — returns `(in_recovery, success, duration_seconds, error_message)`. `in_recovery` is `None` on failure. Used by both cron and metrics modes.
- Produces: `run_metrics_loop(db_config: dict, interval: int = 60) -> None` — runs forever; called by `run_metrics_server()`.
- Produces: `run_metrics_server() -> None` — called by `main()` when `MODE=metrics`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_postgres_check_metrics.py`:

```python
import os
import sys
from unittest.mock import MagicMock, patch, call
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files", "postgres_checks"))


class TestRunSingleCheck:
    def test_returns_false_and_success_when_primary(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = (False,)

        with patch("psycopg2.connect", return_value=mock_conn):
            import postgres_check_example as mod
            in_recovery, success, duration, error = mod._run_single_check(
                {"host": "h", "port": 5432, "dbname": "db", "user": "u", "password": "p"}
            )

        assert in_recovery is False
        assert success is True
        assert duration >= 0
        assert error is None

    def test_returns_true_and_success_when_standby(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = (True,)

        with patch("psycopg2.connect", return_value=mock_conn):
            import postgres_check_example as mod
            in_recovery, success, duration, error = mod._run_single_check(
                {"host": "h", "port": 5432, "dbname": "db", "user": "u", "password": "p"}
            )

        assert in_recovery is True
        assert success is True
        assert error is None

    def test_returns_none_and_failure_on_connection_error(self):
        with patch("psycopg2.connect", side_effect=Exception("connection refused")):
            import postgres_check_example as mod
            in_recovery, success, duration, error = mod._run_single_check(
                {"host": "bad", "port": 5432, "dbname": "db", "user": "u", "password": "p"}
            )

        assert in_recovery is None
        assert success is False
        assert duration >= 0
        assert "connection refused" in error

    def test_closes_connection_after_successful_check(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = (False,)

        with patch("psycopg2.connect", return_value=mock_conn):
            import postgres_check_example as mod
            mod._run_single_check(
                {"host": "h", "port": 5432, "dbname": "db", "user": "u", "password": "p"}
            )

        mock_cur.close.assert_called_once()
        mock_conn.close.assert_called_once()


class TestRunMetricsLoop:
    def test_sets_check_success_gauge_on_success(self, monkeypatch):
        monkeypatch.setenv("SITE_LABEL", "site1")

        mock_check = MagicMock(return_value=(False, True, 0.05, None))
        call_count = 0

        def fake_sleep(_):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise StopIteration

        import postgres_check_example as mod

        mock_success = MagicMock()
        mock_recovery = MagicMock()
        mock_duration = MagicMock()
        mock_ts = MagicMock()

        with patch.object(mod, "_run_single_check", mock_check), \
             patch.object(mod, "_METRIC_CHECK_SUCCESS", mock_success), \
             patch.object(mod, "_METRIC_IN_RECOVERY", mock_recovery), \
             patch.object(mod, "_METRIC_DURATION", mock_duration), \
             patch.object(mod, "_METRIC_LAST_RUN_TS", mock_ts), \
             patch("time.sleep", side_effect=fake_sleep):
            with pytest.raises(StopIteration):
                mod.run_metrics_loop(
                    {"host": "h", "port": 5432, "dbname": "db", "user": "u", "password": "p"},
                    interval=60,
                )

        mock_success.labels.return_value.set.assert_called_with(1)
        mock_recovery.labels.return_value.set.assert_called_with(0)  # in_recovery=False → 0

    def test_sets_check_success_zero_and_skips_recovery_on_failure(self, monkeypatch):
        monkeypatch.setenv("SITE_LABEL", "site1")

        mock_check = MagicMock(return_value=(None, False, 0.01, "refused"))
        call_count = 0

        def fake_sleep(_):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise StopIteration

        import postgres_check_example as mod

        mock_success = MagicMock()
        mock_recovery = MagicMock()
        mock_duration = MagicMock()
        mock_ts = MagicMock()

        with patch.object(mod, "_run_single_check", mock_check), \
             patch.object(mod, "_METRIC_CHECK_SUCCESS", mock_success), \
             patch.object(mod, "_METRIC_IN_RECOVERY", mock_recovery), \
             patch.object(mod, "_METRIC_DURATION", mock_duration), \
             patch.object(mod, "_METRIC_LAST_RUN_TS", mock_ts), \
             patch("time.sleep", side_effect=fake_sleep):
            with pytest.raises(StopIteration):
                mod.run_metrics_loop(
                    {"host": "h", "port": 5432, "dbname": "db", "user": "u", "password": "p"},
                    interval=60,
                )

        mock_success.labels.return_value.set.assert_called_with(0)
        # in_recovery gauge must NOT be updated on failure (stale value preserved)
        mock_recovery.labels.return_value.set.assert_not_called()


class TestMainModeRouting:
    def test_main_calls_run_metrics_server_in_metrics_mode(self, monkeypatch):
        monkeypatch.setenv("MODE", "metrics")

        import postgres_check_example as mod

        with patch.object(mod, "run_metrics_server") as mock_server:
            mod.main()

        mock_server.assert_called_once()

    def test_main_uses_cron_path_by_default(self, monkeypatch):
        monkeypatch.delenv("MODE", raising=False)
        monkeypatch.setenv("WEBHOOK_URL", "https://eda.example.com/webhook")
        monkeypatch.setenv("AUTH_TOKEN", "token123")

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = (False,)
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.text = "OK"

        import postgres_check_example as mod

        with patch("psycopg2.connect", return_value=mock_conn), \
             patch("requests.post", return_value=mock_response) as mock_post, \
             patch.dict(os.environ, {"DB_CONFIG": '{"host":"h","port":5432,"dbname":"db","user":"u","password":"p"}'}):
            result = mod.main()

        assert result == 0
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs[1]["json"] == {"in_recovery": False}
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /path/to/repo
pip install psycopg2-binary requests
pytest tests/test_postgres_check_metrics.py -v 2>&1 | head -40
```

Expected: `ImportError` or `AttributeError` — `_run_single_check` and `run_metrics_loop` do not exist yet.

- [ ] **Step 3: Add `prometheus_client` to requirements.txt**

Edit `files/postgres_checks/requirements.txt` — append:

```
prometheus_client>=0.20.0
```

Full file after edit:
```
psycopg2-binary>=2.9.9
requests>=2.31.0
prometheus_client>=0.20.0
```

Install locally: `pip install prometheus_client>=0.20.0`

- [ ] **Step 4: Rewrite `postgres_check_example.py`**

Replace the entire file with:

```python
"""
PostgreSQL recovery check — two operating modes controlled by MODE env var.

MODE=cron (default)
  Run once: query pg_is_in_recovery(), POST result to EDA Event Stream webhook, exit.
  Required env: WEBHOOK_URL, AUTH_TOKEN, DB_CONFIG or PG* vars.

MODE=metrics
  Run forever: loop every CHECK_INTERVAL seconds, expose Prometheus metrics on
  0.0.0.0:METRICS_PORT/metrics. Does NOT post to the webhook.
  Required env: DB_CONFIG or PG* vars.
  Optional env: SITE_LABEL (default: "unknown"), METRICS_PORT (default: 8080),
                CHECK_INTERVAL (default: 60).

Database config (both modes) — either:
  DB_CONFIG   JSON object: {"host","port","dbname","user","password"}
or:
  PGHOST, PGUSER, PGPASSWORD
  PGPORT (default 5432), PGDATABASE or PG_DBNAME (default awx)

Optional (cron only):
  WEBHOOK_SSL_VERIFY  "true" or "false" (default: true)
"""

from __future__ import annotations

import json
import os
import sys
import time

import psycopg2
import requests
from prometheus_client import Gauge, start_http_server


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _load_db_config() -> dict:
    raw = os.environ.get("DB_CONFIG", "").strip()
    if raw:
        cfg = json.loads(raw)
        if not isinstance(cfg, dict):
            raise ValueError("DB_CONFIG must be a JSON object")
        required = ("host", "port", "dbname", "user", "password")
        missing = [k for k in required if k not in cfg]
        if missing:
            raise ValueError(f"DB_CONFIG missing keys: {', '.join(missing)}")
        cfg["port"] = int(cfg["port"])
        return cfg

    host = os.environ.get("PGHOST", "").strip()
    user = os.environ.get("PGUSER", "").strip()
    password = os.environ.get("PGPASSWORD", "")
    if not host or not user:
        raise ValueError(
            "Set DB_CONFIG (JSON) or PGHOST, PGUSER, and PGPASSWORD for database access"
        )

    port = int(os.environ.get("PGPORT", "5432"))
    dbname = (
        os.environ.get("PGDATABASE") or os.environ.get("PG_DBNAME") or "awx"
    ).strip()

    return {"host": host, "port": port, "dbname": dbname, "user": user, "password": password}


# Prometheus gauges — created once at module load, labelled by site at runtime.
_METRIC_IN_RECOVERY = Gauge(
    "aap_site_in_recovery",
    "1 if the site DB is in recovery (standby/passive), 0 if primary/active",
    ["site"],
)
_METRIC_CHECK_SUCCESS = Gauge(
    "aap_pg_check_success",
    "1 if the last postgres check succeeded, 0 if it failed",
    ["site"],
)
_METRIC_LAST_RUN_TS = Gauge(
    "aap_pg_check_last_run_timestamp_seconds",
    "Unix timestamp of the last completed postgres check",
    ["site"],
)
_METRIC_DURATION = Gauge(
    "aap_pg_check_duration_seconds",
    "Wall-clock time of the last DB query in seconds",
    ["site"],
)


def _run_single_check(db_config: dict) -> tuple:
    """
    Execute SELECT pg_is_in_recovery() against db_config.

    Returns (in_recovery, success, duration_seconds, error_message).
    in_recovery and error_message are None on success and failure respectively.
    """
    t0 = time.monotonic()
    try:
        conn = psycopg2.connect(**db_config)
        cur = conn.cursor()
        cur.execute("SELECT pg_is_in_recovery();")
        in_recovery = cur.fetchone()[0]
        cur.close()
        conn.close()
        return in_recovery, True, time.monotonic() - t0, None
    except Exception as exc:
        return None, False, time.monotonic() - t0, str(exc)


def run_metrics_loop(db_config: dict, interval: int = 60) -> None:
    """
    Infinite loop: check DB every `interval` seconds and update Prometheus gauges.

    On failure, aap_site_in_recovery is intentionally NOT updated so the last
    known-good value is preserved for alerting continuity.
    """
    site = os.environ.get("SITE_LABEL", "unknown")
    while True:
        in_recovery, success, duration, _ = _run_single_check(db_config)
        _METRIC_CHECK_SUCCESS.labels(site=site).set(1 if success else 0)
        _METRIC_DURATION.labels(site=site).set(duration)
        _METRIC_LAST_RUN_TS.labels(site=site).set(time.time())
        if success:
            _METRIC_IN_RECOVERY.labels(site=site).set(1 if in_recovery else 0)
        time.sleep(interval)


def run_metrics_server() -> None:
    """Start Prometheus HTTP server and run the check loop forever (MODE=metrics entry point)."""
    port = int(os.environ.get("METRICS_PORT", "8080"))
    interval = int(os.environ.get("CHECK_INTERVAL", "60"))
    db_config = _load_db_config()
    start_http_server(port)
    run_metrics_loop(db_config, interval)


def main() -> int:
    mode = os.environ.get("MODE", "cron").strip().lower()

    if mode == "metrics":
        run_metrics_server()
        return 0  # unreachable in normal operation

    # --- cron mode: run once, POST to EDA webhook, exit ---
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    auth_token = os.environ.get("AUTH_TOKEN", "").strip()
    if not webhook_url or not auth_token:
        print(
            json.dumps({"error": "WEBHOOK_URL and AUTH_TOKEN environment variables are required"}),
            file=sys.stderr,
        )
        return 1

    try:
        db_config = _load_db_config()
    except (json.JSONDecodeError, ValueError) as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1

    verify = _env_bool("WEBHOOK_SSL_VERIFY", True)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_token}",
    }

    in_recovery, success, _, error = _run_single_check(db_config)
    payload: dict = {"in_recovery": in_recovery} if success else {"error": error}

    print(json.dumps(payload, indent=4))

    try:
        response = requests.post(
            webhook_url, json=payload, headers=headers, timeout=60, verify=verify
        )
    except requests.RequestException as exc:
        print(json.dumps({"error": f"webhook request failed: {exc}"}), file=sys.stderr)
        return 1

    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")
    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Add `EXPOSE 8080` to Dockerfile**

Add one line before `USER nobody`:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY postgres_check_example.py .

EXPOSE 8080

USER nobody

ENTRYPOINT ["/usr/local/bin/python3", "/app/postgres_check_example.py"]
```

- [ ] **Step 6: Run tests to confirm they pass**

```bash
pip install prometheus_client>=0.20.0
pytest tests/test_postgres_check_metrics.py -v
```

Expected: all tests PASS.

- [ ] **Step 7: Smoke-test metrics mode locally**

```bash
cd files/postgres_checks
MODE=metrics SITE_LABEL=site1 DB_CONFIG='{"host":"localhost","port":5432,"dbname":"awx","user":"awx","password":"awx"}' \
  python postgres_check_example.py &
sleep 5
curl -s localhost:8080/metrics | grep aap_
kill %1
```

Expected output contains:
```
aap_pg_check_success{site="site1"} 0.0   # or 1.0 if PG is reachable
aap_pg_check_last_run_timestamp_seconds{site="site1"} ...
aap_pg_check_duration_seconds{site="site1"} ...
```

- [ ] **Step 8: Commit**

```bash
git add files/postgres_checks/postgres_check_example.py \
        files/postgres_checks/requirements.txt \
        files/postgres_checks/Dockerfile \
        tests/test_postgres_check_metrics.py
git commit -m "feat: add MODE=metrics prometheus endpoint to postgres_check"
```

---

### Task 2: OCP User Workload Monitoring Manifests

**Files:**
- Create: `files/monitoring/metrics-deployment.yml`
- Create: `files/monitoring/metrics-service.yml`
- Create: `files/monitoring/service-monitor.yml`
- Create: `files/monitoring/prometheus-rule.yml`

**Interfaces:**
- Consumes: `postgres_check_image` var (image URI), `site_namespace` var (OCP namespace), `site_label` var (e.g. `site1`).
- Produces: A running `aap-site-metrics` Deployment scraped by UWM; `AAPSiteCheckFailing` and `AAPFailoverDetected` alerts in each cluster.

- [ ] **Step 1: Create `files/monitoring/metrics-deployment.yml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aap-site-metrics
  namespace: "{{ site_namespace }}"
  labels:
    app: aap-site-metrics
spec:
  replicas: 1
  selector:
    matchLabels:
      app: aap-site-metrics
  template:
    metadata:
      labels:
        app: aap-site-metrics
    spec:
      containers:
        - name: aap-site-metrics
          image: "{{ postgres_check_image }}"
          env:
            - name: MODE
              value: "metrics"
            - name: SITE_LABEL
              value: "{{ site_label }}"
            - name: CHECK_INTERVAL
              value: "60"
            - name: METRICS_PORT
              value: "8080"
            - name: DB_CONFIG
              valueFrom:
                secretKeyRef:
                  name: postgres-check-db
                  key: DB_CONFIG
            - name: WEBHOOK_URL
              valueFrom:
                secretKeyRef:
                  name: postgres-check-webhook
                  key: WEBHOOK_URL
            - name: AUTH_TOKEN
              valueFrom:
                secretKeyRef:
                  name: postgres-check-webhook
                  key: AUTH_TOKEN
          ports:
            - name: metrics
              containerPort: 8080
              protocol: TCP
          livenessProbe:
            httpGet:
              path: /metrics
              port: 8080
            initialDelaySeconds: 15
            periodSeconds: 30
            failureThreshold: 3
          resources:
            requests:
              cpu: "50m"
              memory: "64Mi"
            limits:
              cpu: "100m"
              memory: "128Mi"
      securityContext:
        runAsNonRoot: true
```

- [ ] **Step 2: Create `files/monitoring/metrics-service.yml`**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: aap-site-metrics
  namespace: "{{ site_namespace }}"
  labels:
    app: aap-site-metrics
spec:
  selector:
    app: aap-site-metrics
  ports:
    - name: metrics
      port: 8080
      targetPort: 8080
      protocol: TCP
```

- [ ] **Step 3: Create `files/monitoring/service-monitor.yml`**

OCP User Workload Monitoring discovers `ServiceMonitor` objects in user namespaces automatically when UWM is enabled. No special cluster-monitoring labels are required.

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: aap-site-metrics
  namespace: "{{ site_namespace }}"
  labels:
    app: aap-site-metrics
spec:
  selector:
    matchLabels:
      app: aap-site-metrics
  endpoints:
    - port: metrics
      path: /metrics
      interval: 60s
      relabelings:
        - targetLabel: site
          replacement: "{{ site_label }}"
```

- [ ] **Step 4: Create `files/monitoring/prometheus-rule.yml`**

Per-site alerts only — cross-site alerts (SplitBrain, NoPrimary) are defined as Grafana alerting rules because each cluster's Prometheus sees only its own metrics.

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: aap-site-alerts
  namespace: "{{ site_namespace }}"
  labels:
    app: aap-site-metrics
    openshift.io/prometheus-rule-evaluation-scope: leaf-prometheus
spec:
  groups:
    - name: aap.site.alerts
      interval: 60s
      rules:
        - alert: AAPSiteCheckFailing
          expr: aap_pg_check_success == 0
          for: 2m
          labels:
            severity: warning
          annotations:
            summary: "AAP postgres check failing on {{ '{{' }} $labels.site {{ '}}' }}"
            description: >
              The postgres check on {{ '{{' }} $labels.site {{ '}}' }} has been failing for
              more than 2 minutes. The aap_site_in_recovery metric may be stale.

        - alert: AAPFailoverDetected
          expr: changes(aap_site_in_recovery[5m]) > 0
          for: 0s
          labels:
            severity: info
          annotations:
            summary: "AAP failover detected on {{ '{{' }} $labels.site {{ '}}' }}"
            description: >
              aap_site_in_recovery changed on {{ '{{' }} $labels.site {{ '}}' }} in the
              last 5 minutes, indicating a database role change (failover or failback).
```

- [ ] **Step 5: Verify manifests are valid YAML**

```bash
python3 -c "
import yaml, sys
for f in [
  'files/monitoring/metrics-deployment.yml',
  'files/monitoring/metrics-service.yml',
  'files/monitoring/service-monitor.yml',
  'files/monitoring/prometheus-rule.yml',
]:
    yaml.safe_load(open(f))
    print(f'OK: {f}')
"
```

Expected: four `OK:` lines, no errors. (Jinja2 `{{ }}` tokens cause no YAML parse errors since they appear inside quoted strings or values.)

- [ ] **Step 6: Commit**

```bash
git add files/monitoring/
git commit -m "feat: add OCP UWM manifests for aap-site-metrics"
```

---

### Task 3: Grafana Datasource Config and Dashboard JSON

**Files:**
- Create: `files/grafana/datasources.yml`
- Create: `files/grafana/dashboard.json`

**Interfaces:**
- Consumes: Thanos Querier URLs and Bearer tokens from `vars/main.yml`.
- Produces: Two Prometheus datasources and one dashboard importable via Grafana HTTP API.

- [ ] **Step 1: Create `files/grafana/datasources.yml`**

Reference file for manual or file-based Grafana provisioning. The Ansible task (Task 4) uses the values here as a template for the API payload.

```yaml
# Grafana datasource provisioning reference.
# Values are Jinja2 templates — rendered by the Ansible deploy_dashboard playbook.
# For manual provisioning, copy to /etc/grafana/provisioning/datasources/ on the Grafana host
# and substitute real values for the {{ }} placeholders.

apiVersion: 1
datasources:
  - name: "AAP Site 1"
    type: prometheus
    access: proxy
    url: "https://{{ thanos_querier_site_one }}"
    basicAuth: false
    jsonData:
      httpMethod: GET
      httpHeaderName1: "Authorization"
    secureJsonData:
      httpHeaderValue1: "Bearer {{ thanos_bearer_token_site_one }}"
    isDefault: false
    editable: true

  - name: "AAP Site 2"
    type: prometheus
    access: proxy
    url: "https://{{ thanos_querier_site_two }}"
    basicAuth: false
    jsonData:
      httpMethod: GET
      httpHeaderName1: "Authorization"
    secureJsonData:
      httpHeaderValue1: "Bearer {{ thanos_bearer_token_site_two }}"
    isDefault: false
    editable: true
```

- [ ] **Step 2: Create `files/grafana/dashboard.json`**

Full Grafana 10.x dashboard JSON. Uses datasource template variables (`$datasource_site1`, `$datasource_site2`) and namespace variables so operators can select their configured datasources without editing JSON.

```json
{
  "__inputs": [
    {
      "name": "DS_SITE1",
      "label": "Site 1 Prometheus",
      "type": "datasource",
      "pluginId": "prometheus"
    },
    {
      "name": "DS_SITE2",
      "label": "Site 2 Prometheus",
      "type": "datasource",
      "pluginId": "prometheus"
    }
  ],
  "__requires": [
    {"type": "grafana", "id": "grafana", "name": "Grafana", "version": "10.0.0"},
    {"type": "datasource", "id": "prometheus", "name": "Prometheus", "version": "1.0.0"},
    {"type": "panel", "id": "stat", "name": "Stat", "version": ""},
    {"type": "panel", "id": "gauge", "name": "Gauge", "version": ""},
    {"type": "panel", "id": "timeseries", "name": "Time series", "version": ""}
  ],
  "annotations": {"list": []},
  "editable": true,
  "graphTooltip": 1,
  "id": null,
  "links": [],
  "refresh": "30s",
  "schemaVersion": 38,
  "tags": ["aap", "multisite", "observability"],
  "templating": {
    "list": [
      {
        "current": {},
        "hide": 0,
        "includeAll": false,
        "label": "Site 1 Datasource",
        "multi": false,
        "name": "datasource_site1",
        "options": [],
        "query": "prometheus",
        "refresh": 1,
        "type": "datasource"
      },
      {
        "current": {},
        "hide": 0,
        "includeAll": false,
        "label": "Site 2 Datasource",
        "multi": false,
        "name": "datasource_site2",
        "options": [],
        "query": "prometheus",
        "refresh": 1,
        "type": "datasource"
      },
      {
        "current": {"value": "aap-26"},
        "hide": 0,
        "label": "Site 1 Namespace",
        "name": "ns_site1",
        "query": "aap-26",
        "type": "textbox"
      },
      {
        "current": {"value": "aap-26-dr"},
        "hide": 0,
        "label": "Site 2 Namespace",
        "name": "ns_site2",
        "query": "aap-26-dr",
        "type": "textbox"
      }
    ]
  },
  "time": {"from": "now-6h", "to": "now"},
  "timepicker": {},
  "timezone": "browser",
  "title": "AAP Multisite Observability",
  "uid": "aap-multisite-obs",
  "version": 1,
  "panels": [
    {
      "id": 1,
      "type": "row",
      "title": "Site Status",
      "collapsed": false,
      "gridPos": {"h": 1, "w": 24, "x": 0, "y": 0}
    },
    {
      "id": 2,
      "type": "stat",
      "title": "Site 1 Role",
      "datasource": {"type": "prometheus", "uid": "${datasource_site1}"},
      "targets": [{
        "expr": "aap_site_in_recovery",
        "instant": true,
        "legendFormat": ""
      }],
      "fieldConfig": {
        "defaults": {
          "mappings": [{
            "type": "value",
            "options": {
              "0": {"text": "ACTIVE", "color": "green", "index": 0},
              "1": {"text": "PASSIVE", "color": "yellow", "index": 1}
            }
          }],
          "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": null}]},
          "color": {"mode": "thresholds"}
        }
      },
      "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "orientation": "auto", "textMode": "auto", "colorMode": "background"},
      "gridPos": {"h": 4, "w": 6, "x": 0, "y": 1}
    },
    {
      "id": 3,
      "type": "stat",
      "title": "Site 2 Role",
      "datasource": {"type": "prometheus", "uid": "${datasource_site2}"},
      "targets": [{
        "expr": "aap_site_in_recovery",
        "instant": true,
        "legendFormat": ""
      }],
      "fieldConfig": {
        "defaults": {
          "mappings": [{
            "type": "value",
            "options": {
              "0": {"text": "ACTIVE", "color": "green", "index": 0},
              "1": {"text": "PASSIVE", "color": "yellow", "index": 1}
            }
          }],
          "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": null}]},
          "color": {"mode": "thresholds"}
        }
      },
      "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "orientation": "auto", "textMode": "auto", "colorMode": "background"},
      "gridPos": {"h": 4, "w": 6, "x": 6, "y": 1}
    },
    {
      "id": 4,
      "type": "stat",
      "title": "Site 1 Check Success",
      "datasource": {"type": "prometheus", "uid": "${datasource_site1}"},
      "targets": [{
        "expr": "aap_pg_check_success",
        "instant": true,
        "legendFormat": ""
      }],
      "fieldConfig": {
        "defaults": {
          "mappings": [{
            "type": "value",
            "options": {
              "0": {"text": "FAILING", "color": "red", "index": 0},
              "1": {"text": "OK", "color": "green", "index": 1}
            }
          }],
          "color": {"mode": "thresholds"},
          "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": null}]}
        }
      },
      "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background"},
      "gridPos": {"h": 4, "w": 6, "x": 12, "y": 1}
    },
    {
      "id": 5,
      "type": "stat",
      "title": "Site 2 Check Success",
      "datasource": {"type": "prometheus", "uid": "${datasource_site2}"},
      "targets": [{
        "expr": "aap_pg_check_success",
        "instant": true,
        "legendFormat": ""
      }],
      "fieldConfig": {
        "defaults": {
          "mappings": [{
            "type": "value",
            "options": {
              "0": {"text": "FAILING", "color": "red", "index": 0},
              "1": {"text": "OK", "color": "green", "index": 1}
            }
          }],
          "color": {"mode": "thresholds"},
          "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": null}]}
        }
      },
      "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background"},
      "gridPos": {"h": 4, "w": 6, "x": 18, "y": 1}
    },
    {
      "id": 6,
      "type": "row",
      "title": "Failover Events",
      "collapsed": false,
      "gridPos": {"h": 1, "w": 24, "x": 0, "y": 5}
    },
    {
      "id": 7,
      "type": "timeseries",
      "title": "DB Role Over Time (0=Active, 1=Passive)",
      "datasource": {"type": "prometheus", "uid": "${datasource_site1}"},
      "targets": [
        {
          "datasource": {"type": "prometheus", "uid": "${datasource_site1}"},
          "expr": "aap_site_in_recovery",
          "legendFormat": "Site 1"
        },
        {
          "datasource": {"type": "prometheus", "uid": "${datasource_site2}"},
          "expr": "aap_site_in_recovery",
          "legendFormat": "Site 2"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "custom": {"lineWidth": 2, "fillOpacity": 10},
          "min": 0,
          "max": 1,
          "mappings": [
            {"type": "value", "options": {"0": {"text": "Active"}, "1": {"text": "Passive"}}}
          ]
        }
      },
      "options": {"tooltip": {"mode": "multi"}},
      "gridPos": {"h": 8, "w": 24, "x": 0, "y": 6}
    },
    {
      "id": 8,
      "type": "row",
      "title": "DB Check Health",
      "collapsed": false,
      "gridPos": {"h": 1, "w": 24, "x": 0, "y": 14}
    },
    {
      "id": 9,
      "type": "timeseries",
      "title": "Check Duration (seconds)",
      "datasource": {"type": "prometheus", "uid": "${datasource_site1}"},
      "targets": [
        {
          "datasource": {"type": "prometheus", "uid": "${datasource_site1}"},
          "expr": "aap_pg_check_duration_seconds",
          "legendFormat": "Site 1"
        },
        {
          "datasource": {"type": "prometheus", "uid": "${datasource_site2}"},
          "expr": "aap_pg_check_duration_seconds",
          "legendFormat": "Site 2"
        }
      ],
      "fieldConfig": {"defaults": {"unit": "s"}},
      "gridPos": {"h": 6, "w": 12, "x": 0, "y": 15}
    },
    {
      "id": 10,
      "type": "stat",
      "title": "Site 1 Last Check Timestamp",
      "datasource": {"type": "prometheus", "uid": "${datasource_site1}"},
      "targets": [{
        "expr": "aap_pg_check_last_run_timestamp_seconds",
        "instant": true,
        "legendFormat": ""
      }],
      "fieldConfig": {"defaults": {"unit": "dateTimeFromNow"}},
      "options": {"reduceOptions": {"calcs": ["lastNotNull"]}},
      "gridPos": {"h": 6, "w": 6, "x": 12, "y": 15}
    },
    {
      "id": 11,
      "type": "stat",
      "title": "Site 2 Last Check Timestamp",
      "datasource": {"type": "prometheus", "uid": "${datasource_site2}"},
      "targets": [{
        "expr": "aap_pg_check_last_run_timestamp_seconds",
        "instant": true,
        "legendFormat": ""
      }],
      "fieldConfig": {"defaults": {"unit": "dateTimeFromNow"}},
      "options": {"reduceOptions": {"calcs": ["lastNotNull"]}},
      "gridPos": {"h": 6, "w": 6, "x": 18, "y": 15}
    },
    {
      "id": 12,
      "type": "row",
      "title": "Pod Health",
      "collapsed": false,
      "gridPos": {"h": 1, "w": 24, "x": 0, "y": 21}
    },
    {
      "id": 13,
      "type": "stat",
      "title": "Site 1 AAP Ready Pods",
      "datasource": {"type": "prometheus", "uid": "${datasource_site1}"},
      "targets": [{
        "expr": "count(kube_pod_status_ready{namespace=\"$ns_site1\", condition=\"true\"})",
        "instant": true,
        "legendFormat": "Ready pods"
      }],
      "fieldConfig": {
        "defaults": {
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {"color": "red", "value": null},
              {"color": "yellow", "value": 1},
              {"color": "green", "value": 4}
            ]
          },
          "color": {"mode": "thresholds"}
        }
      },
      "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background"},
      "gridPos": {"h": 4, "w": 12, "x": 0, "y": 22}
    },
    {
      "id": 14,
      "type": "stat",
      "title": "Site 2 AAP Ready Pods",
      "datasource": {"type": "prometheus", "uid": "${datasource_site2}"},
      "targets": [{
        "expr": "count(kube_pod_status_ready{namespace=\"$ns_site2\", condition=\"true\"})",
        "instant": true,
        "legendFormat": "Ready pods"
      }],
      "fieldConfig": {
        "defaults": {
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {"color": "red", "value": null},
              {"color": "yellow", "value": 1},
              {"color": "green", "value": 4}
            ]
          },
          "color": {"mode": "thresholds"}
        }
      },
      "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background"},
      "gridPos": {"h": 4, "w": 12, "x": 12, "y": 22}
    },
    {
      "id": 15,
      "type": "row",
      "title": "Resource Utilization",
      "collapsed": false,
      "gridPos": {"h": 1, "w": 24, "x": 0, "y": 26}
    },
    {
      "id": 16,
      "type": "timeseries",
      "title": "Site 1 CPU Usage (cores)",
      "datasource": {"type": "prometheus", "uid": "${datasource_site1}"},
      "targets": [{
        "expr": "sum(rate(container_cpu_usage_seconds_total{namespace=\"$ns_site1\", container!=\"\"}[5m])) by (pod)",
        "legendFormat": "{{ pod }}"
      }],
      "fieldConfig": {"defaults": {"unit": "short"}},
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 27}
    },
    {
      "id": 17,
      "type": "timeseries",
      "title": "Site 2 CPU Usage (cores)",
      "datasource": {"type": "prometheus", "uid": "${datasource_site2}"},
      "targets": [{
        "expr": "sum(rate(container_cpu_usage_seconds_total{namespace=\"$ns_site2\", container!=\"\"}[5m])) by (pod)",
        "legendFormat": "{{ pod }}"
      }],
      "fieldConfig": {"defaults": {"unit": "short"}},
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 27}
    },
    {
      "id": 18,
      "type": "timeseries",
      "title": "Site 1 Memory Usage",
      "datasource": {"type": "prometheus", "uid": "${datasource_site1}"},
      "targets": [{
        "expr": "sum(container_memory_working_set_bytes{namespace=\"$ns_site1\", container!=\"\"}) by (pod)",
        "legendFormat": "{{ pod }}"
      }],
      "fieldConfig": {"defaults": {"unit": "bytes"}},
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 35}
    },
    {
      "id": 19,
      "type": "timeseries",
      "title": "Site 2 Memory Usage",
      "datasource": {"type": "prometheus", "uid": "${datasource_site2}"},
      "targets": [{
        "expr": "sum(container_memory_working_set_bytes{namespace=\"$ns_site2\", container!=\"\"}) by (pod)",
        "legendFormat": "{{ pod }}"
      }],
      "fieldConfig": {"defaults": {"unit": "bytes"}},
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 35}
    }
  ]
}
```

- [ ] **Step 3: Validate dashboard JSON is parseable**

```bash
python3 -c "import json; json.load(open('files/grafana/dashboard.json')); print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add files/grafana/
git commit -m "feat: add grafana datasource config and dashboard JSON"
```

---

### Task 4: Ansible Playbook — Deploy Everything

**Files:**
- Create: `playbooks/deploy_dashboard/deploy_dashboard.yml`
- Create: `playbooks/deploy_dashboard/tasks/apply_ocp_manifests.yml`
- Create: `playbooks/deploy_dashboard/tasks/provision_datasources.yml`
- Create: `playbooks/deploy_dashboard/tasks/provision_dashboard.yml`
- Modify: `vars/main.yml.example`

**Interfaces:**
- Consumes: `vars/main.yml` variables (all listed under Global Constraints plus the new vars below).
- Consumes: OCP kubeconfig/`K8S_AUTH_*` env vars for each cluster (same pattern as existing playbooks).
- Consumes: `grafana_api_token`, `grafana_url`, Thanos vars from `vars/main.yml`.
- Produces: Running `aap-site-metrics` Deployment on both clusters + provisioned Grafana datasources and dashboard.

- [ ] **Step 1: Add new variables to `vars/main.yml.example`**

Append to the end of `vars/main.yml.example`:

```yaml
# --- Observability Dashboard ---

# Grafana instance (pre-existing, external host)
grafana_url: "https://grafana.example.com"
grafana_api_token: ""  # Grafana service account token with Editor role

# postgres_check container image (must match the image used in openshift-deployment.example.yaml)
postgres_check_image: "quay.io/chrhamme/postgres-check:v3"

# OCP Thanos Querier endpoints — accessible from the Grafana host
# Find via: oc -n openshift-monitoring get route thanos-querier -o jsonpath='{.spec.host}'
thanos_querier_site_one: ""   # hostname only, e.g. thanos-querier-openshift-monitoring.apps.site1.example.com
thanos_querier_site_two: ""   # hostname only, e.g. thanos-querier-openshift-monitoring.apps.site2.example.com

# OCP Service Account tokens with cluster-monitoring-view role for each cluster
# Create via: oc -n openshift-monitoring create token prometheus-k8s --duration=8760h
thanos_bearer_token_site_one: ""
thanos_bearer_token_site_two: ""

# Site labels — must match the SITE_LABEL env var used in the Deployment (and the site label in ServiceMonitor)
site_label_one: "site1"
site_label_two: "site2"
```

- [ ] **Step 2: Create `playbooks/deploy_dashboard/tasks/apply_ocp_manifests.yml`**

```yaml
---
# Apply OCP monitoring manifests to one site.
# Expected vars: site_namespace, site_label, postgres_check_image

- name: Apply aap-site-metrics Deployment
  kubernetes.core.k8s:
    state: present
    definition: "{{ lookup('ansible.builtin.template', '../../files/monitoring/metrics-deployment.yml') | from_yaml }}"

- name: Apply aap-site-metrics Service
  kubernetes.core.k8s:
    state: present
    definition: "{{ lookup('ansible.builtin.template', '../../files/monitoring/metrics-service.yml') | from_yaml }}"

- name: Apply ServiceMonitor
  kubernetes.core.k8s:
    state: present
    definition: "{{ lookup('ansible.builtin.template', '../../files/monitoring/service-monitor.yml') | from_yaml }}"

- name: Apply PrometheusRule
  kubernetes.core.k8s:
    state: present
    definition: "{{ lookup('ansible.builtin.template', '../../files/monitoring/prometheus-rule.yml') | from_yaml }}"
```

- [ ] **Step 3: Create `playbooks/deploy_dashboard/tasks/provision_datasources.yml`**

```yaml
---
# Provision both Prometheus datasources in Grafana via the HTTP API.
# Idempotent: uses POST /api/datasources — Grafana returns 409 if name exists; we treat that as OK.

- name: Provision Site 1 Prometheus datasource
  ansible.builtin.uri:
    url: "{{ grafana_url }}/api/datasources"
    method: POST
    headers:
      Authorization: "Bearer {{ grafana_api_token }}"
      Content-Type: "application/json"
    body_format: json
    body:
      name: "AAP Site 1"
      type: "prometheus"
      access: "proxy"
      url: "https://{{ thanos_querier_site_one }}"
      basicAuth: false
      jsonData:
        httpMethod: "GET"
        httpHeaderName1: "Authorization"
      secureJsonData:
        httpHeaderValue1: "Bearer {{ thanos_bearer_token_site_one }}"
      isDefault: false
      editable: true
    status_code: [200, 201, 409]
    validate_certs: true

- name: Provision Site 2 Prometheus datasource
  ansible.builtin.uri:
    url: "{{ grafana_url }}/api/datasources"
    method: POST
    headers:
      Authorization: "Bearer {{ grafana_api_token }}"
      Content-Type: "application/json"
    body_format: json
    body:
      name: "AAP Site 2"
      type: "prometheus"
      access: "proxy"
      url: "https://{{ thanos_querier_site_two }}"
      basicAuth: false
      jsonData:
        httpMethod: "GET"
        httpHeaderName1: "Authorization"
      secureJsonData:
        httpHeaderValue1: "Bearer {{ thanos_bearer_token_site_two }}"
      isDefault: false
      editable: true
    status_code: [200, 201, 409]
    validate_certs: true
```

- [ ] **Step 4: Create `playbooks/deploy_dashboard/tasks/provision_dashboard.yml`**

```yaml
---
# Import the AAP Multisite Observability dashboard into Grafana.
# POST /api/dashboards/import — overwrites on uid collision (idempotent).

- name: Read dashboard JSON
  ansible.builtin.set_fact:
    dashboard_json: "{{ lookup('ansible.builtin.file', '../../files/grafana/dashboard.json') | from_json }}"

- name: Import dashboard to Grafana
  ansible.builtin.uri:
    url: "{{ grafana_url }}/api/dashboards/import"
    method: POST
    headers:
      Authorization: "Bearer {{ grafana_api_token }}"
      Content-Type: "application/json"
    body_format: json
    body:
      dashboard: "{{ dashboard_json }}"
      overwrite: true
      folderId: 0
    status_code: [200]
    validate_certs: true
  register: grafana_import_result

- name: Show dashboard URL
  ansible.builtin.debug:
    msg: "Dashboard imported: {{ grafana_url }}{{ grafana_import_result.json.url }}"
```

- [ ] **Step 5: Create `playbooks/deploy_dashboard/deploy_dashboard.yml`**

```yaml
---
# Deploy AAP multisite observability dashboard.
#
# Prerequisites:
#   - OCP User Workload Monitoring enabled on both clusters
#   - postgres-check-db and postgres-check-webhook Secrets already exist in both namespaces
#     (created by openshift-deployment.example.yaml setup)
#   - K8S_AUTH_* env vars or kubeconfig set for each cluster before running
#
# Usage (site 1 kubeconfig active):
#   ansible-playbook playbooks/deploy_dashboard/deploy_dashboard.yml \
#     -e k8s_context_site_one=<ctx> -e k8s_context_site_two=<ctx>
#
# Or set KUBECONFIG to a merged kubeconfig and pass context names via extra vars.

- name: Apply OCP manifests to Site 1
  hosts: localhost
  gather_facts: false
  vars_files:
    - "../../vars/main.yml"
  vars:
    site_namespace: "{{ namespace_site_one }}"
    site_label: "{{ site_label_one }}"
  tasks:
    - name: Apply monitoring manifests to site one
      ansible.builtin.include_tasks: tasks/apply_ocp_manifests.yml
      environment:
        K8S_AUTH_CONTEXT: "{{ k8s_context_site_one | default(omit) }}"

- name: Apply OCP manifests to Site 2
  hosts: localhost
  gather_facts: false
  vars_files:
    - "../../vars/main.yml"
  vars:
    site_namespace: "{{ namespace_site_two }}"
    site_label: "{{ site_label_two }}"
  tasks:
    - name: Apply monitoring manifests to site two
      ansible.builtin.include_tasks: tasks/apply_ocp_manifests.yml
      environment:
        K8S_AUTH_CONTEXT: "{{ k8s_context_site_two | default(omit) }}"

- name: Provision Grafana
  hosts: localhost
  gather_facts: false
  vars_files:
    - "../../vars/main.yml"
  tasks:
    - name: Provision datasources
      ansible.builtin.include_tasks: tasks/provision_datasources.yml

    - name: Provision dashboard
      ansible.builtin.include_tasks: tasks/provision_dashboard.yml
```

- [ ] **Step 5b: Create `playbooks/deploy_dashboard/tasks/provision_alerts.yml`**

Cross-site Grafana alerts (SplitBrain, NoPrimary) are provisioned as Grafana Unified Alerting rules via the ruler API. These require two datasource queries combined with a Math expression. The folder `"AAP Multisite"` is created automatically if it does not exist.

```yaml
---
# Provision cross-site Grafana alert rules.
# Uses Grafana Unified Alerting ruler API — requires Grafana 9+ with unified alerting enabled.

- name: Create alert rule group for cross-site AAP alerts
  ansible.builtin.uri:
    url: "{{ grafana_url }}/api/ruler/grafana/api/v1/rules/AAP%20Multisite"
    method: POST
    headers:
      Authorization: "Bearer {{ grafana_api_token }}"
      Content-Type: "application/json"
    body_format: json
    body:
      name: "aap-cross-site-alerts"
      interval: "1m"
      rules:
        - grafana_alert:
            title: "AAPSiteSplitBrain"
            condition: "C"
            no_data_state: "NoData"
            exec_err_state: "Error"
            for: "1m"
            labels:
              severity: "critical"
            annotations:
              summary: "Both AAP sites report as primary (split-brain)"
              description: >
                Both Site 1 and Site 2 report aap_site_in_recovery=0.
                Two PostgreSQL primaries may be active simultaneously.
            data:
              - refId: "A"
                datasourceUid: "${datasource_site1}"
                model:
                  expr: "aap_site_in_recovery"
                  instant: true
                  refId: "A"
                relativeTimeRange:
                  from: 300
                  to: 0
              - refId: "B"
                datasourceUid: "${datasource_site2}"
                model:
                  expr: "aap_site_in_recovery"
                  instant: true
                  refId: "B"
                relativeTimeRange:
                  from: 300
                  to: 0
              - refId: "C"
                datasourceUid: "__expr__"
                model:
                  type: "math"
                  expression: "$A == 0 && $B == 0"
                  refId: "C"

        - grafana_alert:
            title: "AAPNoPrimary"
            condition: "C"
            no_data_state: "NoData"
            exec_err_state: "Error"
            for: "1m"
            labels:
              severity: "critical"
            annotations:
              summary: "No AAP site is active (no primary)"
              description: >
                Both Site 1 and Site 2 report aap_site_in_recovery=1.
                No PostgreSQL primary is currently active.
            data:
              - refId: "A"
                datasourceUid: "${datasource_site1}"
                model:
                  expr: "aap_site_in_recovery"
                  instant: true
                  refId: "A"
                relativeTimeRange:
                  from: 300
                  to: 0
              - refId: "B"
                datasourceUid: "${datasource_site2}"
                model:
                  expr: "aap_site_in_recovery"
                  instant: true
                  refId: "B"
                relativeTimeRange:
                  from: 300
                  to: 0
              - refId: "C"
                datasourceUid: "__expr__"
                model:
                  type: "math"
                  expression: "$A == 1 && $B == 1"
                  refId: "C"
    status_code: [202]
    validate_certs: true
```

Add a call to this task file in `deploy_dashboard.yml` after `provision_dashboard.yml`:

```yaml
    - name: Provision cross-site Grafana alert rules
      ansible.builtin.include_tasks: tasks/provision_alerts.yml
```

**Note on datasource UIDs:** The `datasourceUid` values above use the variable names from the dashboard template variables (`${datasource_site1}`). For the ruler API, these must be replaced with the **actual UID** of each provisioned datasource. Retrieve them after provisioning datasources:

```bash
curl -s -H "Authorization: Bearer <token>" \
  https://grafana.example.com/api/datasources/name/AAP%20Site%201 | python3 -m json.tool | grep '"uid"'
```

Set these as extra vars when running the playbook:

```bash
ansible-playbook playbooks/deploy_dashboard/deploy_dashboard.yml \
  -e grafana_ds_uid_site1=<uid1> \
  -e grafana_ds_uid_site2=<uid2>
```

And replace `${datasource_site1}` / `${datasource_site2}` in `provision_alerts.yml` with `{{ grafana_ds_uid_site1 }}` and `{{ grafana_ds_uid_site2 }}`.

- [ ] **Step 6: Verify YAML syntax on playbook files**

```bash
python3 -c "
import yaml
for f in [
  'playbooks/deploy_dashboard/deploy_dashboard.yml',
  'playbooks/deploy_dashboard/tasks/apply_ocp_manifests.yml',
  'playbooks/deploy_dashboard/tasks/provision_datasources.yml',
  'playbooks/deploy_dashboard/tasks/provision_dashboard.yml',
  'playbooks/deploy_dashboard/tasks/provision_alerts.yml',
  'vars/main.yml.example',
]:
    yaml.safe_load(open(f))
    print(f'OK: {f}')
"
```

Expected: six `OK:` lines.

- [ ] **Step 7: Run end-to-end deployment dry-run**

With a kubeconfig pointing at the target clusters and `vars/main.yml` filled in:

```bash
ansible-playbook playbooks/deploy_dashboard/deploy_dashboard.yml \
  -e k8s_context_site_one=<ctx1> \
  -e k8s_context_site_two=<ctx2> \
  --check
```

Expected: no failures in `--check` mode. Grafana `uri` tasks will be skipped (check mode skips non-idempotent actions by default); that is acceptable.

- [ ] **Step 8: Apply for real and verify**

```bash
ansible-playbook playbooks/deploy_dashboard/deploy_dashboard.yml \
  -e k8s_context_site_one=<ctx1> \
  -e k8s_context_site_two=<ctx2>
```

Then verify:

```bash
# Confirm Deployment is running on site 1
oc --context <ctx1> -n aap-26 rollout status deployment/aap-site-metrics

# Port-forward and check metrics endpoint
oc --context <ctx1> -n aap-26 port-forward deployment/aap-site-metrics 8080:8080 &
curl -s localhost:8080/metrics | grep aap_
kill %1

# Confirm Grafana dashboard imported
curl -s -H "Authorization: Bearer <token>" \
  https://grafana.example.com/api/dashboards/uid/aap-multisite-obs | python3 -m json.tool | grep title
```

Expected: `aap_site_in_recovery`, `aap_pg_check_success` in curl output; Grafana API returns the dashboard title.

- [ ] **Step 9: Commit**

```bash
git add playbooks/deploy_dashboard/ vars/main.yml.example
git commit -m "feat: add deploy_dashboard playbook for OCP manifests and Grafana provisioning"
```

---

## Grafana Alert Rule Notes

The `provision_alerts.yml` task uses `${datasource_site1}` / `${datasource_site2}` as placeholder datasource UIDs. Before running the full playbook, retrieve the actual UIDs after datasources are provisioned (Step 5, Task 4):

```bash
# After provision_datasources runs:
SITE1_UID=$(curl -s -H "Authorization: Bearer $GRAFANA_TOKEN" \
  $GRAFANA_URL/api/datasources/name/AAP%20Site%201 | python3 -c "import sys,json; print(json.load(sys.stdin)['uid'])")

SITE2_UID=$(curl -s -H "Authorization: Bearer $GRAFANA_TOKEN" \
  $GRAFANA_URL/api/datasources/name/AAP%20Site%202 | python3 -c "import sys,json; print(json.load(sys.stdin)['uid'])")

ansible-playbook playbooks/deploy_dashboard/deploy_dashboard.yml \
  -e grafana_ds_uid_site1=$SITE1_UID \
  -e grafana_ds_uid_site2=$SITE2_UID \
  ...
```

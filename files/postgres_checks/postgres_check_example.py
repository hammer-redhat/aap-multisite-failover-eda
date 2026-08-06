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

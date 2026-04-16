"""
PostgreSQL recovery check → JSON payload POST to Event Stream webhook.

Configuration via environment (OpenShift/Kubernetes):

  Required:
    WEBHOOK_URL       Event Stream / webhook URL
    AUTH_TOKEN        Bearer token for the webhook

  Database — either:
    DB_CONFIG         JSON object: {"host","port","dbname","user","password"}
  or:
    PGHOST, PGUSER, PGPASSWORD
    PGPORT (default 5432), PGDATABASE or PG_DBNAME (default awx)

  Optional:
    WEBHOOK_SSL_VERIFY  "true" or "false" (default: true)
"""

from __future__ import annotations

import json
import os
import sys

import psycopg2
import requests


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
        os.environ.get("PGDATABASE")
        or os.environ.get("PG_DBNAME")
        or "awx"
    ).strip()

    return {
        "host": host,
        "port": port,
        "dbname": dbname,
        "user": user,
        "password": password,
    }


def main() -> int:
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    auth_token = os.environ.get("AUTH_TOKEN", "").strip()
    if not webhook_url or not auth_token:
        print(
            json.dumps(
                {
                    "error": "WEBHOOK_URL and AUTH_TOKEN environment variables are required"
                }
            ),
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

    payload: dict

    try:
        conn = psycopg2.connect(**db_config)
        cur = conn.cursor()
        cur.execute("SELECT pg_is_in_recovery();")
        in_recovery = cur.fetchone()[0]
        cur.close()
        conn.close()
        payload = {"in_recovery": in_recovery}
    except Exception as e:
        payload = {"error": str(e)}

    print(json.dumps(payload, indent=4))

    try:
        response = requests.post(
            webhook_url, json=payload, headers=headers, timeout=60, verify=verify
        )
    except requests.RequestException as e:
        print(json.dumps({"error": f"webhook request failed: {e}"}), file=sys.stderr)
        return 1

    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")
    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

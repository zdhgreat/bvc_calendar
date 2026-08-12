"""Create all tables from app/schema.sql. Idempotent."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg2
from psycopg2.errors import DuplicateDatabase

from app.db import get_conn


def _ensure_database_exists() -> None:
    """Connect to the default 'postgres' database and create target DB if missing."""
    dsn = {
        "host": os.environ.get("POSTGRES_HOST", "localhost"),
        "port": int(os.environ.get("POSTGRES_PORT", "5432")),
        "user": os.environ.get("POSTGRES_USER", "calendar_user"),
        "password": os.environ.get("POSTGRES_PASSWORD", "calendar_password"),
        "dbname": "postgres",
    }
    target_db = os.environ.get("POSTGRES_DB", "financial_calendar")

    conn = psycopg2.connect(**dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE {target_db}")
        print(f"[init_db] created database {target_db}")
    except DuplicateDatabase:
        print(f"[init_db] database {target_db} already exists")
    finally:
        conn.close()


def main() -> None:
    _ensure_database_exists()

    schema = Path(__file__).resolve().parent.parent / "app" / "schema.sql"
    sql = schema.read_text(encoding="utf-8")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    print(f"[init_db] schema applied from {schema}")


if __name__ == "__main__":
    main()

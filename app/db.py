"""PostgreSQL connection helper."""
from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()


def _dsn() -> dict:
    return {
        "host": os.environ.get("POSTGRES_HOST", "localhost"),
        "port": int(os.environ.get("POSTGRES_PORT", "5432")),
        "user": os.environ.get("POSTGRES_USER", "calendar_user"),
        "password": os.environ.get("POSTGRES_PASSWORD", "calendar_password"),
        "dbname": os.environ.get("POSTGRES_DB", "financial_calendar"),
    }


@contextmanager
def get_conn():
    conn = psycopg2.connect(**_dsn())
    try:
        yield conn
    finally:
        conn.close()


def query_all(sql: str, params: tuple | None = None) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return [dict(r) for r in cur.fetchall()]

"""Create all tables from app/schema.sql. Idempotent."""
from __future__ import annotations

from pathlib import Path

from app.db import get_conn


def main() -> None:
    schema = Path(__file__).resolve().parent.parent / "app" / "schema.sql"
    sql = schema.read_text(encoding="utf-8")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    print(f"[init_db] schema applied from {schema}")


if __name__ == "__main__":
    main()

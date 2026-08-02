"""Self-check for the data feed (pull model).

DB-free: validates the per-kind field map and the WHERE-clause builder that
/api/feed and /api/event rely on. Does not exercise the network layer.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.routers.calendar import _KIND_SPEC, _feed_where, _parse_dt


def test_kind_spec_has_identity_and_sync_keys():
    """Every kind must expose id + source_id (bbs dedup) and fetched_at (since)."""
    for kind, spec in _KIND_SPEC.items():
        assert "id" in spec["fields"], f"{kind} missing id"
        assert "source_id" in spec["fields"], f"{kind} missing source_id"
        assert "fetched_at" in spec["fields"], f"{kind} missing fetched_at"
        assert spec["date_col"] in spec["fields"], f"{kind} date_col not in fields"


def test_feed_where_clauses():
    spec = _KIND_SPEC["economic"]
    since = _parse_dt("2026-08-01T00:00:00Z")
    df = datetime(2026, 8, 1)
    dt = datetime(2026, 8, 31)

    where, params = _feed_where(spec, since, df, dt)
    assert where.count("%s") == 3
    assert "fetched_at" in where and "event_time" in where
    assert params == [since, df, dt]

    where_none, params_none = _feed_where(spec, None, None, None)
    assert where_none == "" and params_none == []


def test_parse_dt_handles_z_suffix():
    assert _parse_dt("2026-08-01T00:00:00Z").tzinfo is not None
    assert _parse_dt("2026-08-01T00:00:00+08:00").utcoffset() is not None


if __name__ == "__main__":
    test_kind_spec_has_identity_and_sync_keys()
    test_feed_where_clauses()
    test_parse_dt_handles_z_suffix()
    print("ok — feed shape + where builder verified")

"""Self-check: ensure_topic records map only once, returns existing on re-call.

Doesn't require bbs-go. Uses a temp SQLite? No — runner is PG-coupled.
Skip if PG not available; this is a smoke test, not CI.
"""
from __future__ import annotations

import os
import sys

import pytest


@pytest.mark.skipif(
    not os.environ.get("POSTGRES_DB"),
    reason="needs PG (set POSTGRES_* env to run)",
)
def test_ensure_topic_records_once():
    # import lazily so the module's load_dotenv() doesn't crash collection on missing deps
    from app.bbs_integration import ensure_topic, _lookup_map

    kind = "test"
    sid = f"sid-{os.getpid()}"
    # bbs-go not configured → returns -event_id (stub)
    t1 = ensure_topic(kind=kind, source_id=sid, table="economic_events", event_id=999999)
    assert t1 is not None
    # second call returns the same id via map lookup, no duplicate insert
    t2 = ensure_topic(kind=kind, source_id=sid, table="economic_events", event_id=999999)
    assert t2 == t1
    assert _lookup_map(kind, sid) == t1

"""FastAPI entrypoint — read-only data interface (no UI)."""
from __future__ import annotations

from fastapi import FastAPI

from app.routers import calendar

app = FastAPI(title="financial-calendar", version="0.2.0")
app.include_router(calendar.router)


@app.get("/health")
def health() -> dict:
    return {"ok": True}

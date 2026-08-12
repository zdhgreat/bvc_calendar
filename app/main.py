"""FastAPI entrypoint — read-only data interface (no UI)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import calendar

app = FastAPI(title="financial-calendar", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
)
app.include_router(calendar.router)


@app.get("/health")
def health() -> dict:
    return {"ok": True}

"""FastAPI entrypoint."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.routers import calendar

app = FastAPI(title="financial-calendar", version="0.1.0")

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
# expose to router via app.state so we don't repeat the dir lookup
app.state.templates = templates

app.include_router(calendar.router)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "calendar.html")

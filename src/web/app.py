from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from web.routes.tasks import router as tasks_router


app = FastAPI(title="smart-tasker web", version="0.1.0")


@app.get("/health")
async def health():
    return JSONResponse({"ok": True})


app.include_router(tasks_router)

# --- Frontend (Telegram WebApp) ---
WEBAPP_DIR = (Path(__file__).resolve().parent.parent / "webapp").resolve()
if WEBAPP_DIR.exists():
    # Важно: монтируем ПОСЛЕ /api, чтобы /api/* не перехватывались статика-роутом.
    app.mount("/", StaticFiles(directory=str(WEBAPP_DIR), html=True), name="webapp")



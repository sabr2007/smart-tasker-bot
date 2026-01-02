from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from web.routes.tasks import router as tasks_router
from web.routes.users import router as users_router


app = FastAPI(title="smart-tasker web", version="0.1.0")

# CORS middleware для Telegram WebApp и внешних клиентов
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # В production — конкретные домены
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    import db
    try:
        async with db.get_connection() as conn:
            await conn.fetchval("SELECT 1")
        return JSONResponse({"ok": True, "db": "connected"})
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": str(e)},
            status_code=503
        )


app.include_router(tasks_router)
app.include_router(users_router)

# --- Frontend (Telegram WebApp) ---
WEBAPP_DIR = (Path(__file__).resolve().parent.parent / "webapp").resolve()
if WEBAPP_DIR.exists():
    # Важно: монтируем ПОСЛЕ /api, чтобы /api/* не перехватывались статика-роутом.
    app.mount("/", StaticFiles(directory=str(WEBAPP_DIR), html=True), name="webapp")



# src/db.py

import json
import os
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

import aiosqlite

from time_utils import normalize_deadline_iso, now_local_iso

DB_PATH = os.getenv("DB_PATH")
if not DB_PATH:
    # По умолчанию используем tasks.db рядом с исходниками (src/tasks.db),
    # чтобы бот и WebApp гарантированно смотрели в один и тот же файл вне зависимости от cwd.
    DB_PATH = str((Path(__file__).resolve().parent / "tasks.db"))


@asynccontextmanager
async def get_connection():
    """Создаёт асинхронное подключение к SQLite (aiosqlite)."""
    conn = await aiosqlite.connect(DB_PATH, timeout=30)
    try:
        # Чуть лучше поведение при конкурирующих чтениях/записях.
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        try:
            await conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass
        try:
            await conn.execute("PRAGMA busy_timeout=30000")
        except Exception:
            pass
        yield conn
    finally:
        await conn.close()


async def init_db():
    """
    Создаёт таблицы tasks/events/tasks_history, если они не существуют.
    Проводит миграции (добавляет колонки), если база старая.
    """
    async with get_connection() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                due_at TEXT,
                remind_at TEXT,            -- следующее запланированное напоминание (ISO)
                remind_offset_min INTEGER, -- предпочтение "за сколько" (минут) относительно due_at; null если задано абсолютное remind_at
                notified INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                completed_at TEXT,
                category TEXT  -- Новое поле для категорий (на будущее)
            )
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                task_id INTEGER,
                meta TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                due_at TEXT,
                status TEXT,
                created_at TEXT,
                completed_at TEXT,
                deleted_at TEXT,
                category TEXT,
                source TEXT,
                reason TEXT,
                archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # --- Миграции (на случай старой базы) ---
        async with conn.execute("PRAGMA table_info(tasks)") as cur:
            columns = [row[1] for row in await cur.fetchall()]

        if "due_at" not in columns:
            await conn.execute("ALTER TABLE tasks ADD COLUMN due_at TEXT")
        if "notified" not in columns:
            await conn.execute("ALTER TABLE tasks ADD COLUMN notified INTEGER DEFAULT 0")
        if "status" not in columns:
            await conn.execute("ALTER TABLE tasks ADD COLUMN status TEXT DEFAULT 'active'")
        if "completed_at" not in columns:
            await conn.execute("ALTER TABLE tasks ADD COLUMN completed_at TEXT")
        if "category" not in columns:
            await conn.execute("ALTER TABLE tasks ADD COLUMN category TEXT")
        if "remind_at" not in columns:
            await conn.execute("ALTER TABLE tasks ADD COLUMN remind_at TEXT")
        if "remind_offset_min" not in columns:
            await conn.execute("ALTER TABLE tasks ADD COLUMN remind_offset_min INTEGER")

        # --- Нормализация TZ в уже сохранённых данных (фиксируем +05:00, убираем +06:00) ---
        try:
            async with conn.execute("SELECT id, due_at FROM tasks WHERE due_at IS NOT NULL") as cur:
                rows = await cur.fetchall()
            for task_id, due_at in rows:
                norm = normalize_deadline_iso(due_at)
                if norm and norm != due_at:
                    await conn.execute("UPDATE tasks SET due_at = ? WHERE id = ?", (norm, task_id))
        except Exception:
            # миграция best-effort
            pass

        try:
            async with conn.execute("SELECT id, due_at FROM tasks_history WHERE due_at IS NOT NULL") as cur:
                rows = await cur.fetchall()
            for hid, due_at in rows:
                norm = normalize_deadline_iso(due_at)
                if norm and norm != due_at:
                    await conn.execute("UPDATE tasks_history SET due_at = ? WHERE id = ?", (norm, hid))
        except Exception:
            pass

        await conn.commit()


# ======== USER SETTINGS (Timezone) ========

DEFAULT_TIMEZONE = "Asia/Almaty"


async def _ensure_users_table():
    """Creates users table if not exists."""
    async with get_connection() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                timezone TEXT DEFAULT 'Asia/Almaty',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.commit()


async def get_user_timezone(user_id: int) -> str:
    """Returns IANA timezone string for user, or default if not set."""
    await _ensure_users_table()
    async with get_connection() as conn:
        async with conn.execute(
            "SELECT timezone FROM users WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
    if row and row[0]:
        return row[0]
    return DEFAULT_TIMEZONE


async def set_user_timezone(user_id: int, tz: str) -> None:
    """Creates or updates user timezone setting."""
    await _ensure_users_table()
    async with get_connection() as conn:
        # Upsert: INSERT OR REPLACE
        await conn.execute(
            """
            INSERT INTO users (user_id, timezone, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                timezone = excluded.timezone,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, tz),
        )
        await conn.commit()


async def get_user_settings(user_id: int) -> dict:
    """Returns user settings dict with timezone."""
    tz = await get_user_timezone(user_id)
    return {"user_id": user_id, "timezone": tz}


async def add_task(
    user_id: int,
    text: str,
    due_at_iso: Optional[str] = None,
    category: Optional[str] = None,
) -> int:
    """Добавляет задачу и возвращает её ID."""
    # Note: due_at_iso should already be in UTC format when coming from handlers
    remind_at_iso = due_at_iso if due_at_iso else None
    remind_offset_min = 0 if due_at_iso else None
    async with get_connection() as conn:
        cur = await conn.execute(
            "INSERT INTO tasks (user_id, text, due_at, remind_at, remind_offset_min, category) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, text, due_at_iso, remind_at_iso, remind_offset_min, category),
        )
        await conn.commit()
        return int(cur.lastrowid)


async def get_tasks(user_id: int) -> list[tuple[int, str, Optional[str]]]:
    """
    Возвращает список активных задач: (id, text, due_at).
    Сортировка: сначала с дедлайнами (по возрастанию), потом остальные.
    """
    async with get_connection() as conn:
        async with conn.execute(
            """
            SELECT id, text, due_at
            FROM tasks
            WHERE user_id = ?
              AND (status IS NULL OR status = 'active')
            ORDER BY
                CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,
                due_at ASC,
                id DESC
            """,
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
            return rows


async def get_task(user_id: int, task_id: int) -> Optional[tuple[int, str, Optional[str]]]:
    """Возвращает одну задачу (id, text, due_at) или None."""
    async with get_connection() as conn:
        async with conn.execute(
            "SELECT id, text, due_at FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        ) as cur:
            return await cur.fetchone()


async def _fetch_task_row(conn: aiosqlite.Connection, user_id: int, task_id: int) -> Optional[dict]:
    """Возвращает полную строку задачи (dict) или None."""
    async with conn.execute(
        """
        SELECT id, user_id, text, created_at, due_at, remind_at, remind_offset_min, status, completed_at, category
        FROM tasks
        WHERE id = ? AND user_id = ?
        """,
        (task_id, user_id),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None

    (
        tid,
        uid,
        text,
        created_at,
        due_at,
        remind_at,
        remind_offset_min,
        status,
        completed_at,
        category,
    ) = row
    return {
        "task_id": tid,
        "user_id": uid,
        "text": text,
        "created_at": created_at,
        "due_at": due_at,
        "remind_at": remind_at,
        "remind_offset_min": remind_offset_min,
        "status": status,
        "completed_at": completed_at,
        "category": category,
        "source": None,
    }


async def get_task_reminder_settings(
    user_id: int,
    task_id: int,
) -> tuple[Optional[str], Optional[int], Optional[str], Optional[str]]:
    """
    Возвращает (remind_at, remind_offset_min, due_at, text) для задачи
    или (None, None, None, None), если не найдена.
    """
    async with get_connection() as conn:
        async with conn.execute(
            "SELECT remind_at, remind_offset_min, due_at, text FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None, None, None, None
    remind_at, remind_offset_min, due_at, text = row
    return remind_at, remind_offset_min, due_at, text


async def update_task_reminder_settings(
    user_id: int,
    task_id: int,
    *,
    remind_at_iso: Optional[str],
    remind_offset_min: Optional[int],
) -> None:
    """
    Обновляет настройки напоминания у задачи.
    - remind_at_iso: следующее напоминание (ISO) или None (не напоминать)
    - remind_offset_min: предпочтение "за сколько" в минутах (0/5/30/60/...), или None если remind_at задан как абсолютное время
    """
    # Note: remind_at_iso should already be normalized before reaching here
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE tasks SET remind_at = ?, remind_offset_min = ? WHERE id = ? AND user_id = ?",
            (remind_at_iso, remind_offset_min, task_id, user_id),
        )
        await conn.commit()


async def _archive_task_snapshot(
    conn: aiosqlite.Connection,
    task_row: Optional[dict],
    reason: str,
    *,
    deleted_at: Optional[str] = None,
) -> None:
    """
    Сохраняет копию задачи в аналитический архив.
    Не влияет на пользовательский интерфейс.
    """
    if not task_row:
        return

    await conn.execute(
        """
        INSERT INTO tasks_history (
            task_id, user_id, text, due_at, status, created_at,
            completed_at, deleted_at, category, source, reason
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_row.get("task_id"),
            task_row.get("user_id"),
            task_row.get("text"),
            task_row.get("due_at"),
            task_row.get("status"),
            task_row.get("created_at"),
            task_row.get("completed_at"),
            deleted_at or task_row.get("deleted_at"),
            task_row.get("category"),
            task_row.get("source"),
            reason,
        ),
    )


async def update_task_due(user_id: int, task_id: int, due_at_iso: Optional[str]):
    """Обновляет дедлайн задачи."""
    # Note: due_at_iso should already be normalized before reaching here
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE tasks SET due_at = ? WHERE id = ? AND user_id = ?",
            (due_at_iso, task_id, user_id),
        )
        await conn.commit()


async def update_task_text(user_id: int, task_id: int, new_text: str):
    """Обновляет текст задачи."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE tasks SET text = ? WHERE id = ? AND user_id = ?",
            (new_text, task_id, user_id),
        )
        await conn.commit()


async def delete_task(user_id: int, task_id: int):
    """Удаляет задачу (физически) + пишет снимок в tasks_history."""
    deleted_at_iso = now_local_iso()
    async with get_connection() as conn:
        task_row = await _fetch_task_row(conn, user_id, task_id)
        if task_row:
            task_row["deleted_at"] = deleted_at_iso
            await _archive_task_snapshot(conn, task_row, reason="deleted", deleted_at=deleted_at_iso)

        await conn.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )
        await conn.commit()


async def set_task_done(user_id: int, task_id: int):
    """Помечает задачу выполненной (status='done') + пишет снимок в tasks_history."""
    now_iso = now_local_iso()
    async with get_connection() as conn:
        task_row = await _fetch_task_row(conn, user_id, task_id)

        await conn.execute(
            """
            UPDATE tasks
            SET status = 'done', completed_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now_iso, task_id, user_id),
        )

        if task_row:
            task_row["status"] = "done"
            task_row["completed_at"] = now_iso
            await _archive_task_snapshot(conn, task_row, reason="completed")

        await conn.commit()


async def set_task_active(user_id: int, task_id: int):
    """Возвращает задачу в активное состояние (status='active', completed_at=NULL)."""
    async with get_connection() as conn:
        task_row = await _fetch_task_row(conn, user_id, task_id)

        await conn.execute(
            """
            UPDATE tasks
            SET status = 'active', completed_at = NULL
            WHERE id = ? AND user_id = ?
            """,
            (task_id, user_id),
        )

        if task_row:
            task_row["status"] = "active"
            await _archive_task_snapshot(conn, task_row, reason="reopened")

        await conn.commit()


async def set_task_archived(user_id: int, task_id: int):
    """Помечает задачу как архивную (status='archived')."""
    async with get_connection() as conn:
        task_row = await _fetch_task_row(conn, user_id, task_id)

        await conn.execute(
            "UPDATE tasks SET status = 'archived' WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )

        if task_row:
            task_row["status"] = "archived"
            await _archive_task_snapshot(conn, task_row, reason="archived")

        await conn.commit()


async def get_archived_tasks(
    user_id: int,
    limit: int = 10,
) -> list[tuple[int, str, Optional[str], Optional[str]]]:
    """Возвращает последние выполненные задачи."""
    async with get_connection() as conn:
        async with conn.execute(
            """
            SELECT id, text, due_at, completed_at
            FROM tasks
            WHERE user_id = ? AND status = 'archived'
            ORDER BY completed_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ) as cur:
            return await cur.fetchall()


async def get_completed_tasks_since(
    user_id: int,
    since_iso: str,
) -> list[tuple[int, str, Optional[str], Optional[str]]]:
    """Возвращает задачи, выполненные после указанного времени (ISO)."""
    async with get_connection() as conn:
        async with conn.execute(
            """
            SELECT id, text, due_at, completed_at
            FROM tasks
            WHERE user_id = ? 
              AND status = 'done'
              AND completed_at >= ?
            ORDER BY completed_at DESC
            """,
            (user_id, since_iso),
        ) as cur:
            return await cur.fetchall()


async def clear_archived_tasks(user_id: int) -> None:
    """Очищает архив выполненных задач пользователя + пишет снимки в tasks_history."""
    async with get_connection() as conn:
        async with conn.execute(
            """
            SELECT id, user_id, text, created_at, due_at, status, completed_at, category
            FROM tasks
            WHERE user_id = ? AND status = 'done'
            """,
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()

        if rows:
            deleted_at_iso = now_local_iso()
            for row in rows:
                (
                    tid,
                    uid,
                    text,
                    created_at,
                    due_at,
                    status,
                    completed_at,
                    category,
                ) = row
                snapshot = {
                    "task_id": tid,
                    "user_id": uid,
                    "text": text,
                    "created_at": created_at,
                    "due_at": due_at,
                    "status": status,
                    "completed_at": completed_at,
                    "category": category,
                    "source": None,
                }
                await _archive_task_snapshot(
                    conn,
                    snapshot,
                    reason="cleared_archive",
                    deleted_at=deleted_at_iso,
                )

        await conn.execute(
            "DELETE FROM tasks WHERE user_id = ? AND status = 'done'",
            (user_id,),
        )
        await conn.commit()


async def log_event(
    user_id: int,
    event_type: str,
    task_id: Optional[int] = None,
    meta: Optional[dict] = None,
):
    """Пишет лог события."""
    meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO events (user_id, event_type, task_id, meta)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, event_type, task_id, meta_json),
        )
        await conn.commit()


async def get_users_with_active_tasks() -> list[int]:
    """Возвращает список user_id, у которых есть активные задачи."""
    async with get_connection() as conn:
        async with conn.execute(
            """
            SELECT DISTINCT user_id
            FROM tasks
            WHERE status IS NULL OR status = 'active'
            """
        ) as cur:
            rows = await cur.fetchall()
            return [row[0] for row in rows]


async def get_active_tasks_with_future_due(now_iso: str):
    """
    Возвращает активные задачи с дедлайном в будущем.
    Используется для восстановления напоминаний после рестарта.
    """
    async with get_connection() as conn:
        async with conn.execute(
            """
            SELECT id, user_id, text, due_at
            FROM tasks
            WHERE (status IS NULL OR status = 'active')
              AND due_at IS NOT NULL
              AND due_at > ?
            ORDER BY due_at ASC
            """,
            (now_iso,),
        ) as cur:
            return await cur.fetchall()


async def get_active_tasks_with_future_remind(now_iso: str):
    """
    Возвращает активные задачи с ближайшим напоминанием в будущем.
    Формат: (id, user_id, text, due_at, remind_at, remind_offset_min)
    """
    async with get_connection() as conn:
        async with conn.execute(
            """
            SELECT id, user_id, text, due_at, remind_at, remind_offset_min
            FROM tasks
            WHERE (status IS NULL OR status = 'active')
              AND remind_at IS NOT NULL
              AND remind_at > ?
            ORDER BY remind_at ASC
            """,
            (now_iso,),
        ) as cur:
            return await cur.fetchall()


async def get_active_tasks_with_future_due_without_remind(now_iso: str):
    """
    Fallback для старых/переходных задач: дедлайн в будущем, но remind_at не задан.
    Формат: (id, user_id, text, due_at)
    """
    async with get_connection() as conn:
        async with conn.execute(
            """
            SELECT id, user_id, text, due_at
            FROM tasks
            WHERE (status IS NULL OR status = 'active')
              AND due_at IS NOT NULL
              AND due_at > ?
              AND remind_at IS NULL
            ORDER BY due_at ASC
            """,
            (now_iso,),
        ) as cur:
            return await cur.fetchall()


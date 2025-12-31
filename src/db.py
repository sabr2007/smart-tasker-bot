# src/db.py
# PostgreSQL version using asyncpg

import json
import os
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg

from time_utils import now_local_iso

# Database URL from Railway (e.g. postgresql://user:pass@host:port/dbname)
DATABASE_URL = os.getenv("DATABASE_URL")

# Connection pool (initialized on startup)
_pool: Optional[asyncpg.Pool] = None


async def init_pool():
    """Initialize the connection pool. Call this on application startup."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=10,
            command_timeout=60,
        )


async def close_pool():
    """Close the connection pool. Call this on application shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def get_connection():
    """Acquire a connection from the pool."""
    global _pool
    if _pool is None:
        await init_pool()
    async with _pool.acquire() as conn:
        yield conn


async def init_db():
    """
    Creates tables (tasks, events, tasks_history, users) if they do not exist.
    Also runs migrations for any missing columns.
    """
    async with get_connection() as conn:
        # Create tasks table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                due_at TEXT,
                remind_at TEXT,
                remind_offset_min INTEGER,
                notified INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                completed_at TEXT,
                category TEXT
            )
            """
        )

        # Create events table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                event_type TEXT NOT NULL,
                task_id INTEGER,
                meta TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Create tasks_history table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks_history (
                id SERIAL PRIMARY KEY,
                task_id INTEGER,
                user_id BIGINT NOT NULL,
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

        # Create users table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                timezone TEXT DEFAULT 'Asia/Almaty',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # --- Migrations (add missing columns) ---
        # Get existing columns for tasks table
        columns = await conn.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'tasks'
            """
        )
        existing_cols = {row["column_name"] for row in columns}

        if "due_at" not in existing_cols:
            await conn.execute("ALTER TABLE tasks ADD COLUMN due_at TEXT")
        if "notified" not in existing_cols:
            await conn.execute("ALTER TABLE tasks ADD COLUMN notified INTEGER DEFAULT 0")
        if "status" not in existing_cols:
            await conn.execute("ALTER TABLE tasks ADD COLUMN status TEXT DEFAULT 'active'")
        if "completed_at" not in existing_cols:
            await conn.execute("ALTER TABLE tasks ADD COLUMN completed_at TEXT")
        if "category" not in existing_cols:
            await conn.execute("ALTER TABLE tasks ADD COLUMN category TEXT")
        if "remind_at" not in existing_cols:
            await conn.execute("ALTER TABLE tasks ADD COLUMN remind_at TEXT")
        if "remind_offset_min" not in existing_cols:
            await conn.execute("ALTER TABLE tasks ADD COLUMN remind_offset_min INTEGER")


# ======== USER SETTINGS (Timezone) ========

DEFAULT_TIMEZONE = "Asia/Almaty"


async def get_user_timezone(user_id: int) -> str:
    """Returns IANA timezone string for user, or default if not set."""
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT timezone FROM users WHERE user_id = $1",
            user_id,
        )
    if row and row["timezone"]:
        return row["timezone"]
    return DEFAULT_TIMEZONE


async def set_user_timezone(user_id: int, tz: str) -> None:
    """Creates or updates user timezone setting."""
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, timezone, updated_at)
            VALUES ($1, $2, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET
                timezone = EXCLUDED.timezone,
                updated_at = CURRENT_TIMESTAMP
            """,
            user_id, tz,
        )


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
    """Adds a task and returns its ID."""
    remind_at_iso = due_at_iso if due_at_iso else None
    remind_offset_min = 0 if due_at_iso else None
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO tasks (user_id, text, due_at, remind_at, remind_offset_min, category)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            user_id, text, due_at_iso, remind_at_iso, remind_offset_min, category,
        )
        return int(row["id"])


async def get_tasks(user_id: int) -> list[tuple[int, str, Optional[str]]]:
    """
    Returns list of active tasks: (id, text, due_at).
    Sorted: tasks with deadlines first (ascending), then others.
    """
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, text, due_at
            FROM tasks
            WHERE user_id = $1
              AND (status IS NULL OR status = 'active')
            ORDER BY
                CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,
                due_at ASC,
                id DESC
            """,
            user_id,
        )
        return [(row["id"], row["text"], row["due_at"]) for row in rows]


async def get_task(user_id: int, task_id: int) -> Optional[tuple[int, str, Optional[str]]]:
    """Returns one task (id, text, due_at) or None."""
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT id, text, due_at FROM tasks WHERE id = $1 AND user_id = $2",
            task_id, user_id,
        )
    if not row:
        return None
    return (row["id"], row["text"], row["due_at"])


async def _fetch_task_row(conn: asyncpg.Connection, user_id: int, task_id: int) -> Optional[dict]:
    """Returns full task row (dict) or None."""
    row = await conn.fetchrow(
        """
        SELECT id, user_id, text, created_at, due_at, remind_at, remind_offset_min, status, completed_at, category
        FROM tasks
        WHERE id = $1 AND user_id = $2
        """,
        task_id, user_id,
    )
    if not row:
        return None

    return {
        "task_id": row["id"],
        "user_id": row["user_id"],
        "text": row["text"],
        "created_at": row["created_at"],
        "due_at": row["due_at"],
        "remind_at": row["remind_at"],
        "remind_offset_min": row["remind_offset_min"],
        "status": row["status"],
        "completed_at": row["completed_at"],
        "category": row["category"],
        "source": None,
    }


async def get_task_reminder_settings(
    user_id: int,
    task_id: int,
) -> tuple[Optional[str], Optional[int], Optional[str], Optional[str]]:
    """
    Returns (remind_at, remind_offset_min, due_at, text) for a task
    or (None, None, None, None) if not found.
    """
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT remind_at, remind_offset_min, due_at, text FROM tasks WHERE id = $1 AND user_id = $2",
            task_id, user_id,
        )
    if not row:
        return None, None, None, None
    return row["remind_at"], row["remind_offset_min"], row["due_at"], row["text"]


async def update_task_reminder_settings(
    user_id: int,
    task_id: int,
    *,
    remind_at_iso: Optional[str],
    remind_offset_min: Optional[int],
) -> None:
    """
    Updates reminder settings for a task.
    - remind_at_iso: next reminder (ISO) or None (no reminder)
    - remind_offset_min: how many minutes before (0/5/30/60/...), or None if absolute time
    """
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE tasks SET remind_at = $1, remind_offset_min = $2 WHERE id = $3 AND user_id = $4",
            remind_at_iso, remind_offset_min, task_id, user_id,
        )


async def _archive_task_snapshot(
    conn: asyncpg.Connection,
    task_row: Optional[dict],
    reason: str,
    *,
    deleted_at: Optional[str] = None,
) -> None:
    """
    Saves a copy of the task to the analytics archive.
    Does not affect the user interface.
    """
    if not task_row:
        return

    await conn.execute(
        """
        INSERT INTO tasks_history (
            task_id, user_id, text, due_at, status, created_at,
            completed_at, deleted_at, category, source, reason
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        """,
        task_row.get("task_id"),
        task_row.get("user_id"),
        task_row.get("text"),
        task_row.get("due_at"),
        task_row.get("status"),
        str(task_row.get("created_at")) if task_row.get("created_at") else None,
        task_row.get("completed_at"),
        deleted_at or task_row.get("deleted_at"),
        task_row.get("category"),
        task_row.get("source"),
        reason,
    )


async def update_task_due(user_id: int, task_id: int, due_at_iso: Optional[str]):
    """Updates the task's deadline."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE tasks SET due_at = $1 WHERE id = $2 AND user_id = $3",
            due_at_iso, task_id, user_id,
        )


async def update_task_text(user_id: int, task_id: int, new_text: str):
    """Updates the task's text."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE tasks SET text = $1 WHERE id = $2 AND user_id = $3",
            new_text, task_id, user_id,
        )


async def delete_task(user_id: int, task_id: int):
    """Deletes a task (physically) and writes snapshot to tasks_history."""
    deleted_at_iso = now_local_iso()
    async with get_connection() as conn:
        task_row = await _fetch_task_row(conn, user_id, task_id)
        if task_row:
            task_row["deleted_at"] = deleted_at_iso
            await _archive_task_snapshot(conn, task_row, reason="deleted", deleted_at=deleted_at_iso)

        await conn.execute(
            "DELETE FROM tasks WHERE id = $1 AND user_id = $2",
            task_id, user_id,
        )


async def set_task_done(user_id: int, task_id: int):
    """Marks task as completed (status='done') and writes snapshot to tasks_history."""
    now_iso = now_local_iso()
    async with get_connection() as conn:
        task_row = await _fetch_task_row(conn, user_id, task_id)

        await conn.execute(
            """
            UPDATE tasks
            SET status = 'done', completed_at = $1
            WHERE id = $2 AND user_id = $3
            """,
            now_iso, task_id, user_id,
        )

        if task_row:
            task_row["status"] = "done"
            task_row["completed_at"] = now_iso
            await _archive_task_snapshot(conn, task_row, reason="completed")


async def set_task_active(user_id: int, task_id: int):
    """Returns task to active state (status='active', completed_at=NULL)."""
    async with get_connection() as conn:
        task_row = await _fetch_task_row(conn, user_id, task_id)

        await conn.execute(
            """
            UPDATE tasks
            SET status = 'active', completed_at = NULL
            WHERE id = $1 AND user_id = $2
            """,
            task_id, user_id,
        )

        if task_row:
            task_row["status"] = "active"
            await _archive_task_snapshot(conn, task_row, reason="reopened")


async def set_task_archived(user_id: int, task_id: int):
    """Marks task as archived (status='archived')."""
    async with get_connection() as conn:
        task_row = await _fetch_task_row(conn, user_id, task_id)

        await conn.execute(
            "UPDATE tasks SET status = 'archived' WHERE id = $1 AND user_id = $2",
            task_id, user_id,
        )

        if task_row:
            task_row["status"] = "archived"
            await _archive_task_snapshot(conn, task_row, reason="archived")


async def get_archived_tasks(
    user_id: int,
    limit: int = 10,
) -> list[tuple[int, str, Optional[str], Optional[str]]]:
    """Returns last completed tasks."""
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, text, due_at, completed_at
            FROM tasks
            WHERE user_id = $1 AND status = 'archived'
            ORDER BY completed_at DESC
            LIMIT $2
            """,
            user_id, limit,
        )
        return [(row["id"], row["text"], row["due_at"], row["completed_at"]) for row in rows]


async def get_completed_tasks_since(
    user_id: int,
    since_iso: str,
) -> list[tuple[int, str, Optional[str], Optional[str]]]:
    """Returns tasks completed since the specified time (ISO)."""
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, text, due_at, completed_at
            FROM tasks
            WHERE user_id = $1 
              AND status = 'done'
              AND completed_at >= $2
            ORDER BY completed_at DESC
            """,
            user_id, since_iso,
        )
        return [(row["id"], row["text"], row["due_at"], row["completed_at"]) for row in rows]


async def clear_archived_tasks(user_id: int) -> None:
    """Clears user's completed task archive and writes snapshots to tasks_history."""
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, text, created_at, due_at, status, completed_at, category
            FROM tasks
            WHERE user_id = $1 AND status = 'done'
            """,
            user_id,
        )

        if rows:
            deleted_at_iso = now_local_iso()
            for row in rows:
                snapshot = {
                    "task_id": row["id"],
                    "user_id": row["user_id"],
                    "text": row["text"],
                    "created_at": row["created_at"],
                    "due_at": row["due_at"],
                    "status": row["status"],
                    "completed_at": row["completed_at"],
                    "category": row["category"],
                    "source": None,
                }
                await _archive_task_snapshot(
                    conn,
                    snapshot,
                    reason="cleared_archive",
                    deleted_at=deleted_at_iso,
                )

        await conn.execute(
            "DELETE FROM tasks WHERE user_id = $1 AND status = 'done'",
            user_id,
        )


async def log_event(
    user_id: int,
    event_type: str,
    task_id: Optional[int] = None,
    meta: Optional[dict] = None,
):
    """Logs an event."""
    meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO events (user_id, event_type, task_id, meta)
            VALUES ($1, $2, $3, $4)
            """,
            user_id, event_type, task_id, meta_json,
        )


async def get_users_with_active_tasks() -> list[int]:
    """Returns list of user_ids with active tasks."""
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT user_id
            FROM tasks
            WHERE status IS NULL OR status = 'active'
            """
        )
        return [row["user_id"] for row in rows]


async def get_active_tasks_with_future_due(now_iso: str):
    """
    Returns active tasks with deadline in the future.
    Used for restoring reminders after restart.
    """
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, text, due_at
            FROM tasks
            WHERE (status IS NULL OR status = 'active')
              AND due_at IS NOT NULL
              AND due_at > $1
            ORDER BY due_at ASC
            """,
            now_iso,
        )
        return [(row["id"], row["user_id"], row["text"], row["due_at"]) for row in rows]


async def get_active_tasks_with_future_remind(now_iso: str):
    """
    Returns active tasks with upcoming reminder.
    Format: (id, user_id, text, due_at, remind_at, remind_offset_min)
    """
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, text, due_at, remind_at, remind_offset_min
            FROM tasks
            WHERE (status IS NULL OR status = 'active')
              AND remind_at IS NOT NULL
              AND remind_at > $1
            ORDER BY remind_at ASC
            """,
            now_iso,
        )
        return [
            (row["id"], row["user_id"], row["text"], row["due_at"], row["remind_at"], row["remind_offset_min"])
            for row in rows
        ]


async def get_active_tasks_with_future_due_without_remind(now_iso: str):
    """
    Fallback for old/transitional tasks: deadline in future, but remind_at not set.
    Format: (id, user_id, text, due_at)
    """
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, text, due_at
            FROM tasks
            WHERE (status IS NULL OR status = 'active')
              AND due_at IS NOT NULL
              AND due_at > $1
              AND remind_at IS NULL
            ORDER BY due_at ASC
            """,
            now_iso,
        )
        return [(row["id"], row["user_id"], row["text"], row["due_at"]) for row in rows]

# src/db.py

import sqlite3
import json
import os
from typing import List, Tuple, Optional
from datetime import datetime

from time_utils import normalize_deadline_iso, now_local_iso


DB_PATH = os.getenv("DB_PATH", "tasks.db")

def get_connection():
    """Создает подключение к базе данных."""
    return sqlite3.connect(DB_PATH)


def init_db():
    """
    Создаёт таблицы tasks и events, если они не существуют.
    Проводит миграции (добавляет колонки), если база старая.
    """
    with get_connection() as conn:
        cursor = conn.cursor()

        
        cursor.execute(
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

      
        cursor.execute(
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

        cursor.execute(
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
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]

        if "due_at" not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN due_at TEXT")
        if "status" not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN status TEXT DEFAULT 'active'")
        if "completed_at" not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN completed_at TEXT")
        if "category" not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN category TEXT")
        if "remind_at" not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN remind_at TEXT")
        if "remind_offset_min" not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN remind_offset_min INTEGER")

        # --- Нормализация TZ в уже сохранённых данных (фиксируем +05:00, убираем +06:00) ---
        try:
            cursor.execute("SELECT id, due_at FROM tasks WHERE due_at IS NOT NULL")
            rows = cursor.fetchall()
            for task_id, due_at in rows:
                norm = normalize_deadline_iso(due_at)
                if norm and norm != due_at:
                    cursor.execute("UPDATE tasks SET due_at = ? WHERE id = ?", (norm, task_id))
        except Exception:
            # миграция best-effort
            pass

        try:
            cursor.execute("SELECT id, due_at FROM tasks_history WHERE due_at IS NOT NULL")
            rows = cursor.fetchall()
            for hid, due_at in rows:
                norm = normalize_deadline_iso(due_at)
                if norm and norm != due_at:
                    cursor.execute("UPDATE tasks_history SET due_at = ? WHERE id = ?", (norm, hid))
        except Exception:
            pass

        conn.commit()


def add_task(user_id: int, text: str, due_at_iso: Optional[str] = None, category: Optional[str] = None) -> int:
    """Добавляет задачу и возвращает её ID."""
    due_at_iso = normalize_deadline_iso(due_at_iso)
    remind_at_iso = due_at_iso if due_at_iso else None
    remind_offset_min = 0 if due_at_iso else None
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO tasks (user_id, text, due_at, remind_at, remind_offset_min, category) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, text, due_at_iso, remind_at_iso, remind_offset_min, category),
        )
        return cursor.lastrowid


def get_tasks(user_id: int) -> List[Tuple[int, str, Optional[str]]]:
    """
    Возвращает список активных задач: (id, text, due_at).
    Сортировка: сначала с дедлайнами (по возрастанию), потом остальные.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
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
        )
        return cursor.fetchall()


def get_task(user_id: int, task_id: int) -> Optional[Tuple[int, str, Optional[str]]]:
    """Возвращает одну задачу (id, text, due_at) или None."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, text, due_at FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )
        return cursor.fetchone()


def _fetch_task_row(user_id: int, task_id: int):
    """Возвращает полную строку задачи или None."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, user_id, text, created_at, due_at, remind_at, remind_offset_min, status, completed_at, category
            FROM tasks
            WHERE id = ? AND user_id = ?
            """,
            (task_id, user_id),
        )
        row = cursor.fetchone()
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


def get_task_reminder_settings(user_id: int, task_id: int) -> tuple[Optional[str], Optional[int], Optional[str], Optional[str]]:
    """
    Возвращает (remind_at, remind_offset_min, due_at, text) для задачи или (None, None, None, None), если не найдена.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT remind_at, remind_offset_min, due_at, text FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )
        row = cursor.fetchone()
        if not row:
            return None, None, None, None
        remind_at, remind_offset_min, due_at, text = row
        return remind_at, remind_offset_min, due_at, text


def update_task_reminder_settings(
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
    remind_at_iso = normalize_deadline_iso(remind_at_iso)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tasks SET remind_at = ?, remind_offset_min = ? WHERE id = ? AND user_id = ?",
            (remind_at_iso, remind_offset_min, task_id, user_id),
        )
        conn.commit()


def _archive_task_snapshot(task_row: Optional[dict], reason: str, deleted_at: Optional[str] = None):
    """
    Сохраняет копию задачи в аналитический архив.
    Не влияет на пользовательский интерфейс.
    """
    if not task_row:
        return

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
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
        conn.commit()


def update_task_due(user_id: int, task_id: int, due_at_iso: Optional[str]):
    """Обновляет дедлайн задачи."""
    due_at_iso = normalize_deadline_iso(due_at_iso)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tasks SET due_at = ? WHERE id = ? AND user_id = ?",
            (due_at_iso, task_id, user_id),
        )
        conn.commit()


def update_task_text(user_id: int, task_id: int, new_text: str):
    """Обновляет текст задачи."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tasks SET text = ? WHERE id = ? AND user_id = ?",
            (new_text, task_id, user_id),
        )
        conn.commit()


def delete_task(user_id: int, task_id: int):
    """Удаляет задачу (физически)."""
    task_row = _fetch_task_row(user_id, task_id)
    deleted_at_iso = now_local_iso()
    if task_row:
        task_row["deleted_at"] = deleted_at_iso
        _archive_task_snapshot(task_row, reason="deleted", deleted_at=deleted_at_iso)

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )
        conn.commit()


def set_task_done(user_id: int, task_id: int):
    """Помечает задачу выполненной (status='done')."""
    now_iso = now_local_iso()
    task_row = _fetch_task_row(user_id, task_id)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE tasks 
            SET status = 'done', completed_at = ? 
            WHERE id = ? AND user_id = ?
            """,
            (now_iso, task_id, user_id),
        )
        conn.commit()

    if task_row:
        task_row["status"] = "done"
        task_row["completed_at"] = now_iso
        _archive_task_snapshot(task_row, reason="completed")


def get_archived_tasks(user_id: int, limit: int = 10) -> List[Tuple[int, str, Optional[str], Optional[str]]]:
    """Возвращает последние выполненные задачи."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, text, due_at, completed_at 
            FROM tasks
            WHERE user_id = ? AND status = 'done'
            ORDER BY completed_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        return cursor.fetchall()


def clear_archived_tasks(user_id: int) -> None:
    """Очищает архив выполненных задач пользователя."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, user_id, text, created_at, due_at, status, completed_at, category
            FROM tasks
            WHERE user_id = ? AND status = 'done'
            """,
            (user_id,),
        )
        rows = cursor.fetchall()

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
            _archive_task_snapshot(snapshot, reason="cleared_archive", deleted_at=deleted_at_iso)

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM tasks WHERE user_id = ? AND status = 'done'",
            (user_id,),
        )
        conn.commit()


def log_event(user_id: int, event_type: str, task_id: Optional[int] = None, meta: Optional[dict] = None):
    """Пишет лог события."""
    meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO events (user_id, event_type, task_id, meta)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, event_type, task_id, meta_json),
        )
        conn.commit()


def get_users_with_active_tasks() -> list[int]:
    """
    Возвращает список user_id, у которых есть активные задачи.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT user_id
            FROM tasks
            WHERE status IS NULL OR status = 'active'
            """
        )
        rows = cursor.fetchall()
        return [row[0] for row in rows]


def get_active_tasks_with_future_due(now_iso: str):
    """
    Возвращает активные задачи с дедлайном в будущем.
    Используется для восстановления напоминаний после рестарта.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, user_id, text, due_at
            FROM tasks
            WHERE (status IS NULL OR status = 'active')
              AND due_at IS NOT NULL
              AND due_at > ?
            ORDER BY due_at ASC
            """,
            (now_iso,),
        )
        return cursor.fetchall()


def get_active_tasks_with_future_remind(now_iso: str):
    """
    Возвращает активные задачи с ближайшим напоминанием в будущем.
    Формат: (id, user_id, text, due_at, remind_at, remind_offset_min)
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, user_id, text, due_at, remind_at, remind_offset_min
            FROM tasks
            WHERE (status IS NULL OR status = 'active')
              AND remind_at IS NOT NULL
              AND remind_at > ?
            ORDER BY remind_at ASC
            """,
            (now_iso,),
        )
        return cursor.fetchall()


def get_active_tasks_with_future_due_without_remind(now_iso: str):
    """
    Fallback для старых/переходных задач: дедлайн в будущем, но remind_at не задан.
    Формат: (id, user_id, text, due_at)
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
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
        )
        return cursor.fetchall()
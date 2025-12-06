import sqlite3
import json
import os
from typing import List, Tuple, Optional
from datetime import datetime


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

        conn.commit()


def add_task(user_id: int, text: str, due_at_iso: Optional[str] = None, category: Optional[str] = None) -> int:
    """Добавляет задачу и возвращает её ID."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO tasks (user_id, text, due_at, category) VALUES (?, ?, ?, ?)",
            (user_id, text, due_at_iso, category),
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


def update_task_due(user_id: int, task_id: int, due_at_iso: Optional[str]):
    """Обновляет дедлайн задачи."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tasks SET due_at = ? WHERE id = ? AND user_id = ?",
            (due_at_iso, task_id, user_id),
        )
        conn.commit()


def delete_task(user_id: int, task_id: int):
    """Удаляет задачу (физически)."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )
        conn.commit()


def set_task_done(user_id: int, task_id: int):
    """Помечает задачу выполненной (status='done')."""
    now_iso = datetime.now().isoformat()
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

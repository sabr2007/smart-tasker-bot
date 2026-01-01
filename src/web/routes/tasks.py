from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

import db
from time_utils import compute_remind_at_from_offset, normalize_deadline_to_utc
from web.deps import get_current_user


router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskOut(BaseModel):
    id: int
    text: str
    due_at: Optional[str] = None
    is_recurring: bool = False
    recurrence_type: Optional[str] = None
    recurrence_interval: Optional[int] = None


class ArchivedTaskOut(BaseModel):
    id: int
    text: str
    due_at: Optional[str] = None
    completed_at: Optional[str] = None


class TaskCreateIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    deadline_iso: Optional[str] = None


class TaskPatchIn(BaseModel):
    text: Optional[str] = Field(default=None, min_length=1, max_length=4000)
    deadline_iso: Optional[str] = None


def _task_tuple_to_out(row: tuple[int, str, Optional[str]]) -> TaskOut:
    tid, text, due = row
    # due_at is already stored in UTC format in the database
    return TaskOut(id=int(tid), text=text, due_at=due)


def _archived_tuple_to_out(row: tuple[int, str, Optional[str], Optional[str]]) -> ArchivedTaskOut:
    tid, text, due, completed_at = row
    # due_at and completed_at are already stored in UTC format in the database
    return ArchivedTaskOut(
        id=int(tid),
        text=text,
        due_at=due,
        completed_at=completed_at,
    )


@router.get("", response_model=list[TaskOut])
async def list_tasks(user=Depends(get_current_user)):
    user_id = int(user["user_id"])
    rows = await db.get_tasks(user_id)
    return [_task_tuple_to_out(r) for r in rows]


@router.get("/archive", response_model=list[ArchivedTaskOut])
async def list_archived_tasks(user=Depends(get_current_user), limit: int = 50):
    user_id = int(user["user_id"])
    limit = max(1, min(int(limit), 200))
    rows = await db.get_archived_tasks(user_id, limit=limit)
    return [_archived_tuple_to_out(r) for r in rows]


@router.get("/completed", response_model=list[ArchivedTaskOut])
async def list_completed_tasks(
    since: Optional[str] = None, 
    user=Depends(get_current_user)
):
    """
    Возвращает выполненные задачи.
    Если передан since (ISO), возвращает выполненные после этого времени.
    Иначе возвращает последние 10.
    """
    user_id = int(user["user_id"])
    if since:
        rows = await db.get_completed_tasks_since(user_id, since)
    else:
        rows = await db.get_archived_tasks(user_id, limit=10)
    
    return [_archived_tuple_to_out(r) for r in rows]


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(task_id: int, user=Depends(get_current_user)):
    user_id = int(user["user_id"])
    row = await db.get_task(user_id, task_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Задача не найдена")
    return _task_tuple_to_out(row)


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(payload: TaskCreateIn, user=Depends(get_current_user)):
    user_id = int(user["user_id"])
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Пустой текст задачи")

    # Get user timezone for proper conversion
    user_timezone = await db.get_user_timezone(user_id)
    due_norm = normalize_deadline_to_utc(payload.deadline_iso, user_timezone)
    task_id = await db.add_task(user_id, text, due_norm)
    row = await db.get_task(user_id, task_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Не удалось прочитать созданную задачу")
    return _task_tuple_to_out(row)


@router.patch("/{task_id}", response_model=TaskOut)
async def patch_task(task_id: int, payload: TaskPatchIn, user=Depends(get_current_user)):
    """
    Обновляет text и/или deadline_iso.
    - Если deadline_iso присутствует и равен null => дедлайн снимаем.
    - Если поле не передано => не трогаем.

    Важно: при смене дедлайна синхронизируем remind_at/remind_offset_min в БД,
    не дублируя логику бота.
    """
    user_id = int(user["user_id"])

    existing = await db.get_task(user_id, task_id)
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Задача не найдена")

    fields = payload.model_fields_set

    if "text" in fields:
        new_text = (payload.text or "").strip()
        if not new_text:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Пустой текст задачи")
        await db.update_task_text(user_id, task_id, new_text)

    if "deadline_iso" in fields:
        # Get user timezone for proper conversion
        user_timezone = await db.get_user_timezone(user_id)
        new_due = normalize_deadline_to_utc(payload.deadline_iso, user_timezone)
        await db.update_task_due(user_id, task_id, new_due)

        if not new_due:
            # снимаем и дедлайн, и напоминания
            await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=None, remind_offset_min=None)
        else:
            _remind_at, offset_min, _due_at_db, _task_text_db = await db.get_task_reminder_settings(user_id, task_id)
            if offset_min is None:
                new_remind_at = new_due
            else:
                new_remind_at = compute_remind_at_from_offset(new_due, offset_min)
            await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=new_remind_at, remind_offset_min=offset_min)

    row = await db.get_task(user_id, task_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Задача не найдена")
    return _task_tuple_to_out(row)


@router.post("/{task_id}/complete")
async def complete_task(task_id: int, user=Depends(get_current_user)) -> Dict[str, Any]:
    user_id = int(user["user_id"])
    row = await db.get_task(user_id, task_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Задача не найдена")
    
    # Complete task - may create new occurrence if recurring
    success, new_task_id = await db.set_task_done(user_id, task_id)
    
    # Note: WebApp completions won't auto-schedule reminders for new occurrences
    # until bot restarts. This is a known limitation.
    
    return {"ok": True, "new_task_id": new_task_id}


@router.post("/{task_id}/reopen")
async def reopen_task(task_id: int, user=Depends(get_current_user)) -> Dict[str, Any]:
    user_id = int(user["user_id"])
    row = await db.get_task(user_id, task_id)
    # Note: get_task returns task if it exists, regardless of status usually, 
    # but we should check if it's there.
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Задача не найдена")
    await db.set_task_active(user_id, task_id)
    return {"ok": True}


@router.post("/{task_id}/archive")
async def archive_task(task_id: int, user=Depends(get_current_user)) -> Dict[str, Any]:
    user_id = int(user["user_id"])
    row = await db.get_task(user_id, task_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Задача не найдена")
    await db.set_task_archived(user_id, task_id)
    return {"ok": True}

@router.delete("/archive")
async def clear_archive(user=Depends(get_current_user)) -> Dict[str, Any]:
    """Очищает архив выполненных задач пользователя."""
    user_id = int(user["user_id"])
    await db.clear_archived_tasks(user_id)
    return {"ok": True}


@router.delete("/{task_id}")
async def delete_task(task_id: int, user=Depends(get_current_user)) -> Dict[str, Any]:
    user_id = int(user["user_id"])
    row = await db.get_task(user_id, task_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Задача не найдена")
    await db.delete_task(user_id, task_id)
    return {"ok": True}

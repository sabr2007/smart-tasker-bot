# src/bot/jobs.py
"""Scheduled job functions for the Telegram bot.

Contains: reminders, daily digest, and job restoration logic.
"""

import logging
from datetime import datetime, timedelta, timezone

from telegram.ext import ContextTypes

import db
from bot.keyboards import snooze_keyboard
from time_utils import now_utc, UTC

logger = logging.getLogger(__name__)


def _parse_utc_deadline(deadline_iso: str | None) -> datetime | None:
    """Parse ISO deadline string to UTC datetime.
    
    Handles both 'Z' suffix and '+00:00' offset for UTC.
    """
    if not deadline_iso:
        return None
    s = deadline_iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


async def send_task_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Job-функция: отправляет напоминание по задаче.
    Ожидает в job.data: {"task_id": int, "text": str}
    """
    job = context.job
    if not job:
        return

    data = job.data or {}
    task_id = data.get("task_id")
    text = data.get("text") or "задача"
    chat_id = job.chat_id
    try:
        tid = int(task_id)
    except Exception:
        tid = 0

    # Ensure task is still active and remind_at matches (avoid race conditions with WebApp)
    if tid > 0:
        async with db.get_connection() as conn:
            task_row = await db._fetch_task_row(conn, chat_id, tid)
        
        if not task_row or task_row.get("status") != "active":
            logger.info(f"Skipping reminder for task {tid}: task is not active or deleted.")
            return
        
        # Check if task was rescheduled via WebApp (remind_at changed)
        scheduled_remind_at = data.get("scheduled_remind_at")
        current_remind_at = task_row.get("remind_at")
        if scheduled_remind_at and current_remind_at and scheduled_remind_at != current_remind_at:
            logger.info(f"Skipping reminder for task {tid}: remind_at changed from {scheduled_remind_at} to {current_remind_at}")
            return

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "⏰ Напоминание:\n\n"
            f"{text}\n\n"
            "Если хочешь задачу отложить — нажми на кнопку или отправь точное время текстом "
            "(например, «через 30 минут» или «в 18:10»)."
        ),
        reply_markup=snooze_keyboard(tid) if tid > 0 else None,
    )


async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ежедневный дайджест: в 07:30 отправляет всем список активных задач.
    """
    # Import here to avoid circular import
    from bot.services import send_tasks_list
    
    user_ids = await db.get_users_with_active_tasks()
    if not user_ids:
        return

    for uid in user_ids:
        await send_tasks_list(chat_id=uid, user_id=uid, context=context)


def schedule_task_reminder(
    job_queue,
    task_id: int,
    task_text: str,
    deadline_iso: str | None,
    chat_id: int,
    *,
    remind_at_iso: str | None = None,
):
    """
    Ставит напоминание в job_queue, если дедлайн в будущем и данные валидны.
    Используется как при создании/переносе задач, так и при восстановлении после рестарта.
    
    Note: deadline_iso and remind_at_iso are expected to be in UTC.
    """
    when_iso = remind_at_iso or deadline_iso
    if not job_queue or not when_iso:
        return

    dt = _parse_utc_deadline(when_iso)
    if not dt:
        return

    now = now_utc()
    if dt <= now:
        return

    delay = (dt - now).total_seconds()
    job_queue.run_once(
        send_task_reminder,
        when=timedelta(seconds=delay),
        chat_id=chat_id,
        name=f"reminder:{task_id}",
        data={
            "task_id": task_id,
            "text": task_text,
            "scheduled_remind_at": when_iso,  # For versioning - compare with DB before sending
        },
    )


def cancel_task_reminder(task_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Удаляет job напоминания по id задачи.
    Имя job-а: f"reminder:{task_id}".
    """
    if not context.job_queue:
        return

    jobs = context.job_queue.get_jobs_by_name(f"reminder:{task_id}")
    for job in jobs:
        job.schedule_removal()


def cancel_task_reminder_by_id(task_id: int, job_queue) -> None:
    """Cancel reminder job by task ID. Used by agent tools."""
    if not job_queue:
        return
    jobs = job_queue.get_jobs_by_name(f"reminder:{task_id}")
    for job in jobs:
        job.schedule_removal()


async def restore_reminders(job_queue):
    """
    После рестарта бота восстанавливает напоминания по активным задачам с будущими дедлайнами.
    
    Note: Deadlines are stored in UTC, so we compare with UTC now.
    """
    if not job_queue:
        return

    # Use UTC ISO for comparison (with Z suffix)
    now_iso = now_utc().isoformat().replace("+00:00", "Z")
    tasks = await db.get_active_tasks_with_future_remind(now_iso)
    for task_id, user_id, text, due_at, remind_at, _offset_min in tasks:
        schedule_task_reminder(
            job_queue,
            task_id,
            text,
            deadline_iso=due_at,
            chat_id=user_id,
            remind_at_iso=remind_at,
        )

    # fallback: дедлайн в будущем, но remind_at ещё не задан
    fallback = await db.get_active_tasks_with_future_due_without_remind(now_iso)
    for task_id, user_id, text, due_at in fallback:
        schedule_task_reminder(job_queue, task_id, text, deadline_iso=due_at, chat_id=user_id)


async def restore_reminders_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запускается один раз при старте, чтобы восстановить напоминания из БД."""
    if not context.job_queue:
        return
    await restore_reminders(context.job_queue)


async def sync_reminders_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Periodic job (every 5 min): syncs reminders from DB to job_queue.
    Adds missing jobs for tasks with future remind_at (e.g., after WebApp changes).
    """
    job_queue = context.job_queue
    if not job_queue:
        return
    
    now_iso = now_utc().isoformat().replace("+00:00", "Z")
    tasks = await db.get_active_tasks_with_future_remind(now_iso)
    
    for task_id, user_id, text, due_at, remind_at, _ in tasks:
        job_name = f"reminder:{task_id}"
        existing = job_queue.get_jobs_by_name(job_name)
        
        if not existing:
            # No job exists - schedule new one
            schedule_task_reminder(
                job_queue, task_id, text,
                deadline_iso=due_at, chat_id=user_id,
                remind_at_iso=remind_at,
            )
            logger.info(f"Sync: scheduled reminder for task {task_id}")

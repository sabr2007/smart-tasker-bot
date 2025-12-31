# src/bot/handlers/text_pending.py
"""Handlers for pending user state (deadline clarification, reschedule, snooze)."""

import logging
from datetime import timedelta

from telegram import Update
from telegram.ext import ContextTypes

import db
from bot.constants import NO_REMINDER_PHRASES, TASK_VERB_HINTS
from bot.jobs import cancel_task_reminder, schedule_task_reminder
from bot.keyboards import MAIN_KEYBOARD, reminder_compact_keyboard
from bot.utils import format_deadline_human_local, is_deadline_like
from time_utils import (
    compute_remind_at_from_offset,
    normalize_deadline_to_utc,
    now_in_tz,
    parse_datetime_from_text,
    parse_delay_minutes,
    parse_offset_minutes,
)

logger = logging.getLogger(__name__)


def _looks_like_new_task(text: str) -> bool:
    """
    Determines if text looks like a NEW task with embedded date/time,
    rather than just a deadline response like "завтра" or "в 18:00".
    
    Returns True if text contains task-like verbs AND has enough content.
    Example: "Сходить завтра на тренировку в 18:00" -> True (new task)
    Example: "завтра в 18:00" -> False (just a deadline)
    """
    lower = text.lower()
    
    # Check for task-like verbs
    has_task_verb = any(verb in lower for verb in TASK_VERB_HINTS)
    
    # A new task typically has more content than just a time expression
    # "завтра в 18:00" is ~15 chars, a real task is usually longer
    is_long_enough = len(text) > 15
    
    # Count meaningful words (excluding common time words)
    words = lower.split()
    time_words = {"в", "на", "завтра", "сегодня", "через", "час", "часа", "минут", "минуту"}
    meaningful_words = [w for w in words if w not in time_words and len(w) > 2]
    has_enough_words = len(meaningful_words) >= 2
    
    return has_task_verb or (is_long_enough and has_enough_words)


async def handle_pending_deadline(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    text: str,
) -> bool:
    """
    Handles pending deadline clarification for a new task.
    Returns True if handled (caller should return early), False otherwise.
    """
    pending_deadline = context.user_data.get("pending_deadline")
    if not pending_deadline:
        return False

    task_id = pending_deadline.get("task_id")
    task_text = pending_deadline.get("text")

    lower = text.lower()
    if lower in ["нет", "не надо", "без дедлайна", "отмена"]:
        context.user_data.pop("pending_deadline", None)
        await update.message.reply_text(
            f"Ок, задача «{task_text}» без дедлайна.",
            reply_markup=MAIN_KEYBOARD,
        )
        return True

    # Check if this looks like a NEW task with embedded date/time
    # rather than just a deadline response
    if _looks_like_new_task(text):
        # User is adding a new task, not specifying deadline for previous one
        context.user_data.pop("pending_deadline", None)
        return False  # Let the main handler create new task

    # Get user timezone for proper conversion
    user_timezone = await db.get_user_timezone(user_id)
    
    # Try to parse as delay or datetime
    now = now_in_tz(user_timezone)
    delay_min = parse_delay_minutes(text)
    dt = None
    if delay_min is not None:
        dt = now + timedelta(minutes=delay_min)
    else:
        dt = parse_datetime_from_text(text, now=now, base_date=now.date())

    if dt:
        new_due = normalize_deadline_to_utc(dt.isoformat(), user_timezone)
        await db.update_task_due(user_id, task_id, new_due)

        # Set reminder (smart default: 15 min)
        default_offset = 15
        remind_at = compute_remind_at_from_offset(new_due, default_offset)
        await db.update_task_reminder_settings(
            user_id, task_id, remind_at_iso=remind_at, remind_offset_min=default_offset
        )
        schedule_task_reminder(
            context.job_queue,
            task_id=task_id,
            task_text=task_text,
            deadline_iso=new_due,
            chat_id=chat_id,
            remind_at_iso=remind_at,
        )

        context.user_data.pop("pending_deadline", None)
        # Fetch user timezone for display
        user_timezone = await db.get_user_timezone(user_id)
        human = format_deadline_human_local(new_due, user_timezone)
        await update.message.reply_text(
            f"Отлично! Дедлайн для «{task_text}»: {human}.\n🔔 Напоминание: за 15 мин.",
            reply_markup=reminder_compact_keyboard(task_id),
        )
        return True
    else:
        if is_deadline_like(text):
            await update.message.reply_text(
                "Не совсем понял время. Напиши, например: «завтра в 18:00», «через 2 часа» или «нет».",
                reply_markup=MAIN_KEYBOARD,
            )
            return True
        else:
            context.user_data.pop("pending_deadline", None)
            return False  # Fallthrough to AI parse


async def handle_pending_reschedule(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    text: str,
) -> bool:
    """
    Handles pending reschedule date clarification.
    Returns True if handled, False otherwise.
    """
    pending_reschedule = context.user_data.get("pending_reschedule")
    if not pending_reschedule:
        return False

    task_id = pending_reschedule.get("task_id")
    task_text = pending_reschedule.get("text")

    lower = text.lower()
    if lower in ["нет", "не надо", "отмена"]:
        context.user_data.pop("pending_reschedule", None)
        await update.message.reply_text("Ок, не переносим.", reply_markup=MAIN_KEYBOARD)
        return True

    # Get user timezone for proper conversion
    user_timezone = await db.get_user_timezone(user_id)
    now = now_in_tz(user_timezone)
    delay_min = parse_delay_minutes(text)
    dt = None
    if delay_min is not None:
        dt = now + timedelta(minutes=delay_min)
    else:
        dt = parse_datetime_from_text(text, now=now, base_date=now.date())

    if dt:
        new_due = normalize_deadline_to_utc(dt.isoformat(), user_timezone)
        cancel_task_reminder(task_id, context)
        await db.update_task_due(user_id, task_id, new_due)

        # Recalculate reminder
        _remind_at, offset_min, _due_prev, _txt = await db.get_task_reminder_settings(user_id, task_id)
        if offset_min is None:
            new_remind_at = new_due
        else:
            new_remind_at = compute_remind_at_from_offset(new_due, offset_min) if new_due else None

        await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=new_remind_at, remind_offset_min=offset_min)
        schedule_task_reminder(
            context.job_queue,
            task_id=task_id,
            task_text=task_text,
            deadline_iso=new_due,
            chat_id=chat_id,
            remind_at_iso=new_remind_at,
        )

        context.user_data.pop("pending_reschedule", None)
        # Fetch user timezone for display
        user_timezone = await db.get_user_timezone(user_id)
        human = format_deadline_human_local(new_due, user_timezone)
        await update.message.reply_text(
            f"Перенёс задачу «{task_text}» на {human}.",
            reply_markup=MAIN_KEYBOARD,
        )
        return True
    else:
        if is_deadline_like(text):
            await update.message.reply_text(
                "Не понял дату. Напиши, например: «завтра», «в пятницу» или «нет».",
                reply_markup=MAIN_KEYBOARD,
            )
            return True
        else:
            context.user_data.pop("pending_reschedule", None)
            return False


async def handle_pending_reminder_choice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    text: str,
) -> bool:
    """
    Handles pending reminder offset choice via text input.
    Returns True if handled, False otherwise.
    """
    pending_choice = context.user_data.get("pending_reminder_choice")
    if not pending_choice:
        return False

    task_id = pending_choice.get("task_id")
    offset = parse_offset_minutes(text)
    
    if offset is not None:
        _remind_at, _offset_old, due_at, task_text_db = await db.get_task_reminder_settings(user_id, task_id)
        if not due_at:
            await update.message.reply_text("У задачи нет дедлайна.", reply_markup=MAIN_KEYBOARD)
            context.user_data.pop("pending_reminder_choice", None)
            return True

        new_remind_at = compute_remind_at_from_offset(due_at, offset)
        if new_remind_at:
            cancel_task_reminder(task_id, context)
            await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=new_remind_at, remind_offset_min=offset)
            schedule_task_reminder(
                context.job_queue,
                task_id=task_id,
                task_text=task_text_db or "задача",
                deadline_iso=due_at,
                chat_id=chat_id,
                remind_at_iso=new_remind_at,
            )
            await update.message.reply_text(
                f"Ок, напомню за {offset} мин.",
                reply_markup=MAIN_KEYBOARD,
            )
            context.user_data.pop("pending_reminder_choice", None)
            return True

        if is_deadline_like(text):
            await update.message.reply_text(
                "Не понял. Выбери кнопку или напиши, например, «за 30 минут» / «за 1 час» / «в 08:30».",
                reply_markup=MAIN_KEYBOARD,
            )
            return True

    context.user_data.pop("pending_reminder_choice", None)
    return False


async def handle_pending_snooze(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    text: str,
) -> bool:
    """
    Handles pending snooze time input.
    Returns True if handled, False otherwise.
    """
    pending_snooze = context.user_data.get("pending_snooze")
    if not pending_snooze:
        return False

    task_id = pending_snooze.get("task_id")
    if not isinstance(task_id, int):
        return False

    lower = text.lower().strip()
    if lower in NO_REMINDER_PHRASES:
        context.user_data.pop("pending_snooze", None)
        await update.message.reply_text("Ок.", reply_markup=MAIN_KEYBOARD)
        return True

    user_timezone = await db.get_user_timezone(user_id)
    now = now_in_tz(user_timezone)
    delay_min = parse_delay_minutes(text)
    if delay_min is None:
        delay_min = parse_offset_minutes(text)

    dt = None
    if delay_min is not None:
        dt = now + timedelta(minutes=max(delay_min, 0))
    else:
        dt = parse_datetime_from_text(text, now=now, base_date=now.date())

    if not dt or dt <= now:
        if is_deadline_like(text):
            await update.message.reply_text(
                "Не понял. Напиши, например, «через 5 минут», «через 30 минут» или «в 18:10».",
                reply_markup=MAIN_KEYBOARD,
            )
            return True
        context.user_data.pop("pending_snooze", None)
        return False
    else:
        # User timezone is already fetched
        _remind_at, offset_min, due_at, task_text_db = await db.get_task_reminder_settings(user_id, task_id)
        remind_iso = normalize_deadline_to_utc(dt.isoformat(), user_timezone)
        cancel_task_reminder(task_id, context)
        await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=remind_iso, remind_offset_min=offset_min)
        schedule_task_reminder(
            context.job_queue,
            task_id=task_id,
            task_text=task_text_db or "задача",
            deadline_iso=due_at,
            chat_id=chat_id,
            remind_at_iso=remind_iso,
        )
        context.user_data.pop("pending_snooze", None)
        await update.message.reply_text(
            f"Ок, отложил напоминание до {dt.strftime('%d.%m %H:%M')}.",
            reply_markup=MAIN_KEYBOARD,
        )
        return True

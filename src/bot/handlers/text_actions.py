# src/bot/handlers/text_actions.py
"""Single action handlers for different task operations."""

import logging
from datetime import timedelta

from telegram import Update
from telegram.ext import ContextTypes

import db
from bot.jobs import cancel_task_reminder, schedule_task_reminder
from bot.keyboards import MAIN_KEYBOARD, reminder_compact_keyboard
from bot.services import send_tasks_list
from bot.utils import (
    filter_tasks_by_date,
    format_deadline_human_local,
    match_task_or_none,
    render_clarification_message,
    safe_render_user_reply,
)
from task_schema import TaskInterpretation
from time_utils import (
    compute_remind_at_from_offset,
    normalize_deadline_iso,
    now_local,
    now_in_tz,
    parse_deadline_iso,
)

logger = logging.getLogger(__name__)


async def handle_rename_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ai_result: TaskInterpretation,
    user_id: int,
    chat_id: int,
    tasks_snapshot: list,
) -> bool:
    """Handle rename action. Returns True if handled."""
    if ai_result.action != "rename":
        return False

    if not ai_result.title:
        await update.message.reply_text(
            "Мне нужно новое название задачи, но модель его не вернула.",
            reply_markup=MAIN_KEYBOARD,
        )
        return True

    target, mr = match_task_or_none(
        tasks_snapshot,
        target_task_hint=ai_result.target_task_hint,
        raw_input=ai_result.raw_input,
        action=ai_result.action,
    )
    if not target:
        await update.message.reply_text(render_clarification_message(mr), reply_markup=MAIN_KEYBOARD)
        return True

    task_id, _task_text = target
    await db.update_task_text(user_id, task_id, ai_result.title)
    await update.message.reply_text(
        f"✏️ Переименовал задачу: <b>{ai_result.title}</b>",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )
    return True


async def handle_create_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ai_result: TaskInterpretation,
    user_id: int,
    chat_id: int,
) -> bool:
    """Handle create action. Returns True if handled."""
    if ai_result.action != "create":
        return False

    task_text = ai_result.title or ai_result.raw_input
    task_id = await db.add_task(
        user_id,
        task_text,
        normalize_deadline_iso(ai_result.deadline_iso),
    )

    if ai_result.deadline_iso:
        due_norm = normalize_deadline_iso(ai_result.deadline_iso)
        # Fetch user timezone for display
        user_timezone = await db.get_user_timezone(user_id)
        human_deadline = format_deadline_human_local(due_norm, user_timezone) or "непонятное время"

        if due_norm:
            default_offset = 15
            remind_at = compute_remind_at_from_offset(due_norm, default_offset)

            await db.update_task_reminder_settings(
                user_id, task_id, remind_at_iso=remind_at, remind_offset_min=default_offset
            )

            schedule_task_reminder(
                context.job_queue,
                task_id=task_id,
                task_text=task_text,
                deadline_iso=due_norm,
                chat_id=chat_id,
                remind_at_iso=remind_at,
            )

        context.user_data["pending_reminder_choice"] = {"task_id": task_id}
        await update.message.reply_text(
            f"Задача «{task_text}» добавлена! Дедлайн: {human_deadline}.\n🔔 Напоминание: за 15 мин.",
            reply_markup=reminder_compact_keyboard(task_id),
        )
        return True

    # No deadline - prepare event and ask for clarification
    event = {
        "type": "task_created",
        "task_text": task_text,
        "deadline_iso": normalize_deadline_iso(ai_result.deadline_iso),
        "prev_deadline_iso": None,
        "num_active_tasks": len(await db.get_tasks(user_id)),
        "language": "ru",
        "extra": {},
    }
    reply_text = safe_render_user_reply(event)
    await update.message.reply_text(reply_text, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)

    # Enable pending deadline mode
    context.user_data["pending_deadline"] = {"task_id": task_id, "text": task_text}
    await update.message.reply_text(
        "🕒 Хочешь указать срок? Можешь ответить так: «завтра», «в понедельник», «завтра в 18:00». "
        "Если дедлайн не нужен — напиши «нет» или «без дедлайна».",
        reply_markup=MAIN_KEYBOARD,
    )
    return True


async def handle_complete_or_delete_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ai_result: TaskInterpretation,
    user_id: int,
    tasks_snapshot: list,
) -> bool:
    """Handle complete or delete action. Returns True if handled."""
    if ai_result.action not in ["complete", "delete"]:
        return False

    target, mr = match_task_or_none(
        tasks_snapshot,
        target_task_hint=ai_result.target_task_hint,
        raw_input=ai_result.raw_input,
        action=ai_result.action,
    )
    if not target:
        await update.message.reply_text(render_clarification_message(mr), reply_markup=MAIN_KEYBOARD)
        return True

    task_id, task_text = target
    cancel_task_reminder(task_id, context)

    if ai_result.action == "complete":
        await db.set_task_done(user_id, task_id)
        event = {
            "type": "task_completed",
            "task_text": task_text,
            "deadline_iso": None,
            "prev_deadline_iso": None,
            "num_active_tasks": len(await db.get_tasks(user_id)),
            "language": "ru",
            "extra": {},
        }
    else:
        await db.delete_task(user_id, task_id)
        event = {
            "type": "task_deleted",
            "task_text": task_text,
            "deadline_iso": None,
            "prev_deadline_iso": None,
            "num_active_tasks": len(await db.get_tasks(user_id)),
            "language": "ru",
            "extra": {},
        }

    reply_text = safe_render_user_reply(event)
    await update.message.reply_text(reply_text, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)
    return True


async def handle_reschedule_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ai_result: TaskInterpretation,
    user_id: int,
    chat_id: int,
    tasks_snapshot: list,
) -> bool:
    """Handle reschedule or add_deadline action. Returns True if handled."""
    if ai_result.action not in ["reschedule", "add_deadline"]:
        return False

    target, mr = match_task_or_none(
        tasks_snapshot,
        target_task_hint=ai_result.target_task_hint,
        raw_input=ai_result.raw_input,
        action=ai_result.action,
    )
    if not target:
        await update.message.reply_text(render_clarification_message(mr), reply_markup=MAIN_KEYBOARD)
        return True

    task_id, task_text = target
    
    if not ai_result.deadline_iso:
        await update.message.reply_text(
            "🤔 Я понял, что надо перенести, но не понял НА КОГДА. Напиши, например: «завтра в 18:00» или «в понедельник». Если передумал — скажи «нет».",
            reply_markup=MAIN_KEYBOARD,
        )
        context.user_data["pending_reschedule"] = {"task_id": task_id, "text": task_text}
        return True

    cancel_task_reminder(task_id, context)

    prev_task = await db.get_task(user_id, task_id)
    prev_deadline = prev_task[2] if prev_task else None

    new_due = normalize_deadline_iso(ai_result.deadline_iso)
    await db.update_task_due(user_id, task_id, new_due)

    _remind_at, offset_min, _due_at_db, task_text_db = await db.get_task_reminder_settings(user_id, task_id)
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

    event = {
        "type": "task_rescheduled",
        "task_text": task_text,
        "deadline_iso": new_due,
        "prev_deadline_iso": prev_deadline,
        "num_active_tasks": len(await db.get_tasks(user_id)),
        "language": "ru",
        "extra": {},
    }
    reply_text = safe_render_user_reply(event)
    await update.message.reply_text(reply_text, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)
    return True


async def handle_clear_deadline_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ai_result: TaskInterpretation,
    user_id: int,
    tasks_snapshot: list,
) -> bool:
    """Handle clear_deadline action. Returns True if handled."""
    if ai_result.action != "clear_deadline":
        return False

    target, mr = match_task_or_none(
        tasks_snapshot,
        target_task_hint=ai_result.target_task_hint,
        raw_input=ai_result.raw_input,
        action=ai_result.action,
    )
    if not target:
        await update.message.reply_text(render_clarification_message(mr), reply_markup=MAIN_KEYBOARD)
        return True

    task_id, task_text = target
    cancel_task_reminder(task_id, context)
    
    prev_task = await db.get_task(user_id, task_id)
    prev_deadline = prev_task[2] if prev_task else None
    
    await db.update_task_due(user_id, task_id, None)
    await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=None, remind_offset_min=None)
    
    event = {
        "type": "task_rescheduled",
        "task_text": task_text,
        "deadline_iso": None,
        "prev_deadline_iso": prev_deadline,
        "num_active_tasks": len(await db.get_tasks(user_id)),
        "language": "ru",
        "extra": {"action": "clear_deadline"},
    }
    reply_text = safe_render_user_reply(event)
    await update.message.reply_text(reply_text, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)
    return True


async def handle_needs_clarification_action(
    update: Update,
    ai_result: TaskInterpretation,
) -> bool:
    """Handle needs_clarification action. Returns True if handled."""
    if ai_result.action != "needs_clarification":
        return False

    await update.message.reply_text(
        "Я не уверен, что именно нужно сделать. Напиши, пожалуйста, полное название задачи целиком, как оно есть в списке.",
        reply_markup=MAIN_KEYBOARD,
    )
    return True


async def handle_show_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ai_result: TaskInterpretation,
    user_id: int,
    chat_id: int,
) -> bool:
    """Handle show_* actions. Returns True if handled."""
    if ai_result.action not in ["show_active", "show_today", "show_tomorrow", "show_date"]:
        return False

    target_date = None
    weekend_mode = False

    user_timezone = await db.get_user_timezone(user_id)
    now_tz = now_in_tz(user_timezone) if user_timezone else now_local()

    if ai_result.action == "show_today":
        target_date = now_tz.date()
    elif ai_result.action == "show_tomorrow":
        target_date = (now_tz + timedelta(days=1)).date()
    elif ai_result.action == "show_date" and ai_result.deadline_iso:
        try:
            dt = parse_deadline_iso(ai_result.deadline_iso)
            target_date = dt.date() if dt else None
        except Exception:
            target_date = None

    if ai_result.action == "show_date" and getattr(ai_result, "note", None) == "weekend":
        weekend_mode = True

    if target_date:
        if weekend_mode:
            today = now_tz.date()
            weekday = today.weekday()
            days_to_sat = (5 - weekday) % 7
            days_to_sun = (6 - weekday) % 7
            sat_date = today + timedelta(days=days_to_sat)
            sun_date = today + timedelta(days=days_to_sun)

            parts = []
            for label, d in [("Суббота", sat_date), ("Воскресенье", sun_date)]:
                tasks_for_day = await filter_tasks_by_date(user_id, d, user_timezone)
                if tasks_for_day:
                    lines = []
                    for i, (tid, txt, due) in enumerate(tasks_for_day, 1):
                        d_str = format_deadline_human_local(due, user_timezone)
                        if d_str:
                            lines.append(f"{i}. {txt} (до {d_str})")
                        else:
                            lines.append(f"{i}. {txt}")
                    parts.append(f"📌 {label}:\n" + "\n".join(lines))

            if parts:
                await update.message.reply_text("\n\n".join(parts), reply_markup=MAIN_KEYBOARD)
            else:
                await update.message.reply_text("На выходных задач нет 🙂", reply_markup=MAIN_KEYBOARD)
        else:
            tasks_for_day = await filter_tasks_by_date(user_id, target_date, user_timezone)
            if tasks_for_day:
                lines = []
                for i, (tid, txt, due) in enumerate(tasks_for_day, 1):
                    d_str = format_deadline_human_local(due, user_timezone)
                    if d_str:
                        lines.append(f"{i}. {txt} (до {d_str})")
                    else:
                        lines.append(f"{i}. {txt}")
                await update.message.reply_text(
                    "📌 Задачи на выбранный день:\n\n" + "\n".join(lines),
                    reply_markup=MAIN_KEYBOARD,
                )
            else:
                await update.message.reply_text("На этот день задач нет 🙂", reply_markup=MAIN_KEYBOARD)
    else:
        await send_tasks_list(chat_id, user_id, context)

    tasks_now = await db.get_tasks(user_id)
    event = {
        "type": "show_tasks" if tasks_now else "no_tasks",
        "task_text": None,
        "deadline_iso": None,
        "prev_deadline_iso": None,
        "num_active_tasks": len(tasks_now),
        "language": "ru",
        "extra": {"mode": ai_result.action},
    }
    reply_text = safe_render_user_reply(event)
    await update.message.reply_text(reply_text, reply_markup=MAIN_KEYBOARD)
    return True


async def handle_unknown_action(
    update: Update,
    ai_result: TaskInterpretation,
    user_id: int,
) -> bool:
    """Handle unknown action. Returns True if handled."""
    if ai_result.action != "unknown":
        return False

    event = {
        "type": "error",
        "task_text": None,
        "deadline_iso": None,
        "prev_deadline_iso": None,
        "num_active_tasks": len(await db.get_tasks(user_id)),
        "language": "ru",
        "extra": {"reason": "unknown_intent"},
    }
    reply_text = safe_render_user_reply(event)
    await update.message.reply_text(reply_text, reply_markup=MAIN_KEYBOARD)
    return True

# src/bot/handlers/text_multi.py
"""Multi-intent batch processing handler."""

import json
import logging

from telegram import Update
from telegram.ext import ContextTypes

import db
from bot.jobs import cancel_task_reminder, schedule_task_reminder
from bot.keyboards import MAIN_KEYBOARD
from bot.utils import (
    format_deadline_human_local,
    match_task_or_none,
    render_clarification_message,
)
from llm_client import parse_user_input_multi
from task_schema import TaskInterpretation
from time_utils import compute_remind_at_from_offset, normalize_deadline_iso

logger = logging.getLogger(__name__)

# Supported actions for multi-intent processing
SUPPORTED_ACTIONS_MULTI = {
    "create",
    "complete",
    "reschedule",
    "add_deadline",
    "clear_deadline",
    "delete",
    "rename",
    "needs_clarification",
    "unknown",
}


async def process_multi_intent(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    multi_results: list[TaskInterpretation],
    user_id: int,
    chat_id: int,
    tasks_snapshot: list,
) -> bool:
    """
    Process multiple intents in a batch.
    Returns True if handled, False if should fallback to single-intent processing.
    """
    if not multi_results:
        return False

    if not all(m.action in SUPPORTED_ACTIONS_MULTI for m in multi_results):
        return False

    # Local working copy of snapshot to track create/rename within a single message
    tasks_snapshot_work = list(tasks_snapshot)
    
    # Fetch user timezone for display formatting
    user_timezone = await db.get_user_timezone(user_id)
    
    created_lines: list[str] = []
    completed_lines: list[str] = []
    rescheduled_lines: list[str] = []
    add_deadline_lines: list[str] = []
    clear_deadline_lines: list[str] = []
    deleted_lines: list[str] = []
    renamed_lines: list[str] = []
    not_found_lines: list[str] = []
    clarification_lines: list[str] = []
    needs_deadline_lines: list[str] = []
    needs_reschedule_deadline_lines: list[str] = []
    pending_deadline_data: dict | None = None
    pending_reschedule_data: dict | None = None

    for item in multi_results:
        if item.action in {"unknown"}:
            continue
        if item.action == "needs_clarification":
            clarification_lines.append("• нужно уточнение по одной из задач — напиши название полностью.")
            continue

        if item.action == "create":
            task_text = item.title or item.raw_input
            norm_due = normalize_deadline_iso(item.deadline_iso)
            task_id = await db.add_task(user_id, task_text, norm_due)
            tasks_snapshot_work.append((task_id, task_text, norm_due))

            if item.deadline_iso:
                schedule_task_reminder(
                    context.job_queue,
                    task_id=task_id,
                    task_text=task_text,
                    deadline_iso=norm_due,
                    chat_id=chat_id,
                )

            human_deadline = format_deadline_human_local(item.deadline_iso, user_timezone)
            if human_deadline:
                created_lines.append(f"• создано: {task_text} (до {human_deadline})")
            else:
                created_lines.append(f"• создано: {task_text}")
                if pending_deadline_data is None:
                    pending_deadline_data = {"task_id": task_id, "text": task_text}
                    needs_deadline_lines.append(
                        f"• для «{task_text}» укажи срок (например, «завтра 18:00» или «нет»)"
                    )

        elif item.action == "complete":
            target, mr = match_task_or_none(
                tasks_snapshot_work,
                target_task_hint=item.target_task_hint,
                raw_input=item.raw_input,
                action=item.action,
            )
            if not target:
                clarification_lines.append(render_clarification_message(mr))
                continue

            task_id, task_text = target
            cancel_task_reminder(task_id, context)
            await db.set_task_done(user_id, task_id)
            completed_lines.append(f"• выполнена: {task_text}")

        elif item.action in {"reschedule", "add_deadline"}:
            target, mr = match_task_or_none(
                tasks_snapshot_work,
                target_task_hint=item.target_task_hint,
                raw_input=item.raw_input,
                action=item.action,
            )
            if not target:
                clarification_lines.append(render_clarification_message(mr))
                continue
            task_id, task_text = target

            if not item.deadline_iso:
                if pending_reschedule_data is None:
                    pending_reschedule_data = {"task_id": task_id, "text": task_text}
                    needs_reschedule_deadline_lines.append(
                        f"• для срока по «{task_text}» укажи дату/время (например, «завтра 18:00» или «нет»)"
                    )
                continue

            cancel_task_reminder(task_id, context)
            new_due = normalize_deadline_iso(item.deadline_iso)
            await db.update_task_due(user_id, task_id, new_due)
            tasks_snapshot_work = [
                (tid, txt, (new_due if tid == task_id else due))
                for (tid, txt, due) in tasks_snapshot_work
            ]

            _remind_at, offset_min, _due_db, task_text_db = await db.get_task_reminder_settings(user_id, task_id)
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
            human_deadline = format_deadline_human_local(item.deadline_iso, user_timezone)
            if item.action == "add_deadline":
                add_deadline_lines.append(
                    f"• добавил дедлайн: {task_text}" + (f" → {human_deadline}" if human_deadline else "")
                )
            else:
                rescheduled_lines.append(
                    f"• перенёс: {task_text}" + (f" → {human_deadline}" if human_deadline else "")
                )

        elif item.action == "clear_deadline":
            target, mr = match_task_or_none(
                tasks_snapshot_work,
                target_task_hint=item.target_task_hint,
                raw_input=item.raw_input,
                action=item.action,
            )
            if not target:
                clarification_lines.append(render_clarification_message(mr))
                continue
            task_id, task_text = target
            cancel_task_reminder(task_id, context)
            await db.update_task_due(user_id, task_id, None)
            await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=None, remind_offset_min=None)
            tasks_snapshot_work = [
                (tid, txt, (None if tid == task_id else due))
                for (tid, txt, due) in tasks_snapshot_work
            ]
            clear_deadline_lines.append(f"• убрал дедлайн: {task_text}")

        elif item.action == "rename":
            target, mr = match_task_or_none(
                tasks_snapshot_work,
                target_task_hint=item.target_task_hint,
                raw_input=item.raw_input,
                action=item.action,
            )
            if not target:
                clarification_lines.append(render_clarification_message(mr))
                continue
            if not item.title:
                clarification_lines.append("• для переименования нужно новое название.")
                continue
            task_id, _task_text = target
            await db.update_task_text(user_id, task_id, item.title)
            tasks_snapshot_work = [
                (tid, (item.title if tid == task_id else txt), due)
                for (tid, txt, due) in tasks_snapshot_work
            ]
            renamed_lines.append(f"• переименовал: {item.title}")

        elif item.action == "delete":
            target, mr = match_task_or_none(
                tasks_snapshot_work,
                target_task_hint=item.target_task_hint,
                raw_input=item.raw_input,
                action=item.action,
            )
            if not target:
                clarification_lines.append(render_clarification_message(mr))
                continue
            task_id, task_text = target
            cancel_task_reminder(task_id, context)
            await db.delete_task(user_id, task_id)
            deleted_lines.append(f"• удалена: {task_text}")

    # Build reply
    parts: list[str] = []
    if created_lines:
        parts.append("Добавил задачи:")
        parts.extend(created_lines)
    if completed_lines:
        if parts:
            parts.append("")
        parts.append("Отметил выполненными:")
        parts.extend(completed_lines)
    if rescheduled_lines:
        if parts:
            parts.append("")
        parts.append("Перенёс дедлайны:")
        parts.extend(rescheduled_lines)
    if add_deadline_lines:
        if parts:
            parts.append("")
        parts.append("Добавил дедлайны:")
        parts.extend(add_deadline_lines)
    if clear_deadline_lines:
        if parts:
            parts.append("")
        parts.append("Убрал дедлайны:")
        parts.extend(clear_deadline_lines)
    if renamed_lines:
        if parts:
            parts.append("")
        parts.append("Переименовал задачи:")
        parts.extend(renamed_lines)
    if deleted_lines:
        if parts:
            parts.append("")
        parts.append("Удалил задачи:")
        parts.extend(deleted_lines)
    if clarification_lines:
        if parts:
            parts.append("")
        parts.append("Нужно уточнение:")
        parts.extend(clarification_lines[:3])
    if needs_deadline_lines:
        if parts:
            parts.append("")
        parts.append("Нужен дедлайн:")
        parts.extend(needs_deadline_lines)
    if needs_reschedule_deadline_lines:
        if parts:
            parts.append("")
        parts.append("Нужна дата для переноса:")
        parts.extend(needs_reschedule_deadline_lines)
    if not_found_lines:
        if parts:
            parts.append("")
        parts.append("Не смог сопоставить:")
        parts.extend(not_found_lines)

    reply_text = "\n".join(parts) if parts else "Ничего не сделал."
    await update.message.reply_text(reply_text, reply_markup=MAIN_KEYBOARD)

    # Enable pending modes if needed
    if pending_deadline_data and "pending_deadline" not in context.user_data:
        context.user_data["pending_deadline"] = pending_deadline_data
    if pending_reschedule_data and "pending_reschedule" not in context.user_data:
        context.user_data["pending_reschedule"] = pending_reschedule_data

    return True


def should_route_multi(text: str) -> bool:
    """Determine if text should be routed to multi-intent parser."""
    lower_for_route = text.lower()
    multi_markers = (";", "\n")
    has_separator = any(m in text for m in multi_markers) or ("," in text and len(text) > 40)
    has_connectors = any(w in lower_for_route for w in (" и ", " потом ", " затем ", " также ", " ещё "))
    return has_separator or has_connectors


def parse_multi_intents(text: str, tasks_snapshot: list, user_id: int, user_timezone: str = "Asia/Almaty") -> list[TaskInterpretation]:
    """Parse text for multiple intents with logging."""
    try:
        multi_results = parse_user_input_multi(text, tasks_snapshot=tasks_snapshot, user_timezone=user_timezone)
        if multi_results:
            logger.info(
                "Multi-parsed %d items for user %s: %s",
                len(multi_results),
                user_id,
                [m.model_dump() for m in multi_results],
            )
        return multi_results
    except Exception as e:
        logger.exception("parse_user_input_multi failed for user %s: %s", user_id, e)
        return []

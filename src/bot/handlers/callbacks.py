# src/bot/handlers/callbacks.py
"""Callback handlers for inline keyboard buttons.

Contains handlers for:
- mark_done_menu / mark_done_select
- clear_archive
- remind_set (setting reminder duration)
- remind_expand (showing full reminder options)
- snooze_prompt / snooze_quick
"""

from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import db
from bot.jobs import cancel_task_reminder, schedule_task_reminder
from bot.keyboards import snooze_choice_keyboard, reminder_choice_keyboard
from bot.services import send_tasks_list
from time_utils import (
    compute_remind_at_from_offset,
    normalize_deadline_to_utc,
    now_in_tz,
)


async def on_mark_done_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Нажали кнопку "Отметить задачу выполненной" под списком задач.
    Показываем список задач как inline-кнопки.
    """
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    tasks = await db.get_tasks(user_id)

    if not tasks:
        await query.edit_message_text("Активных задач нет 🙂")
        return

    keyboard: list[list[InlineKeyboardButton]] = []
    for task_id, text, _ in tasks:
        label = text if len(text) <= 30 else text[:27] + "..."
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"✅ {label}",
                    callback_data=f"done_task:{task_id}",
                )
            ]
        )

    await query.edit_message_reply_markup(
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def on_mark_done_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Пользователь выбрал конкретную задачу для отметки как выполненной.
    """
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    try:
        _, task_id_str = data.split(":", maxsplit=1)
        task_id = int(task_id_str)
    except Exception:
        return

    user_id = query.from_user.id

    # отменяем напоминание
    cancel_task_reminder(task_id, context)

    # найдём текст задачи, чтобы красиво показать
    tasks = await db.get_tasks(user_id)
    task_text = None
    for tid, txt, _ in tasks:
        if tid == task_id:
            task_text = txt
            break

    await db.set_task_done(user_id, task_id)

    if task_text:
        await query.edit_message_text(
            f"👍 Задача «{task_text}» отмечена выполненной.",
        )
    else:
        await query.edit_message_text("👍 Задача отмечена выполненной.")

    # отправим обновлённый список задач + меню
    await send_tasks_list(query.message.chat_id, user_id, context)




async def on_remind_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Inline-выбор "за сколько напомнить" после создания задачи с дедлайном.
    callback_data: remind_set:{task_id}:{5|30|60|0|off}
    """
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    try:
        _, task_id_str, val = data.split(":", maxsplit=2)
        task_id = int(task_id_str)
    except Exception:
        return

    user_id = query.from_user.id
    chat_id = query.message.chat_id if query.message else user_id

    _remind_at, _offset_min, due_at, task_text = await db.get_task_reminder_settings(user_id, task_id)
    if not due_at:
        await query.edit_message_text("У этой задачи нет дедлайна — напоминание не настроить.")
        context.user_data.pop("pending_reminder_choice", None)
        return

    if val == "off":
        cancel_task_reminder(task_id, context)
        await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=None, remind_offset_min=None)
        await query.edit_message_text("Ок, не буду напоминать по этой задаче.")
        context.user_data.pop("pending_reminder_choice", None)
        return

    try:
        offset_min = int(val)
    except Exception:
        return

    new_remind_at = compute_remind_at_from_offset(due_at, offset_min)
    if not new_remind_at:
        await query.edit_message_text("Не смог настроить напоминание. Попробуй выбрать другую опцию.")
        return

    cancel_task_reminder(task_id, context)
    await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=new_remind_at, remind_offset_min=offset_min)
    schedule_task_reminder(
        context.job_queue,
        task_id=task_id,
        task_text=task_text or "задача",
        deadline_iso=due_at,
        chat_id=chat_id,
        remind_at_iso=new_remind_at,
    )

    await query.edit_message_text(f"Ок, напомню за {offset_min} мин.")
    context.user_data.pop("pending_reminder_choice", None)


async def on_remind_expand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Разворачивает меню выбора напоминания."""
    query = update.callback_query
    await query.answer()
    
    # data format: remind_expand:{task_id}
    try:
        parts = query.data.split(":")
        task_id = int(parts[1])
        await query.edit_message_reply_markup(
            reply_markup=reminder_choice_keyboard(task_id)
        )
    except Exception:
        pass


async def on_snooze_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Пользователь нажал "Отложить ⏳" в напоминании.
    """
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    try:
        _, task_id_str = data.split(":", maxsplit=1)
        task_id = int(task_id_str)
    except Exception:
        return

    # Запоминаем контекст для ввода текстом и показываем клавиатуру выбора.
    context.user_data["pending_snooze"] = {"task_id": task_id}
    keyboard = snooze_choice_keyboard(task_id)

    if query.message:
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception:
            await query.message.reply_text(
                "На сколько отложить напоминание?",
                reply_markup=keyboard,
            )


async def on_snooze_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Быстрое отложение из inline-кнопок напоминания.
    callback_data: snooze:{task_id}:{5|30|60}
    """
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    try:
        _, task_id_str, minutes_str = data.split(":", maxsplit=2)
        task_id = int(task_id_str)
        minutes = int(minutes_str)
    except Exception:
        return

    user_id = query.from_user.id
    chat_id = query.message.chat_id if query.message else user_id

    # Get user timezone for proper conversion
    user_timezone = await db.get_user_timezone(user_id)
    now = now_in_tz(user_timezone)
    dt = now + timedelta(minutes=max(minutes, 0))
    # Import MAIN_KEYBOARD locally if needed or from bot.keyboards
    from bot.keyboards import MAIN_KEYBOARD
    
    remind_iso = normalize_deadline_to_utc(dt.isoformat(), user_timezone)

    _remind_at, offset_min, due_at, task_text = await db.get_task_reminder_settings(user_id, task_id)
    cancel_task_reminder(task_id, context)
    await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=remind_iso, remind_offset_min=offset_min)
    schedule_task_reminder(
        context.job_queue,
        task_id=task_id,
        task_text=task_text or "задача",
        deadline_iso=due_at,
        chat_id=chat_id,
        remind_at_iso=remind_iso,
    )

    if query.message:
        await query.message.reply_text(f"Ок, отложил на {minutes} мин.", reply_markup=MAIN_KEYBOARD)

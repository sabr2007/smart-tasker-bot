# src/bot/services.py
"""Service functions that interact with bot context.

These are not pure utils (they send messages) but also not handlers.
Used by both handlers and jobs.
"""

import logging
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import db
from bot.keyboards import MAIN_KEYBOARD
from time_utils import now_local, now_utc, now_in_tz, parse_deadline_iso, format_deadline_in_tz, normalize_deadline_to_utc


async def send_tasks_list(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """
    Отправляет список активных задач + inline-кнопку
    «Отметить задачу выполненной» + возвращает нижнее меню.
    """
    tasks = await db.get_tasks(user_id)
    # Fetch user timezone for correct display
    user_timezone = await db.get_user_timezone(user_id)
    now = now_in_tz(user_timezone) if user_timezone else now_local()

    if not tasks:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Спи, отдыхай! Задач нет. 🏝",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    with_due: list[str] = []
    without_due: list[str] = []

    for tid, txt, due, is_recurring in tasks:
        if due:
            try:
                # Format using user's timezone
                d_str = format_deadline_in_tz(due, user_timezone) or due
                
                # Check for overdue using UTC comparison
                overdue = False
                utc_s = normalize_deadline_to_utc(due, user_timezone)
                if utc_s:
                     s = utc_s.replace("Z", "+00:00")
                     dt_utc = datetime.fromisoformat(s)
                     overdue = dt_utc < now_utc()

                suffix = f"(до {d_str}" + (", просрочено🚨)" if overdue else ")")
                with_due.append(f"{len(with_due) + 1}. {txt} {suffix}")
            except Exception:
                with_due.append(f"{len(with_due) + 1}. {txt}")
        else:
            without_due.append(f"{len(without_due) + 1}. {txt}")

    parts: list[str] = ["📋 <b>Твои задачи:</b>"]

    if with_due:
        parts.append("")
        parts.append("Задачи с дедлайном:")
        parts.extend(with_due)

    if with_due and without_due:
        parts.append("")
        parts.append("---")

    if without_due:
        parts.append("")
        parts.append("Задачи без дедлайна:")
        parts.extend(without_due)

    text = "\n".join(parts)

    # 1) сообщение со списком + inline-кнопка
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Отметить задачу выполненной",
                        callback_data="mark_done_menu",
                    )
                ]
            ]
        ),
    )



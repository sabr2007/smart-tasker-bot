# src/bot/handlers/text.py
"""Main text message handler - thin orchestrator.

Delegates to specialized modules:
- text_pending: pending state handlers (deadline, reschedule, snooze)
- text_multi: multi-intent batch processing
- text_actions: single action handlers (create, complete, delete, etc.)
"""

import json
import logging
from datetime import timedelta
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

import db
from bot.constants import MASS_CLEAR_HINTS
from bot.keyboards import MAIN_KEYBOARD
from bot.services import send_tasks_list
from bot.utils import filter_tasks_by_date, parse_explicit_date, format_deadline_human_local

# Import pending handlers
from bot.handlers.text_pending import (
    handle_pending_deadline,
    handle_pending_reschedule,
    handle_pending_reminder_choice,
    handle_pending_snooze,
)

# Import multi-intent processing
from bot.handlers.text_multi import (
    process_multi_intent,
    should_route_multi,
    parse_multi_intents,
    SUPPORTED_ACTIONS_MULTI,
)

# Import single action handlers
from bot.handlers.text_actions import (
    handle_rename_action,
    handle_create_action,
    handle_complete_or_delete_action,
    handle_reschedule_action,
    handle_clear_deadline_action,
    handle_needs_clarification_action,
    handle_show_action,
    handle_unknown_action,
)

from llm_client import parse_user_input
from task_schema import TaskInterpretation
from time_utils import now_local, now_in_tz, parse_deadline_iso
from bot.rate_limiter import check_rate_limit

logger = logging.getLogger(__name__)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main entry point for text message handling."""
    if not update.message:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    logger.info("Incoming text from user %s (chat %s): %r", user_id, chat_id, text)

    # --- 0. Keyboard button handling ---
    if text == "Мои задачи" or text == "Показать задачи":
        await send_tasks_list(chat_id, user_id, context)
        return
    elif text == "Архив":
        from bot.services import send_archive_list
        await send_archive_list(chat_id, user_id, context)
        return
    elif text == "Инструкция":
        await update.message.reply_text(
            "Инструкция теперь внутри WebApp (кнопка слева снизу) 📱",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # --- 1. Check pending states ---
    if await handle_pending_deadline(update, context, user_id, chat_id, text):
        return
    if await handle_pending_reschedule(update, context, user_id, chat_id, text):
        return
    if await handle_pending_reminder_choice(update, context, user_id, chat_id, text):
        return
    if await handle_pending_snooze(update, context, user_id, chat_id, text):
        return

    # --- 1.5. Rate limit check (before AI calls) ---
    is_allowed, wait_seconds = check_rate_limit(user_id)
    if not is_allowed:
        await update.message.reply_text(
            f"⏳ Слишком много запросов. Подожди {wait_seconds} сек.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # --- 2. AI parsing ---
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Quick heuristic for question-like queries
    lower_text = text.lower()
    if await _handle_question_query(update, user_id, lower_text):
        return

    tasks_snapshot = await db.get_tasks(user_id)
    user_timezone = await db.get_user_timezone(user_id)

    # Determine routing (single vs multi)
    ai_result: Optional[TaskInterpretation] = None
    multi_results: list[TaskInterpretation] = []

    route_multi = should_route_multi(text)
    logger.info(
        "parser_route %s",
        json.dumps({"user_id": user_id, "text": text, "route": "multi" if route_multi else "single"}, ensure_ascii=False),
    )

    if route_multi:
        multi_results = parse_multi_intents(text, tasks_snapshot, user_id, user_timezone)
        if multi_results and await process_multi_intent(update, context, multi_results, user_id, chat_id, tasks_snapshot):
            return
        # If multi returned single result, use it as ai_result
        if len(multi_results) == 1 and multi_results[0].action in SUPPORTED_ACTIONS_MULTI:
            ai_result = multi_results[0]

    # Fallback to single parse if no multi results
    if ai_result is None:
        try:
            ai_result = parse_user_input(text, tasks_snapshot=tasks_snapshot, user_timezone=user_timezone)
        except Exception as e:
            logger.exception("parse_user_input failed for user %s: %s", user_id, e)
            await update.message.reply_text("Простите, не могу обработать", reply_markup=MAIN_KEYBOARD)
            return

    logger.info("Parsed intent for user %s: %s", user_id, ai_result.model_dump())

    # --- 3. Mass clear guard ---
    if ai_result.action in ["complete", "delete"] and any(phrase in lower_text for phrase in MASS_CLEAR_HINTS):
        await update.message.reply_text(
            "Пока я не умею очищать все задачи разом — могу помогать закрывать их по одной 🙂",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # --- 4. Route to action handlers ---
    if await handle_rename_action(update, context, ai_result, user_id, chat_id, tasks_snapshot):
        return
    if await handle_create_action(update, context, ai_result, user_id, chat_id):
        return
    if await handle_complete_or_delete_action(update, context, ai_result, user_id, tasks_snapshot):
        return
    if await handle_reschedule_action(update, context, ai_result, user_id, chat_id, tasks_snapshot):
        return
    if await handle_clear_deadline_action(update, context, ai_result, user_id, tasks_snapshot):
        return
    if await handle_needs_clarification_action(update, ai_result):
        return
    if await handle_show_action(update, context, ai_result, user_id, chat_id):
        return
    if await handle_unknown_action(update, ai_result, user_id):
        return


async def _handle_question_query(update: Update, user_id: int, lower_text: str) -> bool:
    """Handle question-like queries about tasks on specific dates."""
    question_like = any(
        q in lower_text
        for q in ["что у меня", "что по", "есть ли", "что на", "какие задачи", "есть что-то"]
    )
    if not question_like:
        return False

    user_timezone = await db.get_user_timezone(user_id)
    now = now_in_tz(user_timezone) if user_timezone else now_local()
    target_date = None

    if "завтра" in lower_text:
        target_date = (now + timedelta(days=1)).date()
    elif "сегодня" in lower_text or "на сегодня" in lower_text:
        target_date = now.date()
    else:
        # Parse explicit dates like "9 декабря" using user's timezone
        explicit = parse_explicit_date(lower_text, user_timezone)
        if explicit:
            target_date = explicit

    if not target_date:
        return False

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

    return True

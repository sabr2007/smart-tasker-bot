# src/bot/handlers/commands.py
"""Command handlers for the Telegram bot.

Contains: /start, /broadcast (admin only).
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes

import db
from config import ADMIN_USER_ID
from bot.keyboards import MAIN_KEYBOARD

logger = logging.getLogger(__name__)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветствие и инициализация нового пользователя."""
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "друг"
    
    # Clear conversation history for fresh start
    from bot.handlers.agent_text import clear_user_history
    await clear_user_history(user_id)
    
    # Ensure user exists in DB with default timezone
    await db.get_user_settings(user_id)
    
    await update.message.reply_text(
        f"Привет, {user_name}! 👋\n\n"
        "Я — Smart Tasker, твой AI-помощник для управления задачами.\n\n"
        "Просто напиши мне, что нужно сделать,",
        reply_markup=MAIN_KEYBOARD,
    )

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Рассылка сообщения всем пользователям с активными задачами (только для админа)."""
    if ADMIN_USER_ID is None or update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Эта команда только для админа.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /broadcast текст сообщения")
        return

    text = " ".join(context.args)
    user_ids = await db.get_users_with_active_tasks()
    if not user_ids:
        await update.message.reply_text("Нет пользователей с активными задачами.")
        return

    sent = 0
    import asyncio
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
            await asyncio.sleep(0.05)  # 50ms delay to respect Telegram limits
        except Exception as e:
            logger.warning(f"Не удалось отправить broadcast пользователю {uid}: {e}")

    await update.message.reply_text(f"Broadcast отправлен {sent} пользователям.")



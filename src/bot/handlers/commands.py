# src/bot/handlers/commands.py
"""Command handlers for the Telegram bot.

Contains: /broadcast (admin only).
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes

import db
from config import ADMIN_USER_ID
from bot.keyboards import MAIN_KEYBOARD

logger = logging.getLogger(__name__)

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
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception as e:
            logger.warning(f"Не удалось отправить broadcast пользователю {uid}: {e}")

    await update.message.reply_text(f"Broadcast отправлен {sent} пользователям.")



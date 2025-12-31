# src/bot/handlers/commands.py
"""Command handlers for the Telegram bot.

Contains: /start, /dumpdb, /broadcast.
"""

import os
import logging
from telegram import Update
from telegram.ext import ContextTypes

import db
from config import ADMIN_USER_ID
from bot.keyboards import MAIN_KEYBOARD

logger = logging.getLogger(__name__)


async def cmd_dumpdb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет файл базы данных (только для админа)."""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Эта команда только для админа.")
        return

    db_path = db.DB_PATH if hasattr(db, "DB_PATH") else "tasks.db"
    if not os.path.exists(db_path):
        await update.message.reply_text("Файл базы данных не найден.")
        return

    with open(db_path, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=os.path.basename(db_path),
            caption="Дамп базы задач",
        )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Рассылка сообщения всем пользователям с активными задачами (только для админа)."""
    if update.effective_user.id != ADMIN_USER_ID:
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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветственное сообщение."""
    text = (
        "Привет! 👋 Я Smart Tasker, твой личный AI-помощник по делам.\n\n"
        "Я понимаю обычную человеческую речь (и текст, и голосовые). Мне не нужны сложные команды — просто скажи, что нужно сделать, как будто пишешь ассистенту.\n\n"
        "🚀 <b>С чего начать?</b> Прежде чем мы начнем, очень советую заглянуть в Инструкцию (в настройках WebApp). Там я показываю, как добавлять 10 задач за одну минуту и управлять ими в одно касание.\n\n"
        "Жми кнопку ниже! 👇"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)

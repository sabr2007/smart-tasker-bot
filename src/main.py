# src/main.py
"""
Главный входной файл бота.
Отвечает за инициализацию, регистрацию хэндлеров и запуск polling.

Основная логика вынесена в пакет `src/bot/`.
"""

import asyncio
import logging
from datetime import time as dtime

from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import db
from config import TELEGRAM_BOT_TOKEN
from time_utils import LOCAL_TZ

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO  # Включаем уровень INFO, чтобы видеть обычные сообщения
)
# Отключаем шум от библиотек (по желанию)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

# --- Handlers ---
from bot.jobs import send_daily_digest, restore_reminders_job
from bot.handlers.commands import cmd_start, cmd_dumpdb, cmd_broadcast
from bot.handlers.voice import handle_voice_message
from bot.handlers.text import handle_message
from bot.handlers.callbacks import (
    on_mark_done_menu,
    on_mark_done_select,
    on_remind_set,
    on_snooze_prompt,
    on_snooze_quick,
    on_remind_expand,
)


def main():
    """Entry point for the bot."""
    # Важно для Python 3.11+: python-telegram-bot (20.x) внутри run_polling()
    # использует asyncio.get_event_loop(). Если перед этим вызвать asyncio.run(...),
    # то он создаст и закроет loop, оставив в MainThread "no current event loop".
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(db.init_db())

    # Build app with increased timeouts for Railway network issues
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(30.0)  # Default is 5.0
        .read_timeout(30.0)     # Default is 5.0
        .write_timeout(30.0)    # Default is 5.0
        .build()
    )

    # текстовые сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # голосовые сообщения
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))

    # inline-кнопки
    app.add_handler(CallbackQueryHandler(on_mark_done_menu, pattern=r"^mark_done_menu$"))
    app.add_handler(CallbackQueryHandler(on_mark_done_select, pattern=r"^done_task:\d+$"))
    app.add_handler(CallbackQueryHandler(on_remind_set, pattern=r"^remind_set:\d+:(?:off|0|5|30|60)$"))
    app.add_handler(CallbackQueryHandler(on_snooze_prompt, pattern=r"^snooze_prompt:\d+$"))
    app.add_handler(CallbackQueryHandler(on_snooze_quick, pattern=r"^snooze:\d+:(?:5|30|60)$"))
    app.add_handler(CallbackQueryHandler(on_remind_expand, pattern=r"^remind_expand:\d+$"))
 

    # команды админа
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("dumpdb", cmd_dumpdb))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    # --- УТРЕННИЙ ДАЙДЖЕСТ 07:30 ---
    if app.job_queue:
        app.job_queue.run_daily(
            send_daily_digest,
            time=dtime(hour=7, minute=30, tzinfo=LOCAL_TZ),
            name="daily_digest",
        )
        # восстановим напоминания для задач с будущими дедлайнами (после старта event loop)
        app.job_queue.run_once(restore_reminders_job, when=0, name="restore_reminders_init")

    print("AI Smart-Tasker запущен... 🚀")
    # Retry loop for network issues (Railway)
    while True:
        try:
            app.run_polling()
            break # Exit loop if polling stops cleanly (e.g. signal)
        except Exception as e:
            logging.error(f"Network error in polling: {e}")
            logging.info("Retrying in 5 seconds...")
            import time
            time.sleep(5)


if __name__ == "__main__":
    main()
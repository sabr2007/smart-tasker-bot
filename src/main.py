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
from time_utils import get_tz, DEFAULT_TIMEZONE

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO  # Включаем уровень INFO, чтобы видеть обычные сообщения
)
# Отключаем шум от библиотек (по желанию)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

# --- Handlers ---
from bot.jobs import send_daily_digest, restore_reminders_job, sync_reminders_job
from bot.handlers.commands import cmd_broadcast
from bot.handlers.agent_text import handle_agent_message, handle_agent_voice
from bot.handlers.callbacks import (
    on_mark_done_menu,
    on_mark_done_select,
    on_snooze_prompt,
    on_snooze_quick,
)


def main():
    """Entry point for the bot."""
    print("AI Smart-Tasker запущен... 🚀")

    # Retry loop for network issues (Railway)
    while True:
        try:
            # --- DIAGNOSTICS START ---
            try:
                import httpx
                logging.info("Testing connection to api.telegram.org...")
                resp = httpx.get("https://api.telegram.org", timeout=5.0)
                logging.info(f"Connection to Telegram OK: {resp.status_code}")
            except Exception as net_err:
                logging.error(f"Connection to Telegram FAILED: {net_err}")
            # --- DIAGNOSTICS END ---

            # 1. Создаем новый Event Loop для каждой попытки
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # 2. Инициализируем пул соединений PostgreSQL и БД
            loop.run_until_complete(db.init_pool())
            loop.run_until_complete(db.init_db())

            # 3. Строим приложение
            app = (
                ApplicationBuilder()
                .token(TELEGRAM_BOT_TOKEN)
                .connect_timeout(30.0)
                .read_timeout(30.0)
                .write_timeout(30.0)
                .build()
            )

            # Inject cancel reminder callback for agent tools
            from llm_client import set_cancel_reminder_callback, set_schedule_reminder_callback
            from bot.jobs import cancel_task_reminder_by_id, schedule_task_reminder
            set_cancel_reminder_callback(lambda tid: cancel_task_reminder_by_id(tid, app.job_queue))
            set_schedule_reminder_callback(
                lambda tid, text, deadline, uid: schedule_task_reminder(
                    app.job_queue, tid, text, deadline, uid
                )
            )

            # 4. Регистрируем хэндлеры
            # текстовые сообщения (AI Agent)
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_agent_message))

            # голосовые сообщения (AI Agent)
            app.add_handler(MessageHandler(filters.VOICE, handle_agent_voice))

            # inline-кнопки
            app.add_handler(CallbackQueryHandler(on_mark_done_menu, pattern=r"^mark_done_menu$"))
            app.add_handler(CallbackQueryHandler(on_mark_done_select, pattern=r"^done_task:\d+$"))
            app.add_handler(CallbackQueryHandler(on_snooze_prompt, pattern=r"^snooze_prompt:\d+$"))
            app.add_handler(CallbackQueryHandler(on_snooze_quick, pattern=r"^snooze:\d+:(?:5|30|60)$"))

            # команды
            app.add_handler(CommandHandler("broadcast", cmd_broadcast))

            # --- УТРЕННИЙ ДАЙДЖЕСТ 07:30 ---
            if app.job_queue:
                app.job_queue.run_daily(
                    send_daily_digest,
                    time=dtime(hour=7, minute=30, tzinfo=get_tz(DEFAULT_TIMEZONE)),
                    name="daily_digest",
                )
                app.job_queue.run_once(restore_reminders_job, when=0, name="restore_reminders_init")
                # Periodic sync for WebApp changes (every 5 minutes)
                app.job_queue.run_repeating(
                    sync_reminders_job,
                    interval=300,  # 5 minutes
                    first=60,  # First run after 1 minute
                    name="sync_reminders",
                )

            # 5. Запускаем polling
            logging.info("Starting polling...")
            app.run_polling()
            
            # Если вышли штатно
            break

        except Exception as e:
            logging.error(f"Critical error in main loop: {e}")
            logging.info("Restarting bot in 10 seconds...")
            import time
            time.sleep(10)
        finally:
            try:
                # Закрываем пул соединений PostgreSQL
                if 'loop' in locals() and not loop.is_closed():
                    loop.run_until_complete(db.close_pool())
                    loop.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
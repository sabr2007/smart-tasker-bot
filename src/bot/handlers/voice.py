# src/bot/handlers/voice.py
"""Voice message handler.

Transcribes voice messages using OpenAI Whisper (via llm_client)
and passes the text to the main text handler.
"""

import logging
import os
import tempfile

from telegram import Message, Update
from telegram.ext import ContextTypes

from bot.constants import ENABLE_VOICE_AUTO_HANDLE
from bot.keyboards import MAIN_KEYBOARD
from bot.rate_limiter import check_rate_limit
from llm_client import transcribe_audio

logger = logging.getLogger(__name__)


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает голосовое сообщение:
    - скачивает файл
    - отправляет в OpenAI на транскриб
    - подменяет текст сообщения на транскриб
    - передаёт в ту же логику, что и обычный текст (handle_message).
    """
    # Lazy import to avoid circular dependency if text.py imports this (though unlikely)
    # and because text.py might not be fully initialized when this module is imported.
    from bot.handlers.text import handle_message

    if not update.message or not update.message.voice:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    voice = update.message.voice

    # Rate limit check before Whisper API call
    is_allowed, wait_seconds = check_rate_limit(user_id)
    if not is_allowed:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Слишком много запросов. Подожди {wait_seconds} сек.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    temp_path = None
    try:
        file = await context.bot.get_file(voice.file_id)
        
        # Cross-platform temp file path
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, f"voice_{user_id}_{voice.file_unique_id}.ogg")
        
        await file.download_to_drive(temp_path)

        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        await context.bot.send_message(
            chat_id=chat_id,
            text="Секунду, расшифровываю голосовое...",
            reply_markup=MAIN_KEYBOARD,
        )

        # Transcribe
        text = transcribe_audio(temp_path)
        
        if not text or len(text.strip()) < 2:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Не смог нормально разобрать голосовое. Попробуй ещё раз или напиши текстом 🙂",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        logger.info(
            "Whisper transcript for user %s (chat %s): %r", user_id, chat_id, text
        )

        if not ENABLE_VOICE_AUTO_HANDLE:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Я услышал из голосового:\n\n«{text}»\n\nМожешь отправить это текстом или скорректировать 🙂",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        # Создаём новый Update с текстовым сообщением, чтобы пройти обычный pipeline
        msg_dict = update.message.to_dict()
        msg_dict["text"] = text
        if "voice" in msg_dict:
            msg_dict.pop("voice")
            
        new_message = Message.de_json(msg_dict, context.bot)
        new_update = Update(update.update_id, message=new_message)

        await handle_message(new_update, context)

    except Exception as e:
        logger.exception("Error while processing voice message from %s: %s", user_id, e)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Что-то пошло не так с голосовым. Попробуй ещё раз или напиши текстом 🙂",
            reply_markup=MAIN_KEYBOARD,
        )
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

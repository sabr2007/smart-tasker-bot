# src/bot/handlers/agent_text.py
"""
AI Agent text message handler.

This handler uses the AI Agent architecture with OpenAI Function Calling
to process user messages. The agent can autonomously call tools to get
context before executing actions.
"""

import logging
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

import db
from bot.keyboards import MAIN_KEYBOARD
from bot.rate_limiter import check_rate_limit
from llm_client import run_agent_turn

logger = logging.getLogger(__name__)

# Store conversation history per user (in-memory, limited)
# In production, consider using Redis or database
_user_histories: dict[int, list[dict]] = {}
MAX_HISTORY_PER_USER = 20


def _get_user_history(user_id: int) -> list[dict]:
    """Get conversation history for user."""
    return _user_histories.get(user_id, [])


def _update_user_history(user_id: int, history: list[dict]) -> None:
    """Update conversation history for user, keeping only last N messages."""
    if history:
        _user_histories[user_id] = history[-MAX_HISTORY_PER_USER:]
    elif user_id in _user_histories:
        del _user_histories[user_id]


def clear_user_history(user_id: int) -> None:
    """Clear conversation history for user (e.g., on /start command)."""
    if user_id in _user_histories:
        del _user_histories[user_id]


async def handle_agent_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Main entry point for AI Agent text message handling.
    
    This replaces the old Router/Parser architecture with an AI Agent
    that can reason and call tools autonomously.
    """
    if not update.message:
        return
    
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    
    logger.info("Agent: Incoming text from user %s: %r", user_id, text[:100])
    
    # --- 0. Quick keyboard button handling (bypass AI for simple commands) ---
    if text in ("Мои задачи", "Показать задачи"):
        from bot.services import send_tasks_list
        await send_tasks_list(chat_id, user_id, context)
        return
 
    # --- 1. Rate limit check ---
    is_allowed, wait_seconds = check_rate_limit(user_id)
    if not is_allowed:
        await update.message.reply_text(
            f"⏳ Слишком много запросов. Подожди {wait_seconds} сек.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    
    # --- 2. Show typing indicator ---
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    
    # --- 3. Get user settings ---
    user_timezone = await db.get_user_timezone(user_id)
    
    # --- 4. Get conversation history ---
    history = _get_user_history(user_id)
    
    # --- 5. Run AI Agent ---
    try:
        response, updated_history = await run_agent_turn(
            user_text=text,
            user_id=user_id,
            user_timezone=user_timezone,
            history=history,
        )
    except Exception as e:
        logger.exception("Agent error for user %s: %s", user_id, e)
        await update.message.reply_text(
            "😔 Произошла ошибка при обработке. Попробуй ещё раз.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    
    # --- 6. Update conversation history ---
    _update_user_history(user_id, updated_history)
    
    # --- 7. Send response ---
    if response:
        # Limit response length for Telegram
        if len(response) > 4000:
            response = response[:3997] + "..."
        
        await update.message.reply_text(
            response,
            reply_markup=MAIN_KEYBOARD,
            parse_mode=None,  # Let Telegram auto-detect
        )
    else:
        await update.message.reply_text(
            "Готово!",
            reply_markup=MAIN_KEYBOARD,
        )


async def handle_agent_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle voice messages by transcribing and passing to agent.
    """
    if not update.message or not update.message.voice:
        return
    
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    logger.info("Agent: Voice message from user %s", user_id)
    
    # Rate limit check
    is_allowed, wait_seconds = check_rate_limit(user_id)
    if not is_allowed:
        await update.message.reply_text(
            f"⏳ Слишком много запросов. Подожди {wait_seconds} сек.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    
    # Show typing indicator
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    
    # Download voice file
    import tempfile
    import os
    
    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        
        await file.download_to_drive(tmp_path)
        
        # Transcribe
        from llm_client import transcribe_audio
        text = transcribe_audio(tmp_path)
        
        # Clean up
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        
        if not text:
            await update.message.reply_text(
                "Не удалось распознать голосовое сообщение. Попробуй ещё раз.",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        
        logger.info("Agent: Transcribed voice from user %s: %r", user_id, text[:100])
        
        # Echo transcribed text
        await update.message.reply_text(
            f"🎤 *Распознано:* {text}",
            parse_mode="Markdown",
        )
        
        # Process with agent
        user_timezone = await db.get_user_timezone(user_id)
        history = _get_user_history(user_id)
        
        response, updated_history = await run_agent_turn(
            user_text=text,
            user_id=user_id,
            user_timezone=user_timezone,
            history=history,
        )
        
        _update_user_history(user_id, updated_history)
        
        if response:
            if len(response) > 4000:
                response = response[:3997] + "..."
            await update.message.reply_text(
                response,
                reply_markup=MAIN_KEYBOARD,
            )
        
    except Exception as e:
        logger.exception("Voice processing error for user %s: %s", user_id, e)
        await update.message.reply_text(
            "😔 Ошибка при обработке голосового сообщения.",
            reply_markup=MAIN_KEYBOARD,
        )

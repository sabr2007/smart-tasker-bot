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


def _strip_markdown(text: str) -> str:
    """Remove common Markdown formatting that Telegram doesn't render well."""
    import re
    # Remove **bold** and __bold__
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    # Remove *italic* and _italic_ 
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'\1', text)
    text = re.sub(r'(?<!_)_(?!_)(.+?)(?<!_)_(?!_)', r'\1', text)
    return text


# Conversation history is now persisted in PostgreSQL (see db.py)
# In-memory cache for performance (optional, reduces DB calls)
_user_histories_cache: dict[int, list[dict]] = {}


async def _get_user_history(user_id: int) -> list[dict]:
    """Get conversation history for user from database."""
    # Check cache first
    if user_id in _user_histories_cache:
        return _user_histories_cache[user_id]
    
    # Load from database
    history = await db.get_conversation_history(user_id)
    _user_histories_cache[user_id] = history
    return history


async def _update_user_history(user_id: int, history: list[dict]) -> None:
    """Update conversation history for user in database."""
    # Update cache
    _user_histories_cache[user_id] = history if history else []
    
    # Persist to database
    await db.set_conversation_history(user_id, history)


async def clear_user_history(user_id: int) -> None:
    """Clear conversation history for user (e.g., on /start command)."""
    # Clear cache
    if user_id in _user_histories_cache:
        del _user_histories_cache[user_id]
    
    # Clear from database
    await db.clear_conversation_history(user_id)


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
    history = await _get_user_history(user_id)
    
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
    await _update_user_history(user_id, updated_history)
    
    # --- 7. Send response ---
    if response:
        # Strip Markdown formatting that Telegram doesn't render
        response = _strip_markdown(response)
        
        # Limit response length for Telegram
        if len(response) > 4000:
            response = response[:3997] + "..."
        
        await update.message.reply_text(
            response,
            reply_markup=MAIN_KEYBOARD,
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
        text = await transcribe_audio(tmp_path)
        
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
        history = await _get_user_history(user_id)
        
        response, updated_history = await run_agent_turn(
            user_text=text,
            user_id=user_id,
            user_timezone=user_timezone,
            history=history,
        )
        
        await _update_user_history(user_id, updated_history)
        
        if response:
            # Strip Markdown formatting
            response = _strip_markdown(response)
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

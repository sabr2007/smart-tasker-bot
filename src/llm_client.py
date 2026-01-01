# src/llm_client.py
"""
AI Agent implementation using OpenAI Function Calling.

This module implements the ReAct (Reasoning + Action) pattern:
1. Send messages + tools to OpenAI
2. If model responds with text -> return to user
3. If model requests tool_calls -> execute, add result, repeat
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from openai import AsyncOpenAI

import db
from agent_tools import AGENT_TOOLS
from config import OPENAI_API_KEY, OPENAI_MODEL
from time_utils import (
    normalize_deadline_to_utc,
    now_in_tz,
    format_deadline_in_tz,
    utc_to_local,
)


logger = logging.getLogger(__name__)

# Async OpenAI client
async_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Max iterations to prevent infinite loops
MAX_AGENT_ITERATIONS = 10

# Callbacks for reminder management (injected from main.py)
_cancel_reminder_callback: Callable[[int], None] | None = None
_schedule_reminder_callback: Callable[[int, str, str, int], None] | None = None


def set_cancel_reminder_callback(callback: Callable[[int], None]) -> None:
    """Set callback to cancel reminders. Called from main.py on startup."""
    global _cancel_reminder_callback
    _cancel_reminder_callback = callback


def set_schedule_reminder_callback(callback: Callable[[int, str, str, int], None]) -> None:
    """Set callback to schedule reminders. Called from main.py on startup.
    
    Callback signature: (task_id, task_text, deadline_utc_iso, user_id)
    """
    global _schedule_reminder_callback
    _schedule_reminder_callback = callback


def _parse_utc_iso(iso_str: str | None) -> datetime | None:
    """Parse UTC ISO string to datetime. Supports 'Z' suffix."""
    if not iso_str:
        return None
    s = iso_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def build_agent_system_prompt(now_str: str, user_timezone: str) -> str:
    """Build system prompt for the agent."""
    return f"""Ты — AI-помощник для управления задачами в Telegram. Твоё имя — Smart Tasker.

Текущее время пользователя: {now_str}
Часовой пояс: {user_timezone}

## Твои возможности (инструменты):

1. **get_tasks()** — получить список активных задач с их ID
2. **add_task(text, deadline?)** — создать новую задачу
3. **complete_task(task_id)** — отметить задачу выполненной
4. **delete_task(task_id)** — удалить задачу
5. **update_deadline(task_id, action, deadline?)** — изменить дедлайн
6. **rename_task(task_id, new_text)** — переименовать задачу
7. **show_tasks(filter, date?)** — показать задачи с фильтром

## ВАЖНЫЕ ПРАВИЛА:

1. **Перед удалением/завершением/изменением задачи** — ВСЕГДА сначала вызови get_tasks(), чтобы узнать ID нужной задачи. Никогда не угадывай ID!

2. **Дедлайны** передавай в формате ISO 8601 без таймзоны (например: 2025-01-15T10:00:00). Даты интерпретируй относительно текущего времени пользователя.

3. **Если задача не найдена** — вежливо сообщи об этом и предложи список текущих задач.

4. **Отвечай кратко** и по-русски. Используй эмодзи для наглядности.

5. **При создании задачи** — подтверди создание, укажи текст и дедлайн (если есть).

6. **Если пользователь здоровается** или спрашивает что-то не связанное с задачами — ответь дружелюбно, но кратко.

## Примеры:
- "добавь задачу купить молоко завтра в 10" → add_task(text="Купить молоко", deadline="2025-01-02T10:00:00")
- "удали задачу про молоко" → сначала get_tasks(), потом delete_task(найденный_id)
- "что у меня на сегодня?" → show_tasks(filter="today")
"""


# ============================================================
# TOOL EXECUTORS
# ============================================================

async def execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    user_id: int,
    user_timezone: str,
) -> str:
    """
    Execute a tool and return the result as a string.
    
    All database operations are wrapped in try/except to provide
    friendly error messages to the agent.
    """
    try:
        if tool_name == "get_tasks":
            return await _execute_get_tasks(user_id, user_timezone)
        
        elif tool_name == "add_task":
            return await _execute_add_task(
                user_id,
                arguments.get("text", ""),
                arguments.get("deadline"),
                user_timezone,
            )
        
        elif tool_name == "complete_task":
            return await _execute_complete_task(user_id, arguments.get("task_id"))
        
        elif tool_name == "delete_task":
            return await _execute_delete_task(user_id, arguments.get("task_id"))
        
        elif tool_name == "update_deadline":
            return await _execute_update_deadline(
                user_id,
                arguments.get("task_id"),
                arguments.get("action", "reschedule"),
                arguments.get("deadline"),
                user_timezone,
            )
        
        elif tool_name == "rename_task":
            return await _execute_rename_task(
                user_id,
                arguments.get("task_id"),
                arguments.get("new_text", ""),
            )
        
        elif tool_name == "show_tasks":
            return await _execute_show_tasks(
                user_id,
                arguments.get("filter", "all"),
                arguments.get("date"),
                user_timezone,
            )
        
        else:
            return f"Ошибка: неизвестный инструмент '{tool_name}'"
    
    except Exception as e:
        logger.exception("Tool execution error: %s", tool_name)
        return f"Ошибка при выполнении операции: {str(e)}"


async def _execute_get_tasks(user_id: int, user_timezone: str) -> str:
    """Get all active tasks for user."""
    tasks = await db.get_tasks(user_id)
    
    if not tasks:
        return "У пользователя нет активных задач."
    
    lines = []
    for task_id, text, due_at in tasks:
        if due_at:
            due_str = format_deadline_in_tz(due_at, user_timezone) or due_at
            lines.append(f"ID {task_id}: {text} | Дедлайн: {due_str}")
        else:
            lines.append(f"ID {task_id}: {text} | Без дедлайна")
    
    return "Список задач:\n" + "\n".join(lines)


async def _execute_add_task(
    user_id: int,
    text: str,
    deadline: Optional[str],
    user_timezone: str,
) -> str:
    """Create a new task."""
    if not text or not text.strip():
        return "Ошибка: текст задачи не может быть пустым."
    
    # Normalize deadline to UTC
    deadline_utc = None
    if deadline:
        deadline_utc = normalize_deadline_to_utc(deadline, user_timezone)
        if not deadline_utc:
            return f"Ошибка: неверный формат дедлайна '{deadline}'. Используй ISO 8601."
    
    task_id = await db.add_task(user_id, text.strip(), deadline_utc)
    
    # Schedule reminder if deadline is set
    if deadline_utc and _schedule_reminder_callback:
        _schedule_reminder_callback(task_id, text.strip(), deadline_utc, user_id)
    
    if deadline_utc:
        due_str = format_deadline_in_tz(deadline_utc, user_timezone) or deadline
        return f"Задача создана (ID {task_id}): '{text}' с дедлайном {due_str}"
    else:
        return f"Задача создана (ID {task_id}): '{text}'"


async def _execute_complete_task(user_id: int, task_id: Optional[int]) -> str:
    """Mark task as completed."""
    if task_id is None:
        return "Ошибка: не указан ID задачи."
    
    # Check if task exists
    task = await db.get_task(user_id, task_id)
    if not task:
        return f"Ошибка: задача с ID {task_id} не найдена."
    
    await db.set_task_done(user_id, task_id)
    
    # Cancel reminder if callback is set
    if _cancel_reminder_callback:
        _cancel_reminder_callback(task_id)
    
    return f"Задача '{task[1]}' отмечена как выполненная ✓"


async def _execute_delete_task(user_id: int, task_id: Optional[int]) -> str:
    """Delete a task."""
    if task_id is None:
        return "Ошибка: не указан ID задачи."
    
    # Check if task exists
    task = await db.get_task(user_id, task_id)
    if not task:
        return f"Ошибка: задача с ID {task_id} не найдена."
    
    task_text = task[1]
    await db.delete_task(user_id, task_id)
    
    # Cancel reminder if callback is set
    if _cancel_reminder_callback:
        _cancel_reminder_callback(task_id)
    
    return f"Задача '{task_text}' удалена."


async def _execute_update_deadline(
    user_id: int,
    task_id: Optional[int],
    action: str,
    deadline: Optional[str],
    user_timezone: str,
) -> str:
    """Update task deadline."""
    if task_id is None:
        return "Ошибка: не указан ID задачи."
    
    # Check if task exists
    task = await db.get_task(user_id, task_id)
    if not task:
        return f"Ошибка: задача с ID {task_id} не найдена."
    
    task_text = task[1]
    
    if action == "remove":
        # Cancel existing reminder
        if _cancel_reminder_callback:
            _cancel_reminder_callback(task_id)
        await db.update_task_due(user_id, task_id, None)
        await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=None, remind_offset_min=None)
        return f"Дедлайн задачи '{task_text}' убран."
    
    elif action in ("add", "reschedule"):
        if not deadline:
            return f"Ошибка: для действия '{action}' требуется указать дедлайн."
        
        deadline_utc = normalize_deadline_to_utc(deadline, user_timezone)
        if not deadline_utc:
            return f"Ошибка: неверный формат дедлайна '{deadline}'."
        
        # Cancel old reminder and schedule new one
        if _cancel_reminder_callback:
            _cancel_reminder_callback(task_id)
        
        await db.update_task_due(user_id, task_id, deadline_utc)
        # Update remind_at to match new deadline (remind at deadline time)
        await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=deadline_utc, remind_offset_min=0)
        
        # Schedule new reminder
        if _schedule_reminder_callback:
            _schedule_reminder_callback(task_id, task_text, deadline_utc, user_id)
        
        due_str = format_deadline_in_tz(deadline_utc, user_timezone) or deadline
        
        if action == "add":
            return f"Дедлайн '{due_str}' добавлен к задаче '{task_text}'."
        else:
            return f"Задача '{task_text}' перенесена на {due_str}."
    
    else:
        return f"Ошибка: неизвестное действие '{action}'. Используй add/reschedule/remove."


async def _execute_rename_task(
    user_id: int,
    task_id: Optional[int],
    new_text: str,
) -> str:
    """Rename a task."""
    if task_id is None:
        return "Ошибка: не указан ID задачи."
    
    if not new_text or not new_text.strip():
        return "Ошибка: новый текст задачи не может быть пустым."
    
    # Check if task exists
    task = await db.get_task(user_id, task_id)
    if not task:
        return f"Ошибка: задача с ID {task_id} не найдена."
    
    old_text = task[1]
    await db.update_task_text(user_id, task_id, new_text.strip())
    return f"Задача переименована: '{old_text}' → '{new_text.strip()}'"


async def _execute_show_tasks(
    user_id: int,
    filter_type: str,
    date_str: Optional[str],
    user_timezone: str,
) -> str:
    """Show tasks with filter."""
    tasks = await db.get_tasks(user_id)
    
    if not tasks:
        return "У пользователя нет активных задач."
    
    now = now_in_tz(user_timezone)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    
    filtered_tasks = []
    
    for task_id, text, due_at in tasks:
        if filter_type == "all":
            filtered_tasks.append((task_id, text, due_at))
        
        elif filter_type == "today":
            if due_at:
                dt = _parse_utc_iso(due_at)
                if dt:
                    # Convert to user's timezone for comparison
                    local_dt = utc_to_local(dt, user_timezone)
                    if local_dt and local_dt.date() == today:
                        filtered_tasks.append((task_id, text, due_at))
        
        elif filter_type == "tomorrow":
            if due_at:
                dt = _parse_utc_iso(due_at)
                if dt:
                    local_dt = utc_to_local(dt, user_timezone)
                    if local_dt and local_dt.date() == tomorrow:
                        filtered_tasks.append((task_id, text, due_at))
        
        elif filter_type == "date" and date_str:
            try:
                target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if due_at:
                    dt = _parse_utc_iso(due_at)
                    if dt:
                        local_dt = utc_to_local(dt, user_timezone)
                        if local_dt and local_dt.date() == target_date:
                            filtered_tasks.append((task_id, text, due_at))
            except ValueError:
                return f"Ошибка: неверный формат даты '{date_str}'. Используй YYYY-MM-DD."
    
    if not filtered_tasks:
        filter_names = {
            "all": "активных",
            "today": "на сегодня",
            "tomorrow": "на завтра",
            "date": f"на {date_str}",
        }
        return f"Нет задач {filter_names.get(filter_type, '')}."
    
    lines = []
    for task_id, text, due_at in filtered_tasks:
        if due_at:
            due_str = format_deadline_in_tz(due_at, user_timezone) or due_at
            lines.append(f"ID {task_id}: {text} | {due_str}")
        else:
            lines.append(f"ID {task_id}: {text}")
    
    filter_headers = {
        "all": "Все активные задачи",
        "today": "Задачи на сегодня",
        "tomorrow": "Задачи на завтра",
        "date": f"Задачи на {date_str}",
    }
    
    return f"{filter_headers.get(filter_type, 'Задачи')}:\n" + "\n".join(lines)


# ============================================================
# AGENT LOOP
# ============================================================

async def run_agent_turn(
    user_text: str,
    user_id: int,
    user_timezone: str,
    history: Optional[list[dict]] = None,
) -> tuple[str, list[dict]]:
    """
    Run one turn of the AI agent conversation.
    
    Implements the ReAct (Reasoning + Action) loop:
    1. Send messages + tools to OpenAI
    2. If model responds with text -> return to user
    3. If model requests tool_calls -> execute, add result, repeat
    
    Args:
        user_text: User's message
        user_id: Telegram user ID
        user_timezone: User's IANA timezone
        history: Previous conversation history (optional)
    
    Returns:
        Tuple of (agent_response, updated_history)
    """
    # Build system prompt
    now = now_in_tz(user_timezone)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    system_prompt = build_agent_system_prompt(now_str, user_timezone)
    
    # Initialize messages
    messages = [{"role": "system", "content": system_prompt}]
    
    # Add history if provided (limited to last N messages)
    if history:
        messages.extend(history[-10:])  # Keep last 10 messages for context
    
    # Add current user message
    messages.append({"role": "user", "content": user_text})
    
    # ReAct loop
    for iteration in range(MAX_AGENT_ITERATIONS):
        logger.info(
            "Agent iteration %d for user %d: %d messages",
            iteration + 1, user_id, len(messages)
        )
        
        try:
            response = await async_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                tools=AGENT_TOOLS,
                tool_choice="auto",
                temperature=0.3,
            )
        except Exception as e:
            logger.exception("OpenAI API error")
            # Preserve history on error (skip system prompt which is messages[0])
            preserved_history = [
                msg for msg in messages[1:]
                if msg.get("role") in ("user", "assistant") and msg.get("content")
            ]
            return "Произошла ошибка при обработке запроса. Попробуй позже.", preserved_history
        
        message = response.choices[0].message
        
        # Add assistant message to history
        messages.append({
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in (message.tool_calls or [])
            ] if message.tool_calls else None,
        })
        
        if message.tool_calls:
            for tool_call in message.tool_calls:
                tool_name = tool_call.function.name
                
                try:
                    arguments = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError as e:
                    logger.error(
                        "Failed to parse tool arguments for %s: %s",
                        tool_name, tool_call.function.arguments
                    )
                    # Provide error to LLM so it can recover
                    tool_result = f"Ошибка: не удалось распарсить аргументы инструмента. Попробуй вызвать инструмент ещё раз с корректными параметрами."
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_result,
                    })
                    continue
                
                logger.info(
                    "Agent calling tool: %s with args: %s",
                    tool_name, arguments
                )
                
                # Execute tool
                tool_result = await execute_tool(
                    tool_name, arguments, user_id, user_timezone
                )
                
                logger.info("Tool result: %s", tool_result[:200])
                
                # Add tool result to messages
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_result,
                })
            
            # Continue loop to get final response
            continue
        
        # No tool calls - we have the final response
        final_response = message.content or "Готово!"
        
        # Build clean history for future turns (without system prompt)
        updated_history = [
            msg for msg in messages[1:]  # Skip system prompt
            if msg.get("role") in ("user", "assistant") and msg.get("content")
        ]
        
        return final_response, updated_history
    
    # Max iterations reached
    logger.warning("Agent reached max iterations for user %d", user_id)
    return "Не удалось обработать запрос. Попробуй переформулировать.", []


# ============================================================
# LEGACY FUNCTIONS (kept for backward compatibility)
# ============================================================

from openai import OpenAI

# Sync client for legacy functions
_sync_client = OpenAI(api_key=OPENAI_API_KEY)


def transcribe_audio(file_path: str) -> Optional[str]:
    """
    Transcribe audio file (voice message from Telegram) to text.
    Returns transcribed text or None on error.
    """
    try:
        with open(file_path, "rb") as f:
            result = _sync_client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=f,
            )
        text = getattr(result, "text", None)
        if text:
            return text.strip()
        return None
    except Exception:
        logger.exception("Audio transcription error")
        return None

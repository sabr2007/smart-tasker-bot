# src/llm_client.py
"""
AI Agent implementation using OpenAI Function Calling.

This module implements the ReAct (Reasoning + Action) pattern:
1. Send messages + tools to OpenAI
2. If model responds with text -> return to user
3. If model requests tool_calls -> execute, add result, repeat
"""

import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Union

from openai import AsyncOpenAI

import db
from agent_tools import AGENT_TOOLS
from config import OPENAI_API_KEY, OPENAI_MODEL
from time_utils import (
    normalize_deadline_to_utc,
    now_in_tz,
    format_deadline_in_tz,
    utc_to_local,
    parse_utc_iso,
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



def build_agent_system_prompt(now_str: str, user_timezone: str, active_tasks_count: int = 0, today_tasks_count: int = 0) -> str:
    """Build system prompt for the agent."""
    return f"""Ты — Smart Tasker, умный помощник для управления задачами.

Текущее время: {now_str}
Часовой пояс: {user_timezone}
Активных задач: {active_tasks_count}
Задач на сегодня: {today_tasks_count}

## Твой характер

Ты — профессиональный ассистент с человеческим лицом. Ты не просто фиксируешь сухие факты, а понимаешь контекст жизни пользователя. Если задача важная или дедлайн поздний — ты можешь это коротко отметить. Твой тон: поддерживающий, лаконичный, но не механический и не сильно болтливый.

## Инструменты

1. get_tasks() — список активных задач с ID
2. add_task(text, deadline?, url?, phone?) — создать задачу
3. complete_task(task_id) — отметить выполненной
4. delete_task(task_id) — удалить
5. update_deadline(task_id, action, deadline?) — изменить дедлайн
6. rename_task(task_id, new_text) — переименовать
7. show_tasks(filter, date?) — показать с фильтром
8. set_task_recurring(task_id, recurrence_type, interval?, end_date?) — повторение
9. remove_task_recurrence(task_id) — отключить повторение
10. get_attachment(task_id) — отправить файл

## Правила

1. ПЕРЕД любой операцией с существующей задачей — СНАЧАЛА get_tasks() для получения ID. Никогда не угадывай!

2. Различай НОВЫЕ задачи (add_task) и ОБНОВЛЕНИЕ существующих (update_deadline).

3. Дедлайны в ISO 8601 без таймзоны: 2025-01-15T10:00:00

4. Ссылки передавай в url, телефоны в phone — НЕ включай их в text!

5. ПЕРЕД добавлением из фото/PDF — проверь дубликаты через get_tasks().

## Стиль ответов

- Кратко: 1-2 предложения максимум
- Живой контекст: Ты можешь добавить короткий комментарий от себя, если это уместно (например, пожелать удачи на встрече или похвалить за закрытие сложной задачи).
- БЕЗ Markdown (без **, __, и т.д.)
- Эмодзи используй системно:
  ✓ — успех
  ⏰ — напоминание
  📋 — список
  🏝 — нет задач
  ⚡ — просрочено
  🔗 — есть ссылка
  📞 — есть телефон
  📎 — есть файл
  🔁 — повторяющаяся

## Формат ответов

Создание задачи:
"Добавил: [текст] → [дата время] ✓"
Если есть конфликт: "Добавил: [текст] → [дата]. На это время уже есть задача, имей в виду"

Выполнение:
"Готово ✓" или "Готово ✓ Осталось [N] на сегодня"

Удаление:
"Удалил: [текст] ✓"

Список задач (компактный формат):
"📋 Сегодня ([N]):

1. [текст] → [время]
2. [текст] → [время] 🔗
3. [текст] → [время] 📎

Скоро ([N]):
4. [текст] → завтра [время]
5. [текст] → пт

—
Управляй в Панели задач"

Нет задач:
"Задач нет — время выдохнуть и отдохнуть! 🏝"

## Примеры

- "купить молоко завтра в 10" → add_task(text="Купить молоко", deadline="2025-01-02T10:00:00")
- "созвон в 15:00 https://meet.google.com/abc" → add_task(text="Созвон", deadline=..., url="https://meet.google.com/abc")
- "позвонить маме +77001234567" → add_task(text="Позвонить маме", phone="+77001234567")
- "перенеси молоко на завтра" → get_tasks(), update_deadline(id, "reschedule", deadline=...)

## Ограничения

Ты сфокусирован на задачах. На отвлеченные темы отвечай кратко либо вообще не отвечай если запрос пользователя например о рецепте чего то, о погоде, возвращая к делам: "Я рядом. Но давай вернемся к списку: что добавим или изменим?"

## Фото и PDF

Проанализируй содержимое и предложи: "Вижу данные о [событие]. Добавить как задачу? Выглядит важно." Не добавляй  без подтверждения
"""


# ============================================================
# TOOL EXECUTORS
# ============================================================

async def execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    user_id: int,
    user_timezone: str,
    extra_context: Optional[dict] = None,
) -> str:
    """
    Execute a tool and return the result as a string.
    
    All database operations are wrapped in try/except to provide
    friendly error messages to the agent.
    
    Args:
        extra_context: Additional context from handler (source, origin_user_name)
    """
    try:
        if tool_name == "get_tasks":
            return await _execute_get_tasks(user_id, user_timezone)
        
        elif tool_name == "add_task":
            # Get source from extra_context (passed from handler)
            source = (extra_context or {}).get("source", "text")
            origin_from_context = (extra_context or {}).get("origin_user_name")
            # LLM can also extract origin_user_name from message context
            origin_user_name = arguments.get("origin_user_name") or origin_from_context
            
            # Get attachment info from extra_context
            attachment_file_id = (extra_context or {}).get("attachment_file_id")
            attachment_type = (extra_context or {}).get("attachment_type")
            # Default to True for single-file sources (pdf), False for multi-task sources (screenshot)
            send_with_reminder = (extra_context or {}).get("send_attachment_with_reminder", True)
            
            return await _execute_add_task(
                user_id,
                arguments.get("text", ""),
                arguments.get("deadline"),
                user_timezone,
                source=source,
                origin_user_name=origin_user_name,
                attachment_file_id=attachment_file_id,
                attachment_type=attachment_type,
                send_attachment_with_reminder=send_with_reminder,
                link_url=arguments.get("url"),
                phone=arguments.get("phone"),
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
        
        elif tool_name == "set_task_recurring":
            return await _execute_set_task_recurring(
                user_id,
                arguments.get("task_id"),
                arguments.get("recurrence_type"),
                arguments.get("interval"),
                arguments.get("end_date"),
                user_timezone,
            )
        
        elif tool_name == "remove_task_recurrence":
            return await _execute_remove_task_recurrence(
                user_id,
                arguments.get("task_id"),
            )
        
        elif tool_name == "get_attachment":
            return await _execute_get_attachment(
                user_id,
                arguments.get("task_id"),
                extra_context,
            )
        
        else:
            return f"Ошибка: неизвестный инструмент '{tool_name}'"
    
    except Exception as e:
        logger.exception("Tool execution error: %s", tool_name)
        return f"Ошибка при выполнении операции: {str(e)}"


async def _execute_get_tasks(user_id: int, user_timezone: str) -> str:
    """Get all active tasks for user (excludes completed tasks)."""
    tasks = await db.get_tasks(user_id)
    
    # Filter out completed tasks - only show tasks where completed_at is None
    active_tasks = [
        t for t in tasks if t[7] is None  # t[7] is completed_at
    ]
    
    if not active_tasks:
        return "У пользователя нет активных задач."
    
    lines = []
    for task_id, text, due_at, is_recurring, origin_user_name, _attachment, _link, _completed, _phone in active_tasks:
        parts = [f"ID {task_id}: {text}"]
        
        if due_at:
            due_str = format_deadline_in_tz(due_at, user_timezone) or due_at
            parts.append(f"Дедлайн: {due_str}")
        else:
            parts.append("Без дедлайна")
        
        if origin_user_name:
            parts.append(f"от {origin_user_name}")
        
        lines.append(" | ".join(parts))
    
    return "Список задач:\n" + "\n".join(lines)


async def _execute_add_task(
    user_id: int,
    text: str,
    deadline: Optional[str],
    user_timezone: str,
    source: str = "text",
    origin_user_name: Optional[str] = None,
    attachment_file_id: Optional[str] = None,
    attachment_type: Optional[str] = None,
    send_attachment_with_reminder: bool = True,
    link_url: Optional[str] = None,
    phone: Optional[str] = None,
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

    task_id = await db.add_task(
        user_id,
        text.strip(),
        deadline_utc,
        source=source,
        origin_user_name=origin_user_name,
        attachment_file_id=attachment_file_id,
        attachment_type=attachment_type,
        send_attachment_with_reminder=send_attachment_with_reminder,
        link_url=link_url,
        phone=phone,
    )

    # Schedule reminder if deadline is set
    if deadline_utc and _schedule_reminder_callback:
        _schedule_reminder_callback(task_id, text.strip(), deadline_utc, user_id)

    # Build response with new compact format
    due_str = format_deadline_in_tz(deadline_utc, user_timezone) if deadline_utc else None

    # Build icons
    icons = []
    if link_url:
        icons.append("🔗")
    if phone:
        icons.append("📞")
    if attachment_file_id:
        icons.append("📎")
    icons_str = " " + "".join(icons) if icons else ""

    if due_str:
        return f"Добавил: {text.strip()} → {due_str}{icons_str} ✓"
    else:
        return f"Добавил: {text.strip()}{icons_str} ✓"


async def _execute_complete_task(user_id: int, task_id: Optional[int]) -> str:
    """Mark task as completed. If recurring, creates next occurrence."""
    if task_id is None:
        return "Ошибка: не указан ID задачи."
    
    # Check if task exists
    task = await db.get_task(user_id, task_id)
    if not task:
        return f"Ошибка: задача с ID {task_id} не найдена."
    
    # Complete task - returns (success, new_task_id if recurring)
    success, new_task_id = await db.set_task_done(user_id, task_id)
    
    # Cancel reminder for completed task
    if _cancel_reminder_callback:
        _cancel_reminder_callback(task_id)
    
    # Schedule reminder for new occurrence if task was recurring
    if new_task_id and _schedule_reminder_callback:
        new_task = await db.get_task(user_id, new_task_id)
        if new_task:
            _, text, due_at, _ = new_task
            if due_at:
                _schedule_reminder_callback(new_task_id, text, due_at, user_id)
    
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
    """Show tasks with filter (excludes completed tasks)."""
    tasks = await db.get_tasks(user_id)
    
    # Filter out completed tasks - only show tasks where completed_at is None
    active_tasks = [
        t for t in tasks if t[7] is None  # t[7] is completed_at
    ]
    
    if not active_tasks:
        return "У пользователя нет активных задач."
    
    now = now_in_tz(user_timezone)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    
    filtered_tasks = []
    
    for task_id, text, due_at, is_recurring, origin_user_name, _attachment, _link, _completed, _phone in active_tasks:
        if filter_type == "all":
            filtered_tasks.append((task_id, text, due_at, is_recurring, origin_user_name))
        
        elif filter_type == "today":
            if due_at:
                dt = parse_utc_iso(due_at)
                if dt:
                    # Convert to user's timezone for comparison
                    local_dt = utc_to_local(dt, user_timezone)
                    if local_dt and local_dt.date() == today:
                        filtered_tasks.append((task_id, text, due_at, is_recurring, origin_user_name))
        
        elif filter_type == "tomorrow":
            if due_at:
                dt = parse_utc_iso(due_at)
                if dt:
                    local_dt = utc_to_local(dt, user_timezone)
                    if local_dt and local_dt.date() == tomorrow:
                        filtered_tasks.append((task_id, text, due_at, is_recurring, origin_user_name))
        
        elif filter_type == "date" and date_str:
            try:
                target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if due_at:
                    dt = parse_utc_iso(due_at)
                    if dt:
                        local_dt = utc_to_local(dt, user_timezone)
                        if local_dt and local_dt.date() == target_date:
                            filtered_tasks.append((task_id, text, due_at, is_recurring, origin_user_name))
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
    for task_id, text, due_at, is_recurring, origin_user_name in filtered_tasks:
        parts = [f"ID {task_id}: {text}"]
        
        if due_at:
            due_str = format_deadline_in_tz(due_at, user_timezone) or due_at
            parts.append(due_str)
        
        if origin_user_name:
            parts.append(f"от {origin_user_name}")
        
        lines.append(" | ".join(parts))
    
    filter_headers = {
        "all": "Все активные задачи",
        "today": "Задачи на сегодня",
        "tomorrow": "Задачи на завтра",
        "date": f"Задачи на {date_str}",
    }
    
    return f"{filter_headers.get(filter_type, 'Задачи')}:\n" + "\n".join(lines)


async def _execute_set_task_recurring(
    user_id: int,
    task_id: Optional[int],
    recurrence_type: Optional[str],
    interval: Optional[int],
    end_date: Optional[str],
    user_timezone: str,
) -> str:
    """Set a task as recurring."""
    if task_id is None:
        return "Ошибка: не указан ID задачи."
    
    if not recurrence_type:
        return "Ошибка: не указан тип повторения (daily, weekly, monthly, custom)."
    
    valid_types = ["daily", "weekly", "monthly", "custom"]
    if recurrence_type not in valid_types:
        return f"Ошибка: неверный тип повторения '{recurrence_type}'. Используй: {', '.join(valid_types)}."
    
    if recurrence_type == "custom" and (not interval or interval < 1):
        return "Ошибка: для типа 'custom' требуется параметр interval (количество дней, минимум 1)."
    
    # Check if task exists
    task = await db.get_task(user_id, task_id)
    if not task:
        return f"Ошибка: задача с ID {task_id} не найдена."
    
    # Convert end_date to UTC if provided
    end_date_utc = None
    if end_date:
        end_date_utc = normalize_deadline_to_utc(end_date, user_timezone)
    
    # Set recurrence
    success = await db.set_task_recurrence(
        user_id, task_id, recurrence_type, interval, end_date_utc
    )
    
    if not success:
        return f"Ошибка: не удалось установить повторение для задачи с ID {task_id}."
    
    # Build confirmation message
    type_names = {
        "daily": "каждый день",
        "weekly": "каждую неделю",
        "monthly": "каждый месяц",
        "custom": f"каждые {interval} дн.",
    }
    type_str = type_names.get(recurrence_type, recurrence_type)
    
    return f"Задача '{task[1]}' теперь повторяется {type_str} 🔁"


async def _execute_remove_task_recurrence(
    user_id: int,
    task_id: Optional[int],
) -> str:
    """Remove recurrence from a task."""
    if task_id is None:
        return "Ошибка: не указан ID задачи."
    
    # Check if task exists
    task = await db.get_task(user_id, task_id)
    if not task:
        return f"Ошибка: задача с ID {task_id} не найдена."
    
    # Remove recurrence
    success = await db.remove_task_recurrence(user_id, task_id)
    
    if not success:
        return f"Ошибка: не удалось отключить повторение для задачи с ID {task_id}."
    
    return f"Повторение задачи '{task[1]}' отключено ✓"


# Callback for sending attachments (injected from main.py)
_send_attachment_callback: Callable[[int, str, str], None] | None = None


def set_send_attachment_callback(callback: Callable[[int, str, str], None]) -> None:
    """Set callback to send attachments. Called from main.py on startup.
    
    Callback signature: (chat_id, file_id, attachment_type)
    """
    global _send_attachment_callback
    _send_attachment_callback = callback


async def _execute_get_attachment(
    user_id: int,
    task_id: Optional[int],
    extra_context: Optional[dict],
) -> str:
    """Get and send attachment for a task."""
    if task_id is None:
        return "Ошибка: не указан ID задачи."
    
    # Check if task exists
    task = await db.get_task(user_id, task_id)
    if not task:
        return f"Ошибка: задача с ID {task_id} не найдена."
    
    # Get attachment
    file_id, att_type, _ = await db.get_task_attachment(user_id, task_id)
    
    if not file_id:
        return f"К задаче '{task[1]}' не прикреплён файл."
    
    # Send file via callback
    if _send_attachment_callback:
        try:
            await _send_attachment_callback(user_id, file_id, att_type or "document")
            return f"Файл к задаче '{task[1]}' отправлен 📎"
        except Exception as e:
            logger.error("Failed to send attachment: %s", e)
            return "Ошибка при отправке файла."
    else:
        return "Функция отправки файлов не настроена."


# ============================================================
# AGENT LOOP
# ============================================================

async def run_agent_turn(
    user_text: str,
    user_id: int,
    user_timezone: str,
    history: Optional[list[dict]] = None,
    extra_context: Optional[dict] = None,
    image_bytes: Optional[bytes] = None,
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
        extra_context: Handler context (source, origin_user_name)
        image_bytes: Raw image bytes for GPT-4o Vision (optional)
    
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
        # Validate history entries - must be dicts with 'role' and 'content'
        valid_history = [
            msg for msg in history[-10:]
            if isinstance(msg, dict) and "role" in msg and "content" in msg
        ]
        messages.extend(valid_history)
    
    # Build user message content (text or multimodal)
    if image_bytes:
        # Encode image to Base64 for GPT-4o Vision
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        
        # Determine MIME type by magic bytes (imghdr is deprecated in 3.11+)
        def _detect_image_mime(data: bytes) -> str:
            if data[:8] == b'\x89PNG\r\n\x1a\n':
                return 'image/png'
            if data[:2] == b'\xff\xd8':
                return 'image/jpeg'
            if data[:6] in (b'GIF87a', b'GIF89a'):
                return 'image/gif'
            if data[:4] == b'RIFF' and len(data) > 12 and data[8:12] == b'WEBP':
                return 'image/webp'
            return 'image/jpeg'  # fallback
        
        mime_type = _detect_image_mime(image_bytes)
        
        user_content: Union[str, list] = [
            {
                "type": "text", 
                "text": user_text or "Проанализируй изображение и извлеки задачи, даты и время."
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{image_b64}",
                    "detail": "high"  # High detail for reading text
                }
            }
        ]
    else:
        user_content = user_text
    
    # Add current user message
    messages.append({"role": "user", "content": user_content})
    
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
            error_type = type(e).__name__
            logger.error(
                "OpenAI API error for user %d: %s: %s. Messages count: %d",
                user_id, error_type, str(e)[:200], len(messages)
            )
            # Clear history on error to prevent cascading failures
            # User can start fresh with next message
            return f"Произошла ошибка при обработке запроса ({error_type}). Попробуй ещё раз.", []
        
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
                    tool_name, arguments, user_id, user_timezone, extra_context
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
        # IMPORTANT: Only keep user messages and assistant messages WITHOUT tool_calls
        # to avoid "tool_calls must be followed by tool messages" errors
        # Also strip Base64 image data to prevent memory/token bloat
        updated_history = []
        for msg in messages[1:]:  # Skip system prompt
            role = msg.get("role")
            content = msg.get("content")
            tool_calls = msg.get("tool_calls")
            
            if role == "user" and content:
                # Strip Base64 from multimodal messages
                if isinstance(content, list):
                    # Extract only text parts, replace image with placeholder
                    text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                    content = "[Изображение] " + " ".join(text_parts)
                updated_history.append({"role": "user", "content": content})
            elif role == "assistant" and content and not tool_calls:
                # Only keep assistant messages that have content and NO tool_calls
                updated_history.append({"role": "assistant", "content": content})
        
        return final_response, updated_history
    
    # Max iterations reached
    logger.warning("Agent reached max iterations for user %d", user_id)
    return "Не удалось обработать запрос. Попробуй переформулировать.", []


# ============================================================
# LEGACY FUNCTIONS (kept for backward compatibility)
# ============================================================


async def transcribe_audio(file_path: str) -> Optional[str]:
    """
    Transcribe audio file (voice message from Telegram) to text.
    Returns transcribed text or None on error.
    """
    try:
        with open(file_path, "rb") as f:
            result = await async_client.audio.transcriptions.create(
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

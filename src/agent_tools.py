# src/agent_tools.py
"""
OpenAI Function Calling tool definitions for the AI Agent.

Each tool maps to database operations in db.py.
The agent will use these tools to manage tasks.
"""

from typing import Any

# ============================================================
# TOOL DEFINITIONS FOR OPENAI FUNCTION CALLING
# ============================================================

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_tasks",
            "description": (
                "Получить список активных задач пользователя. "
                "Возвращает список задач с их ID, текстом и дедлайном. "
                "ВАЖНО: Вызывай эту функцию ПЕРВОЙ, когда нужно найти задачу для удаления, "
                "завершения, переименования или изменения дедлайна."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_task",
            "description": (
                "Создать новую задачу. "
                "Параметр deadline — опциональный, в формате ISO 8601 (например: 2025-01-15T10:00:00)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Текст задачи (краткое описание, что нужно сделать)",
                    },
                    "deadline": {
                        "type": "string",
                        "description": (
                            "Дедлайн задачи в формате ISO 8601 (без таймзоны). "
                            "Например: 2025-01-15T10:00:00. Если не указан, задача без дедлайна."
                        ),
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": (
                "Отметить задачу как выполненную. "
                "Требуется task_id — используй get_tasks() чтобы узнать ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "ID задачи для завершения",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_task",
            "description": (
                "Удалить задачу. "
                "Требуется task_id — используй get_tasks() чтобы узнать ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "ID задачи для удаления",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_deadline",
            "description": (
                "Изменить дедлайн задачи. "
                "action='add' — добавить дедлайн (если не было). "
                "action='reschedule' — перенести на новую дату. "
                "action='remove' — убрать дедлайн. "
                "Для add/reschedule нужен параметр deadline."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "ID задачи",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["add", "reschedule", "remove"],
                        "description": "Тип операции: add (добавить), reschedule (перенести), remove (убрать)",
                    },
                    "deadline": {
                        "type": "string",
                        "description": (
                            "Новый дедлайн в формате ISO 8601. "
                            "Обязателен для action='add' и 'reschedule'. "
                            "Игнорируется для action='remove'."
                        ),
                    },
                },
                "required": ["task_id", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_task",
            "description": (
                "Переименовать задачу (изменить её текст). "
                "Требуется task_id — используй get_tasks() чтобы узнать ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "ID задачи для переименования",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Новый текст задачи",
                    },
                },
                "required": ["task_id", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_tasks",
            "description": (
                "Показать задачи с фильтром. "
                "filter='all' — все активные задачи. "
                "filter='today' — только на сегодня. "
                "filter='tomorrow' — только на завтра. "
                "filter='date' — на конкретную дату (требуется параметр date)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "enum": ["all", "today", "tomorrow", "date"],
                        "description": "Тип фильтра для отображения задач",
                    },
                    "date": {
                        "type": "string",
                        "description": (
                            "Дата для фильтра 'date' в формате YYYY-MM-DD. "
                            "Игнорируется для других фильтров."
                        ),
                    },
                },
                "required": ["filter"],
            },
        },
    },
]


def get_tool_names() -> list[str]:
    """Return list of available tool names."""
    return [tool["function"]["name"] for tool in AGENT_TOOLS]


def get_tool_by_name(name: str) -> dict | None:
    """Get tool definition by name."""
    for tool in AGENT_TOOLS:
        if tool["function"]["name"] == name:
            return tool
    return None

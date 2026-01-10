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
                    "origin_user_name": {
                        "type": "string",
                        "description": (
                            "Имя пользователя, от которого переслано сообщение. "
                            "Используй только если явно указано в контексте."
                        ),
                    },
                    "url": {
                        "type": "string",
                        "description": (
                            "Ссылка, связанная с задачей (например, ссылка на созвон, Zoom, Meet). "
                            "Используй если пользователь явно указал URL."
                        ),
                    },
                    "phone": {
                        "type": "string",
                        "description": (
                            "Номер телефона, связанный с задачей. "
                            "Используй если пользователь явно указал номер телефона."
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
    {
        "type": "function",
        "function": {
            "name": "set_task_recurring",
            "description": (
                "Сделать задачу повторяющейся (регулярной). "
                "После выполнения задачи автоматически создастся новая с тем же текстом. "
                "recurrence_type: 'daily' (каждый день), 'weekly' (каждую неделю), "
                "'monthly' (каждый месяц), 'custom' (каждые N дней). "
                "Для 'custom' требуется параметр interval (количество дней)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "ID задачи",
                    },
                    "recurrence_type": {
                        "type": "string",
                        "enum": ["daily", "weekly", "monthly", "custom"],
                        "description": "Тип повторения",
                    },
                    "interval": {
                        "type": "integer",
                        "description": "Интервал в днях (только для type=custom)",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Дата окончания повторений в ISO 8601 (опционально)",
                    },
                },
                "required": ["task_id", "recurrence_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_task_recurrence",
            "description": (
                "Отключить повторение задачи. "
                "Задача останется в списке, но больше не будет автоматически "
                "создаваться после выполнения."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "ID задачи",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_attachment",
            "description": (
                "Отправить пользователю прикреплённый файл (PDF, фото) к задаче. "
                "Используй когда пользователь просит отправить билет, документ или файл."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "ID задачи с прикреплённым файлом",
                    },
                },
                "required": ["task_id"],
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

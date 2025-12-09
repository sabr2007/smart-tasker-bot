# src/task_schema.py
from typing import Optional, Literal
from pydantic import BaseModel, Field


ActionType = Literal[
    "create",       # создать новую задачу
    "reschedule",   # перенести/поменять дедлайн
    "complete",     # отметить задачу выполненной
    "delete",       # удалить задачу
    "show_active",  # показать активные задачи
    "show_today",   # показать задачи на сегодня
    "show_tomorrow",# показать задачи на завтра
    "show_date",    # показать задачи на конкретную дату
    "unknown",      # не понял, что хочет пользователь
]


class TaskInterpretation(BaseModel):
    """
    Результат разбора одной пользовательской фразы.
    Это чистый JSON, который будет возвращать модель.
    """

    action: ActionType = Field(
        ...,
        description="Тип операции над задачами: create / reschedule / complete / delete / show_* / unknown",
    )

    title: Optional[str] = Field(
        None,
        description="Краткий текст задачи (если есть). Например: 'сходить в магазин'",
    )

    deadline_iso: Optional[str] = Field(
        None,
        description=(
            "Дата и время дедлайна в ISO 8601 с таймзоной, если есть. "
            "Например: '2025-12-05T19:00:00+06:00'"
        ),
    )

    target_task_hint: Optional[str] = Field(
        None,
        description=(
            "Текст, который помогает понять, к какой существующей задаче относится действие. "
            "Например: 'созвон с Пашей', 'эту задачу', 'вчерашний дедлайн по отчёту'."
        ),
    )

    note: Optional[str] = Field(
        None,
        description="Дополнительный комментарий/контекст, если он был в фразе.",
    )

    language: Optional[str] = Field(
        None,
        description="Язык, на котором говорит пользователь (чаще всего 'ru').",
    )

    raw_input: str = Field(
        ...,
        description="Оригинальный текст пользователя без изменений.",
    )

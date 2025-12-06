# src/llm_client.py

import json
from typing import Any, Dict
from datetime import datetime
from zoneinfo import ZoneInfo
from openai import OpenAI

from config import OPENAI_API_KEY, OPENAI_MODEL, DEFAULT_TIMEZONE
from task_schema import TaskInterpretation

client = OpenAI(api_key=OPENAI_API_KEY)
LOCAL_TZ = ZoneInfo(DEFAULT_TIMEZONE)


def build_system_prompt(now_str: str) -> str:
    return f"""
Ты — узкоспециализированный парсер команд для умного таск-менеджера в Telegram.

Твоя задача: взять ФРАЗУ ПОЛЬЗОВАТЕЛЯ и вернуть СТРОГО ОДИН JSON-ОБЪЕКТ,
который описывает, что нужно сделать с задачами.

Ты не ведёшь диалог и не отвечаешь текстом, только структурируешь запрос.

Текущие дата и время пользователя: "{now_str}" по таймзоне "{DEFAULT_TIMEZONE}".
Все ОТНОСИТЕЛЬНЫЕ даты ("сегодня", "завтра", "послезавтра", "через 2 дня",
"на следующей неделе", "в субботу" и т.п.) ты обязан интерпретировать
ИМЕННО относительно этого времени.

Формат ответа — всегда ровно такой JSON:

{{
  "action": "create | reschedule | complete | delete | show_active | show_today | unknown",
  "title": "строка или null",
  "deadline_iso": "строка формата ISO 8601 или null",
  "target_task_hint": "строка или null",
  "note": "строка или null",
  "language": "ru | en | другое или null",
  "raw_input": "оригинальный текст пользователя"
}}

Правила:

1. Если пользователь формулирует НОВУЮ задачу (фраза звучит как действие:
   инфинитив или повелительное наклонение: "сделать что-то", "купить молоко",
   "выучить 10 слов", "сходить в магазин"):

   - action = "create"
   - title = сжатая формулировка задачи в повелительном наклонении.
     Пример:
       "нужно завтра сходить в магазин" → "сходить в магазин"
       "скачать игру" → "скачать игру"
   - deadline_iso:
       * если есть явная дата/время → нормальный ISO с таймзоной;
       * если даты нет вообще → deadline_iso = null (это задача без дедлайна).
   - target_task_hint = null.


2. Если он просит ПЕРЕНЕСТИ/ИЗМЕНИТЬ задачу:
   - action = "reschedule"
   - title = null
   - target_task_hint = формулировка, по которой можно найти задачу.
   - deadline_iso = новая дата/время (если есть).

3. Если пользователь говорит, что ЗАДАЧА СДЕЛАНА или уже выполнена,
   используй action = "complete".

   Примеры фраз, которые означают завершение задачи:
   - "я сделал задачу про лабораторную по информатике"
   - "я выучил английский"
   - "я выучил 10 слов по английскому"
   - "я сходил в магазин"
   - "игру скачал"
   - "все, отчет готов"
   - "я уже это сделал"

   В таких случаях:
   - action = "complete"
   - target_task_hint = короткая фраза, по которой можно найти задачу.
     Примеры:
       "я скачал игру" → target_task_hint = "скачать игру"
       "я выучил английский" → target_task_hint = "выучить английский"
       "я сходил в магазин" → target_task_hint = "сходить в магазин"
   - deadline_iso = null.
   Если фраза начинается на "я " и содержит глагол прошедшего времени
   ("сделал", "сходил", "выучил", "закрыл", "закончил", "скачал", "отправил"
   и похожие) → почти всегда это завершение задачи (complete), а не unknown.


4. Если он просит ПОКАЗАТЬ задачи:
   - action = "show_active" — если просит просто показать текущие/все активные.
   - action = "show_today" — если явно про сегодня/сегодняшний день.
   - остальные поля = null, raw_input = оригинальная фраза.

5. Если просит УДАЛИТЬ задачу:
   - action = "delete"
   - target_task_hint = по какой задаче.
   - deadline_iso = null.

6. Если запрос не про задачи, дедлайны или планирование:
   - action = "unknown"
   - остальные поля = null (кроме raw_input).
   - Ты НЕ придумываешь задачи, если человек просто задаёт общий вопрос.

7. Интерпретация дат и времени для поля deadline_iso:
   - если пользователь указал ТОЛЬКО время ("в 17:00", "в 6", "в 6 утра")
     → используй СЕГОДНЯШНЮЮ дату относительно "{now_str}".
   - если указан только день/дата ("завтра", "в субботу", "15 января")
     без конкретного времени → используй время 23:59 по "{DEFAULT_TIMEZONE}".
   - если указана и дата, и время → используй их как есть, в таймзоне "{DEFAULT_TIMEZONE}".
   - если даты нельзя понять → deadline_iso = null.
   - ЕСЛИ фраза состоит ТОЛЬКО из даты/времени/выражения вроде
     "сегодня", "завтра", "в субботу", "через 10 минут", "к 15 января" —
     ты всё равно обязан заполнить поле deadline_iso по этим правилам,
     даже если action при этом будет "unknown".

8. ОБЯЗАТЕЛЬНО:
   - Всегда возвращай корректный JSON-объект без текста до/после.
   - Не используй комментарии, лишние поля, NaN, Infinity.
"""


def _normalize_deadline_iso(raw_value: Any) -> str | None:
    """
    Нормализует deadline_iso к локальной таймзоне:
    - если None → None
    - если строка без tz → считаем, что это локальное время в DEFAULT_TIMEZONE
    - если строка с tz → переводим в DEFAULT_TIMEZONE
    - если формат кривой → возвращаем None (лучше без дедлайна, чем сломаться)
    """
    if raw_value is None:
        return None

    if not isinstance(raw_value, str):
        return None

    s = raw_value.strip()
    if not s:
        return None

    # Поддержка "Z" в конце
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Модель вернула что-то странное → игнорируем дедлайн
        return None

    if dt.tzinfo is None:
        # Время без таймзоны → считаем, что это локальное время пользователя
        dt = dt.replace(tzinfo=LOCAL_TZ)
    else:
        # Переводим в локальную таймзону
        dt = dt.astimezone(LOCAL_TZ)

    return dt.isoformat()


def parse_user_input(user_text: str) -> TaskInterpretation:
    now = datetime.now(LOCAL_TZ)
    now_str = now.isoformat()
    system_prompt = build_system_prompt(now_str)

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
    )

    raw = response.choices[0].message.content
    data: Dict[str, Any] = json.loads(raw)

    if "raw_input" not in data or not data["raw_input"]:
        data["raw_input"] = user_text

    data["deadline_iso"] = _normalize_deadline_iso(data.get("deadline_iso"))

    return TaskInterpretation.model_validate(data)

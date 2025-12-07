# src/llm_client.py

import json
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime
from zoneinfo import ZoneInfo
from openai import OpenAI

from config import OPENAI_API_KEY, OPENAI_MODEL, DEFAULT_TIMEZONE
from task_schema import TaskInterpretation

client = OpenAI(api_key=OPENAI_API_KEY)
LOCAL_TZ = ZoneInfo(DEFAULT_TIMEZONE)


def _format_tasks_for_prompt(
    tasks_snapshot: Optional[List[Tuple[int, str, Optional[str]]]]
) -> str:
    """
    Превращает список задач пользователя в компактный текстовый блок
    для вставки в системный промпт парсера.
    """
    if not tasks_snapshot:
        return "Сейчас у пользователя нет активных задач."

    lines: list[str] = []
    for i, (_tid, text, due) in enumerate(tasks_snapshot, start=1):
        if due:
            try:
                dt = datetime.fromisoformat(due).astimezone(LOCAL_TZ)
                d_str = dt.strftime("%Y-%m-%d %H:%M")
                lines.append(f"{i}. {text} (дедлайн: {d_str})")
            except Exception:
                lines.append(f"{i}. {text}")
        else:
            lines.append(f"{i}. {text}")

    return "Актуальные задачи пользователя:\n" + "\n".join(lines)


def build_system_prompt(now_str: str, tasks_block: str) -> str:
    return f"""
Ты — узкоспециализированный парсер команд для умного таск-менеджера в Telegram.

Твоя задача: взять ФРАЗУ ПОЛЬЗОВАТЕЛЯ и вернуть СТРОГО ОДИН JSON-ОБЪЕКТ,
который описывает, что нужно сделать с задачами.

Ты не ведёшь диалог и не отвечаешь текстом, только структурируешь запрос.

Текущие дата и время пользователя: "{now_str}" по таймзоне "{DEFAULT_TIMEZONE}".
Все ОТНОСИТЕЛЬНЫЕ даты ("сегодня", "завтра", "послезавтра", "через 2 дня",
"на следующей неделе", "в субботу" и т.п.) ты обязан интерпретировать
ИМЕННО относительно этого времени.

Ниже — АКТУАЛЬНЫЕ ЗАДАЧИ пользователя. Это единственный источник правды
о том, какие задачи вообще существуют:

{tasks_block}

Инструкции по работе со списком задач:

- Если пользователь просит ПЕРЕНЕСТИ, ОТМЕТИТЬ ВЫПОЛНЕННОЙ или УДАЛИТЬ задачу:
  - Всегда выбирай ровно ОДНУ задачу из списка выше, к которой относится действие.
  - Поле "target_task_hint" должно быть максимально похоже на текст задачи из списка.
  - НЕ придумывай новых задач в target_task_hint, если явно видно, что речь идёт
    об одной из существующих.

- Если фраза пользователя явно ссылается на задачу, которой НЕТ в списке,
  то всё равно заполни "target_task_hint" по смыслу, но понимай, что
  логика приложения может потом не найти такую задачу.

- Если пользователь создаёт НОВУЮ задачу (action = "create"), то "title"
  — это НОВАЯ формулировка, не обязанная совпадать ни с одной из задач в списке.

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

1. НОВАЯ задача:
   - action = "create"
   - title = сжатая формулировка задачи в повелительном наклонении.
   - deadline_iso:
       * если есть явная дата/время → нормальный ISO с таймзоной;
       * если даты нет вообще → deadline_iso = null.
   - target_task_hint = null.
   - Важно отличать план от завершения:
     * Если фраза звучит как план/намерение/необходимость ("надо", "нужно", "хочу",
       "планирую", "собираюсь", "надо сдать отчёт", "хочу выучить 10 слов") → это create.
     * Если нет явного признака, что действие уже сделано, выбирай create, а не complete.

2. ПЕРЕНОС задачи:
   - action = "reschedule"
   - title = null
   - target_task_hint = та формулировка, по которой можно найти ЗАДАЧУ ИЗ СПИСКА.
   - deadline_iso = новая дата/время (если есть).
   Примеры переносов (reschedule):
   - "перенеси созвон с тимлидом на завтра в 10 утра"
   - "сдвинь дедлайн дочитать книгу по истории на два дня"
   - "измени задачу дочитать книгу по истории на послезавтра"
   - "измени задачу про отчёт на пятницу вечером"

   Если фраза содержит слова "перенеси", "сдвинь", "измени задачу" +
   указание новой даты/времени/срока ("завтра", "через два дня",
   "в пятницу", "на понедельник"), то почти всегда это RESCHEDULE,
   а не создание новой задачи.
   - Если фраза содержит глаголы переименования ("переименовать", "переименуй",
     "измени название", "исправь название") — это НЕ перенос и НЕ завершение.
     В таких случаях ставь action = "unknown", чтобы приложение само обработало
     переименование.

3. ЗАВЕРШЕНИЕ задачи:
   - action = "complete"
   - target_task_hint = формулировка задачи, максимально похожая на одну из задач из списка.
   - deadline_iso = null.
   - Используй complete ТОЛЬКО если явно сказано, что задача УЖЕ сделана или прошла:
       "я сделал", "я уже сделал", "я сходил", "сходил", "закрыл", "закончил",
       "готово", "отчёт сдал", "встреча прошла", "можешь отметить выполненной".
     Фразы с модальными словами ("надо", "нужно", "хочу", "планирую", "собираюсь")
     относятся к планам → это почти всегда create, а не complete.

4. ПОКАЗАТЬ задачи:
   - action = "show_active" — если просит просто показать текущие/все активные.
   - action = "show_today" — если явно про сегодня/сегодняшний день.
   - остальные поля = null, raw_input = оригинальная фраза.

5. УДАЛИТЬ задачу:
   - action = "delete"
   - target_task_hint = по какой задаче (желательно совпадает с одной из задач из списка).
   - deadline_iso = null.

6. Не про задачи:
   - action = "unknown"
   - остальные поля = null (кроме raw_input).
   - Ты НЕ придумываешь задачи, если человек просто задаёт общий вопрос.

7. Интерпретация дат и времени для поля deadline_iso:
   - если пользователь указал ТОЛЬКО время ("в 17:00", "в 6", "в 6 утра")
     → используй СЕГОДНЯШНЮЮ дату относительно "{now_str}".
   - если указан только день/дата без времени → используй время 23:59 по "{DEFAULT_TIMEZONE}".
   - если указана и дата, и время → используй их как есть, в таймзоне "{DEFAULT_TIMEZONE}".
   - если даты нельзя понять → deadline_iso = null.
   - ЕСЛИ фраза состоит ТОЛЬКО из даты/времени/выражения вроде
     "сегодня", "завтра", "в субботу", "через 10 минут", "к 15 января" —
     ты всё равно обязан заполнить поле deadline_iso по этим правилам,
     даже если action при этом будет "unknown".

8. ОБЯЗАТЕЛЬНО:
   - Всегда возвращай корректный JSON-объект без текста до/после.
   - Не используй комментарии, лишние поля, NaN, Infinity.

9. Отдельно про "надо/нужно/хочу/планирую":
   - Фразы вида "надо сдать отчёт", "нужно помыть балкон",
     "хочу подготовиться к мидтерму", "планирую выучить 20 слов"
     трактуй как action = "create" (новая задача),
     даже если в списке задач есть что-то похожее.
   - Для action = "complete" требуй прошедшее время:
     "сделал", "сдал", "сходил", "дочитал", "позвонил",
     "разобрал", "помыл" и т.п.
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


def parse_user_input(
    user_text: str,
    tasks_snapshot: Optional[List[Tuple[int, str, Optional[str]]]] = None,
) -> TaskInterpretation:
    now = datetime.now(LOCAL_TZ)
    now_str = now.isoformat()

    tasks_block = _format_tasks_for_prompt(tasks_snapshot)
    system_prompt = build_system_prompt(now_str, tasks_block)

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


def build_reply_prompt() -> str:
    return """
Ты — голос Telegram-бота Smart-Tasker, умного таск-менеджера.

На вход ты получаешь ОДИН JSON-объект, описывающий, что произошло:
создана задача, перенесён дедлайн, задача выполнена, удалена, не найдена и т.п.

Твоя задача — вернуть КОРОТКИЙ человеко-понятный ответ пользователю
на русском языке. Без JSON, без объяснений, только текст сообщения.

Стиль:
- Дружелюбно, но без чрезмерного пафоса.
- Коротко: 1–2 строки, максимум ~180 символов.
- Можно использовать эмодзи, но умеренно (0–2 на сообщение).
- Не повторяй дословно всю структуру события, говори естественно.

Типы событий (поле "type"):

- "task_created":
    Новая задача добавлена. Используй "task_text" и, если есть, "deadline_iso".
    Если deadline_iso есть — упомяни дату/время в человеческом формате.
    Если дедлайна нет — можно мягко намекнуть, что дедлайн можно поставить.

- "task_completed":
    Задача выполнена. Подтверди и, по желанию, коротко похвали.

- "task_deleted":
    Задача удалена. Коротко подтверди.

- "task_rescheduled":
    Дедлайн изменён. Можешь кратко сказать, на когда перенесли.

- "show_tasks":
    Пользователь запросил список задач (список отправляется отдельным сообщением).
    Можно сказать что-то вроде: "Вот твои задачи на сейчас."

- "no_tasks":
    У пользователя нет задач. Можно предложить отдохнуть или добавить новую.

- "task_not_found":
    Приложение не смогло сопоставить фразу ни с одной задачей.
    Объясни это мягко и предложи сформулировать задачу точнее.

- "error":
    Что-то пошло не так внутри приложения. Скажи об этом коротко и нейтрально.

Важно:
- Таймзона пользователя — Asia/Almaty. Если нужно упомянуть дату/время,
  используй формат "дд.мм HH:MM" и только если в JSON есть уже подготовленное
  поле с человеком читаемой датой.
- Если каких-то полей нет или они null — просто игнорируй их.
- Всегда возвращай ТОЛЬКО один текстовый ответ без кавычек и без JSON.
"""


def _format_deadline_human(deadline_iso: Optional[str]) -> Optional[str]:
    """
    Превращает ISO-дедлайн в строку "дд.мм HH:MM" в локальной таймзоне.
    """
    if not deadline_iso:
        return None
    try:
        dt = datetime.fromisoformat(deadline_iso).astimezone(LOCAL_TZ)
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return None


def render_user_reply(event: Dict[str, Any]) -> str:
    """
    Принимает JSON-событие и возвращает текст ответа пользователю.
    """
    deadline_human = _format_deadline_human(event.get("deadline_iso"))
    prev_deadline_human = _format_deadline_human(event.get("prev_deadline_iso"))

    enriched_event = dict(event)
    enriched_event["deadline_human"] = deadline_human
    enriched_event["prev_deadline_human"] = prev_deadline_human

    system_prompt = build_reply_prompt()

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0.4,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": "Вот событие в JSON. Сформулируй короткий ответ пользователю:\n\n"
                + json.dumps(enriched_event, ensure_ascii=False),
            },
        ],
    )

    text = response.choices[0].message.content.strip()
    return text

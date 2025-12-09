# src/llm_client.py

import json
import logging
import re
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime
from zoneinfo import ZoneInfo
from openai import OpenAI

from config import OPENAI_API_KEY, OPENAI_MODEL, DEFAULT_TIMEZONE
from task_schema import TaskInterpretation

client = OpenAI(api_key=OPENAI_API_KEY)
LOCAL_TZ = ZoneInfo(DEFAULT_TIMEZONE)

# Хелперы для времени суток и дат
MORNING_WORDS = {"утром", "утро", "morning"}
DAY_WORDS = {"днём", "днем", "день", "daytime", "afternoon", "day"}
EVENING_WORDS = {"вечером", "вечер", "evening"}
NIGHT_WORDS = {"ночью", "ночь", "night"}

TODAY_WORDS = {"сегодня", "today"}
TOMORROW_WORDS = {"завтра", "tomorrow"}

WEEKDAY_RU = {
    "понедельник": 0,
    "вторник": 1,
    "среда": 2,
    "среду": 2,
    "четверг": 3,
    "пятница": 4,
    "пятницу": 4,
    "суббота": 5,
    "субботу": 5,
    "воскресенье": 6,
}
WEEKDAY_EN = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
logger = logging.getLogger(__name__)


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
- Если в фразе есть только местоимения ("ей", "ему", "эту задачу") без
  содержательных слов, совпадающих с текстами задач, НЕ пытайся угадывать
  конкретную задачу. В таких случаях лучше вернуть action = "unknown" или
  оставить target_task_hint как есть (местоимение), чтобы приложение вернуло
  "не нашёл задачу".
- Не вставляй в target_task_hint текст задачи из списка, если в пользовательской
  фразе нет лексического пересечения с этим текстом. target_task_hint должен
  основываться на словах пользователя.

- Если пользователь создаёт НОВУЮ задачу (action = "create"), то "title"
  — это НОВАЯ формулировка, не обязанная совпадать ни с одной из задач в списке.

Формат ответа — всегда ровно такой JSON:

{{
  "action": "create | reschedule | complete | delete | show_active | show_today | show_tomorrow | show_date | unknown",
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
      * Английский императив ("finish math assignment", "book appointment", "send update", "schedule call") → это create, если нет явного прошедшего времени.
     * Если нет явного признака, что действие уже сделано, выбирай create, а не complete.

2. ПЕРЕНОС задачи:
   - action = "reschedule"
   - title = null
   - target_task_hint = та формулировка, по которой можно найти ЗАДАЧУ ИЗ СПИСКА.
   - deadline_iso = новая дата/время (если есть).
   - Фразы вида "добавь/поставь/назначь дедлайн/срок к задаче ..." тоже считаются reschedule,
     даже если у задачи раньше не было дедлайна (было null → стало дата/время).
   Примеры переносов (reschedule):
   - "перенеси созвон с тимлидом на завтра в 10 утра"
   - "сдвинь дедлайн дочитать книгу по истории на два дня"
   - "измени задачу дочитать книгу по истории на послезавтра"
   - "измени задачу про отчёт на пятницу вечером"
   - "добавь дедлайн к задаче про тимбилдинг на пятницу"
   - "поставь срок на встречу с ректором в понедельник в 11"
   - "назначь дедлайн на проект завтра в 10"

   Если фраза содержит слова "перенеси", "сдвинь", "измени задачу" +
   указание новой даты/времени/срока ("завтра", "через два дня",
   "в пятницу", "на понедельник"), то почти всегда это RESCHEDULE,
   а не создание новой задачи.
   Если в тексте просто "сделать/напиши/добавить ..." без слов переноса —
   это CREATE, даже если текст похож на существующую задачу.
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
     Английские маркеры завершения: "I finished", "I've finished", "I completed", "it's done".

4. ПОКАЗАТЬ задачи:
   - action = "show_active" — если просит просто показать текущие/все активные.
   - action = "show_today" — если явно про сегодня/сегодняшний день.
   - action = "show_tomorrow" — если явно про завтра.
   - action = "show_date" — если указан конкретный день/дата; deadline_iso = этот день в 23:59 (если времени нет) или указанное время.
   - остальные поля = null, raw_input = оригинальная фраза (deadline_iso только для show_date).

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
   - если указан день/дата + указание времени суток без цифр ("утром", "днём", "вечером", "ночью")
     → ставь время по умолчанию: утром=09:00, днём=15:00, вечером=21:00, ночью=23:00.
   - если указана и дата, и время → используй их как есть, в таймзоне "{DEFAULT_TIMEZONE}".
   - если даты нельзя понять → deadline_iso = null.
   - "до/к <дню недели>" означает конец этого дня (23:59) без смещения на предыдущий день.
   - ЕСЛИ фраза состоит ТОЛЬКО из даты/времени/выражения вроде
     "сегодня", "завтра", "в субботу", "через 10 минут", "к 15 января" —
     ты всё равно обязан заполнить поле deadline_iso по этим правилам,
     даже если action при этом будет "unknown".

8. ОБЯЗАТЕЛЬНО:
   - Всегда возвращай корректный JSON-объект без текста до/после.
   - Не используй комментарии, лишние поля, NaN, Infinity.

9. Отдельно про массовые действия и "очистить всё":
   - Если фраза звучит так, будто пользователь говорит про ВСЕ задачи сразу,
     а не про одну конкретную, НЕ нужно выбирать delete/complete для одной задачи.
   - Примеры таких фраз:
       * "я всё сделал по учёбе"
       * "можно очистить список задач?"
       * "очисти список задач"
       * "очистить список задач"
       * "очисти все задачи"
       * "удали все задачи"
       * "удалить все задачи"
       * "убери все задачи"
       * "убери всё из списка"
       * "очистить задачи"
       * "очисти задачи"
   - В таких случаях ставь:
       action = "unknown"
       target_task_hint = null
       deadline_iso = null

10. Отдельно про "надо/нужно/хочу/планирую":
   - Фразы вида "надо сдать отчёт", "нужно помыть балкон",
     "хочу подготовиться к мидтерму", "планирую выучить 20 слов"
     трактуй как action = "create" (новая задача),
     даже если в списке задач есть что-то похожее.
   - Для action = "complete" требуй прошедшее время:
     "сделал", "сдал", "сходил", "дочитал", "позвонил",
     "разобрал", "помыл" и т.п.
"""


def build_system_prompt_multi(now_str: str, tasks_block: str, max_items: int) -> str:
    return f"""
Ты — узкоспециализированный парсер команд для умного таск-менеджера в Telegram.

Твоя задача: взять ФРАЗУ ПОЛЬЗОВАТЕЛЯ и вернуть СТРОГО ОДИН JSON-ОБЪЕКТ верхнего уровня,
который содержит массив элементов (задач/действий).

Формат ответа:
{{
  "items": [
    {{
      "action": "create | complete",
      "title": "строка или null",
      "deadline_iso": "ISO 8601 или null",
      "target_task_hint": "строка или null",
      "note": "строка или null",
      "language": "ru | en | другое или null",
      "raw_input": "оригинальный фрагмент пользователя"
    }}
  ]
}}

Важно:
- Максимум {max_items} элементов в массиве. Бери только самые явные задачи.
- Каждый элемент описывает ОДНУ независимую задачу/действие. Не придумывай лишние.
- На этом этапе поддерживаем только action = "create" (новая задача) и "complete" (уже выполненная задача).
- Если фраза без самостоятельной задачи — пропусти её.
- Не используй ссылки вроде "ей", "ему", "этой задаче" без содержательных слов. Если нет опоры на текст задачи, лучше вернуть action="unknown" или не создавать элемент.
- target_task_hint основывай только на словах пользователя; не подставляй текст задач из списка без лексического пересечения с фразой.

Правила интерпретации дат/времени (строго как в single-парсере):
- если указано ТОЛЬКО время → используй СЕГОДНЯШНЮЮ дату относительно "{now_str}" (TZ "{DEFAULT_TIMEZONE}").
- если указана только дата/день недели/относительный срок ("до понедельника", "в пятницу", "завтра", "через 3 дня") без времени → ставь время 23:59 того дня в "{DEFAULT_TIMEZONE}".
- если указано время суток без цифр ("утром", "днём", "вечером", "ночью") → ставь соответственно 09:00 / 15:00 / 21:00 / 23:00.
- если указана и дата, и время → используй их как есть в "{DEFAULT_TIMEZONE}".
- если даты нельзя понять → deadline_iso = null.
- День недели трактуем как ближайший будущий соответствующий день относительно "{now_str}". "до/к понедельнику" → конец этого дня (23:59).

Как делить исходный текст:
- Мысленно раздели сообщение на смысловые фрагменты: по предложениям, нумерации ("1.", "2.", "во-первых"), словам "дальше", "потом", переносам строк.
- Для каждого фрагмента реши, есть ли явная новая задача или факт выполнения. Если нет — не добавляй элемент.
- Не более {max_items} элементов из одного сообщения.

Отличия create vs complete:
- create — планы/намерения/будущее: "надо", "нужно", "хочу", "планирую", формы будущего времени; английский императив ("finish", "book", "send", "schedule") → create.
- complete — уже свершилось: прошедшее время/слова "сделал", "сделала", "закрыл", "сдал", "готов", "провёл"; английские маркеры "finished", "have finished", "completed", "done".
- Для complete deadline_iso = null; target_task_hint должен помогать найти задачу из списка.

Ниже — актуальные задачи пользователя (для контекста, новые задачи могут быть любыми):
{tasks_block}
"""


def _normalize_deadline_iso(raw_value: Any, raw_text: Optional[str] = None) -> str | None:
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

    # Вспомогательные флаги
    time_in_text = None
    if raw_text:
        m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\b", raw_text)
        if m:
            h = int(m.group(1))
            mm = int(m.group(2)) if m.group(2) else 0
            if 0 <= h <= 23 and 0 <= mm <= 59:
                time_in_text = (h, mm)

    part_of_day_hour = None
    if raw_text:
        t_lower = raw_text.lower()
        if any(w in t_lower for w in MORNING_WORDS):
            part_of_day_hour = 9
        elif any(w in t_lower for w in DAY_WORDS):
            part_of_day_hour = 15
        elif any(w in t_lower for w in EVENING_WORDS):
            part_of_day_hour = 21
        elif any(w in t_lower for w in NIGHT_WORDS):
            part_of_day_hour = 23

    # Определяем, было ли время в iso-строке
    has_time_in_iso = bool(re.search(r"\d{1,2}:\d{2}", s)) or ("T" in s)

    if dt.tzinfo is None:
        # Время без таймзоны → считаем, что это локальное время пользователя
        dt = dt.replace(tzinfo=LOCAL_TZ)
    else:
        # Переводим в локальную таймзону
        dt = dt.astimezone(LOCAL_TZ)

    # Применяем время, приоритезируя явные указания пользователя
    if time_in_text and part_of_day_hour is not None:
        hour = time_in_text[0]
        minute = time_in_text[1]
        # Если указан вечер/ночь и время в 1-11 → прибавляем 12 часов
        if part_of_day_hour >= 12 and hour < 12:
            hour = min(hour + 12, 23)
        dt = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    elif time_in_text:
        dt = dt.replace(hour=time_in_text[0], minute=time_in_text[1], second=0, microsecond=0)
    elif part_of_day_hour is not None:
        dt = dt.replace(hour=part_of_day_hour, minute=0, second=0, microsecond=0)
    elif not has_time_in_iso:
        # дата без времени → конец дня
        dt = dt.replace(hour=23, minute=59, second=0, microsecond=0)
    else:
        # iso содержит время, но если в тексте не было времени и не было времени суток,
        # прижимаем к 23:59, чтобы убрать "креатив" типа 22:59
        dt = dt.replace(hour=23, minute=59, second=0, microsecond=0)

    return dt.isoformat()


def _sanitize_target_hint(user_text: str, hint: Optional[str]) -> Optional[str]:
    """
    Сбрасывает target_task_hint, если в нём нет лексического пересечения с текстом пользователя.
    Это защита от галлюцинаций (подстановка текста чужой задачи).
    """
    if not hint:
        # Попробуем вытянуть содержательную часть после "про/с" как подсказку
        m = re.search(r"(?:про|по|с)\s+(.+)", user_text, flags=re.IGNORECASE)
        if m:
            extracted = m.group(1).strip()
            if extracted:
                hint = extracted
            else:
                return None
        else:
            return None

    def _stem(token: str) -> str:
        token = token.lower()
        return re.sub(
            r"(ому|ему|ого|ими|ыми|ами|лях|ях|ах|ам|ой|ый|ий|ая|ое|ые|ую|ом|ев|ов|ей|ами?|ях|ях|ах)$",
            "",
            token,
        )

    user_tokens = {_stem(t) for t in re.findall(r"\w+", user_text.lower()) if t}
    hint_tokens = {_stem(t) for t in re.findall(r"\w+", hint.lower()) if t}

    if not user_tokens or not hint_tokens:
        return None

    if user_tokens.isdisjoint(hint_tokens):
        return None

    return hint


def parse_user_input_multi(
    user_text: str,
    tasks_snapshot: Optional[List[Tuple[int, str, Optional[str]]]] = None,
    max_items: int = 5,
) -> List[TaskInterpretation]:
    """
    Парсит сообщение в массив задач/действий (create | complete).
    """
    now = datetime.now(LOCAL_TZ)
    now_str = now.isoformat()

    tasks_block = _format_tasks_for_prompt(tasks_snapshot)
    system_prompt = build_system_prompt_multi(now_str, tasks_block, max_items)

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
    try:
        data: Dict[str, Any] = json.loads(raw)
    except Exception:
        logger.exception("Failed to decode multi-parse JSON: %r", raw)
        return []

    items = data.get("items")
    if not isinstance(items, list):
        return []

    results: list[TaskInterpretation] = []
    unsupported = 0

    for item in items[:max_items]:
        if not isinstance(item, dict):
            continue

        action = item.get("action")
        if action not in {"create", "complete"}:
            unsupported += 1
            continue

        # English imperative mistakenly marked complete -> treat as create
        if action == "complete":
            raw_frag = (item.get("raw_input") or user_text or "").lower()
            past_markers = ["finished", "i finished", "i've finished", "i completed", "completed", "have done", "done", "i did", "i have done", "it's done"]
            imperative_markers = ["finish", "book", "send", "schedule", "call", "buy", "make", "add", "plan"]
            has_past = any(pm in raw_frag for pm in past_markers)
            has_imperative = any(im in raw_frag for im in imperative_markers)
            if has_imperative and not has_past:
                action = "create"
                item["action"] = "create"

        ti_dict = dict(item)
        ti_dict["action"] = action
        ti_dict["deadline_iso"] = (
            _normalize_deadline_iso(item.get("deadline_iso"), item.get("raw_input") or user_text)
            if action == "create"
            else None
        )
        ti_dict["raw_input"] = item.get("raw_input") or user_text
        ti_dict["target_task_hint"] = _sanitize_target_hint(
            item.get("raw_input") or user_text, item.get("target_task_hint")
        )

        try:
            ti = TaskInterpretation.model_validate(ti_dict)
            if not ti.title and not ti.raw_input:
                continue
            results.append(ti)
        except Exception:
            logger.exception("Failed to validate multi item: %r", ti_dict)
            continue

    if unsupported:
        logger.info("Multi-parse skipped %d unsupported actions", unsupported)

    return results


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

    data["deadline_iso"] = _normalize_deadline_iso(data.get("deadline_iso"), user_text)
    data["target_task_hint"] = _sanitize_target_hint(user_text, data.get("target_task_hint"))

    # Heuristic: если модель решила reschedule, но в тексте нет явных слов переноса —
    # трактуем как создание новой задачи (защита от ложного reschedule на многофразовые команды).
    if data.get("action") == "reschedule":
        lower = user_text.lower()
        transfer_markers = [
            "перенеси",
            "перенести",
            "сдвинь",
            "сдвинуть",
            "измени задачу",
            "перенесите",
            "move",
            "reschedule",
            "shift",
            "delay",
            "добавь дедлайн",
            "поставь дедлайн",
            "добавь срок",
            "поставь срок",
            "назначь срок",
            "назначь дедлайн",
        ]
        if not any(m in lower for m in transfer_markers):
            data["action"] = "create"
            data["title"] = data.get("title") or data.get("raw_input")
            data["target_task_hint"] = None
            # deadline оставляем, если был
    # Если про выходные — показываем ближайшие субботу и воскресенье как два дня (через note)
    if data.get("action") in {"show_active", "show_today", "show_tomorrow", "show_date"}:
        lower = user_text.lower()
        if "выходных" in lower or "выходные" in lower or "weekend" in lower:
            data["action"] = "show_date"
            data["note"] = "weekend"
            data["deadline_iso"] = None

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


def transcribe_audio(file_path: str) -> Optional[str]:
    """
    Расшифровывает аудиофайл (голосовое из Telegram) в текст с помощью OpenAI.
    Возвращает строку с текстом или None в случае ошибки.
    """
    try:
        with open(file_path, "rb") as f:
            result = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=f,
            )
        text = getattr(result, "text", None)
        if text:
            return text.strip()
        return None
    except Exception:
        return None

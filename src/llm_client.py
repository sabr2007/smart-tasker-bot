# src/llm_client.py

import json
import logging
import re
import difflib
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime, timedelta
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

# Максимум элементов в multi-ответе, которые готовы обработать в одном запросе
MAX_ITEMS_PER_REQUEST = 10
# Сколько delete подряд считаем потенциально опасными без подтверждения
MAX_DELETE_WITHOUT_CONFIRM = 2


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
- Слова "удали", "убери", "сотри", "delete", "remove" → всегда action = "delete".
- Слова "перенеси", "сдвинь", "move", "shift", "reschedule", "change deadline",
  "поставь дедлайн/срок", "добавь дедлайн/срок", "назначь дедлайн/срок" → всегда action = "reschedule".

- Если пользователь создаёт НОВУЮ задачу (action = "create"), то "title"
  — это НОВАЯ формулировка, не обязанная совпадать ни с одной из задач в списке.

Формат ответа — всегда ровно такой JSON:

{{
  "action": "create | reschedule | complete | delete | rename | show_active | show_today | show_tomorrow | show_date | unknown",
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
   - title обязателен; если не получается выделить внятный заголовок — лучше unknown.
   - deadline_iso:
       * если есть явная дата/время → нормальный ISO с таймзоной;
       * если даты нет вообще → deadline_iso = null.
   - target_task_hint = null.
   - Важно отличать план от завершения:
     * Если фраза звучит как план/намерение/необходимость ("надо", "нужно", "хочу",
       "планирую", "собираюсь", "надо сдать отчёт", "хочу выучить 10 слов") → это create.
     * Английский императив ("finish math assignment", "book appointment", "send update", "schedule call") → это create, если нет явного прошедшего времени.
     * Если нет явного признака, что действие уже сделано, выбирай create, а не complete.
   - Не включай в title фразы про сроки ("к понедельнику", "до пятницы"); эти куски должны отразиться в deadline_iso.

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
   - "move team meeting to next monday at 11am"
   - "поставь/добавь/назначь дедлайн/срок" → это reschedule.

   Если фраза содержит слова "перенеси", "сдвинь", "измени задачу" +
   указание новой даты/времени/срока ("завтра", "через два дня",
   "в пятницу", "на понедельник"), то почти всегда это RESCHEDULE,
   а не создание новой задачи.
   Если в тексте просто "сделать/напиши/добавить ..." без слов переноса —
   это CREATE, даже если текст похож на существующую задачу.
   - Переименование — отдельное действие (см. пункт про rename).

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
   - action = "delete" если есть "удали", "убери", "сотри", "delete", "remove".
   - target_task_hint = по какой задаче (желательно совпадает с одной из задач из списка).
   - deadline_iso = null.

6. ПЕРЕИМЕНОВАНИЕ задачи:
   - action = "rename"
   - Когда пользователь явно просит изменить название задачи:
     "переименуй задачу про курсовую на отчёт по истории",
     "измени название задачи с созвона с Тимуром на созвон с тимлидом",
     "rename task about math to calculus homework".
   - title = новое название задачи (как она должна называться после переименования).
   - target_task_hint = старая формулировка или фраза, по которой можно найти существующую задачу
     (как пользователь её описал).
   - deadline_iso = null.
   - Не создаём новую задачу, меняем только текст существующей.

7. Не про задачи:
   - action = "unknown"
   - остальные поля = null (кроме raw_input).
   - Ты НЕ придумываешь задачи, если человек просто задаёт общий вопрос.

8. Интерпретация дат и времени для поля deadline_iso:
   - если пользователь указал ТОЛЬКО время ("в 17:00", "в 6", "в 6 утра")
     → используй СЕГОДНЯШНЮЮ дату относительно "{now_str}".
   - если указан только день/дата без времени → используй время 23:59 по "{DEFAULT_TIMEZONE}".
   - если указан день/дата + указание времени суток без цифр ("утром", "днём", "вечером", "ночью")
     → ставь время по умолчанию: утром=09:00, днём=15:00, вечером=21:00, ночью=23:00.
   - если указано явное время (09:00, 19:00, 11am) и одновременно есть слова "утром/днём/вечером/ночью" — оставляй указанное время как есть; части суток не меняют часы (кроме правила 7 вечера → 19:00).
   - если указан день недели + конкретное время ("к понедельнику 12:00", "Friday 19:00") — используй ровно этот день недели и это время без сдвигов.
   - если указана и дата, и время → используй их как есть, в таймзоне "{DEFAULT_TIMEZONE}".
   - если даты нельзя понять → deadline_iso = null.
   - "до/к <дню недели>" означает конец этого дня (23:59) без смещения на предыдущий день.
   - ЕСЛИ фраза состоит ТОЛЬКО из даты/времени/выражения вроде
     "сегодня", "завтра", "в субботу", "через 10 минут", "к 15 января" —
     ты всё равно обязан заполнить поле deadline_iso по этим правилам,
     даже если action при этом будет "unknown".

9. ОБЯЗАТЕЛЬНО:
   - Всегда возвращай корректный JSON-объект без текста до/после.
   - Не используй комментарии, лишние поля, NaN, Infinity.

10. Отдельно про массовые действия и "очистить всё":
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

11. Отдельно про "надо/нужно/хочу/планирую":
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
      "action": "create | complete | reschedule | delete | rename",
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
- Максимум {max_items} элементов в массиве. Бери только самые явные задачи/действия.
- Каждый элемент описывает ОДНУ независимую задачу/действие. Не придумывай лишние.
- Разрешены action = "create", "complete", "reschedule", "delete", "rename".
- Если фраза без самостоятельной задачи — пропусти её.
- Не используй ссылки вроде "ей", "ему", "этой задаче" без содержательных слов. Если нет опоры на текст задачи, лучше вернуть action="unknown" или не создавать элемент.
- target_task_hint основывай только на словах пользователя; не подставляй текст задач из списка без лексического пересечения с фразой.
- Переименование ("переименуй", "rename", "измени название") обрабатывай как action = "rename":
  * title = новое название задачи,
  * target_task_hint = как пользователь описал текущую задачу,
  * deadline_iso = null.
- Не включай куски про сроки ("к пятнице", "by Friday", "на субботу 15:00") в title. Сроки должны отражаться только в deadline_iso.
- Если во фрагменте есть явная дата/время/день недели/relative ("сегодня", "завтра", "Friday 19:00") — deadline_iso не может быть null. Подготовь корректный ISO.

Правила интерпретации дат/времени (строго как в single-парсере):
- если указано ТОЛЬКО время → используй СЕГОДНЯШНЮЮ дату относительно "{now_str}" (TZ "{DEFAULT_TIMEZONE}").
- если указана только дата/день недели/относительный срок ("до понедельника", "в пятницу", "завтра", "через 3 дня") без времени → ставь время 23:59 того дня в "{DEFAULT_TIMEZONE}".
- если указано время суток без цифр ("утром", "днём", "вечером", "ночью") → ставь соответственно 09:00 / 15:00 / 21:00 / 23:00.
- если указана и дата, и время → используй их как есть в "{DEFAULT_TIMEZONE}".
- если указано явное время (09:00, 19:00, 11am) и есть слова "утром/днём/вечером/ночью" — оставляй указанное время как есть; части суток не меняют часы (кроме правила 7 вечера → 19:00).
- если указан день недели + время ("к понедельнику 12:00", "Friday 19:00") — используй ровно этот день недели и это время без сдвигов.
- если даты нельзя понять → deadline_iso = null.
- День недели трактуем как ближайший будущий соответствующий день относительно "{now_str}". "до/к понедельнику" → конец этого дня (23:59).

Как делить исходный текст:
- Мысленно раздели сообщение на смысловые фрагменты: по предложениям, нумерации ("1.", "2.", "во-первых"), словам "дальше", "потом", переносам строк.
- Союзы "и/та/а ещё/also/and" между глаголами удаления/создания/выполнения — это отдельные элементы, если там разные задачи.
- Для каждого фрагмента реши, есть ли явная новая задача или факт выполнения. Если нет — не добавляй элемент.
- Не более {max_items} элементов из одного сообщения.

Отличия create/complete/reschedule/delete/rename:
- create — планы/намерения/будущее: "надо", "нужно", "хочу", "планирую", формы будущего времени; английский императив ("finish", "book", "send", "schedule") → create. Title обязателен; не включай сроки ("к пятнице") в title.
- complete — уже свершилось: прошедшее время/слова "сделал", "сделала", "закрыл", "сдал", "готов", "провёл"; английские маркеры "finished", "have finished", "completed", "done". deadline_iso = null; target_task_hint помогает найти задачу из списка. Если в списке задач есть пересечение по словам — используй максимально похожее название задачи.
- reschedule — есть глаголы переноса/установки срока ("перенеси", "сдвинь", "move", "reschedule", "поставь/добавь/назначь дедлайн/срок"); deadline_iso = новая дата/время.
- delete — "удали", "убери", "сотри", "delete", "remove"; deadline_iso = null. Для delete обязателен target_task_hint (по словам пользователя).
- rename — явная просьба изменить название задачи ("переименуй задачу...", "измени название...", "rename task ..."): title = новое имя, target_task_hint = старая формулировка, deadline_iso = null.

Ниже — актуальные задачи пользователя (для контекста, новые задачи могут быть любыми):
{tasks_block}
"""


def _guess_deadline_from_text(raw_text: str) -> Optional[datetime]:
    """
    Грубый fallback, если модель не вернула валидный ISO,
    но в тексте явно есть день/время.
    """
    if not raw_text:
        return None

    t_lower = raw_text.lower()
    now = datetime.now(LOCAL_TZ)

    # --- время ---
    time_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", t_lower)
    hour = None
    minute = 0
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2)) if time_match.group(2) else 0
        meridiem = time_match.group(3)
        if meridiem:
            if meridiem == "pm" and hour < 12:
                hour += 12
            if meridiem == "am" and hour == 12:
                hour = 0

    part_of_day_hour = None
    if hour is None:
        if any(w in t_lower for w in MORNING_WORDS):
            part_of_day_hour = 9
        elif any(w in t_lower for w in DAY_WORDS):
            part_of_day_hour = 15
        elif any(w in t_lower for w in EVENING_WORDS):
            part_of_day_hour = 21
        elif any(w in t_lower for w in NIGHT_WORDS):
            part_of_day_hour = 23

    # --- дата ---
    target_date = None
    for word, idx in WEEKDAY_RU.items():
        if word in t_lower:
            delta = (idx - now.weekday() + 7) % 7
            delta = delta or 7  # ближайший будущий
            target_date = now.date() + timedelta(days=delta)
            break
    if target_date is None:
        for word, idx in WEEKDAY_EN.items():
            if word in t_lower:
                delta = (idx - now.weekday() + 7) % 7
                delta = delta or 7
                target_date = now.date() + timedelta(days=delta)
                break
    if target_date is None:
        if any(w in t_lower for w in TODAY_WORDS):
            target_date = now.date()
        elif any(w in t_lower for w in TOMORROW_WORDS):
            target_date = (now + timedelta(days=1)).date()

    if target_date is None and hour is not None:
        target_date = now.date()

    if target_date is None:
        return None

    if hour is None:
        hour = part_of_day_hour if part_of_day_hour is not None else 23
        minute = 59 if part_of_day_hour is None else 0

    return datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        hour,
        minute,
        tzinfo=LOCAL_TZ,
    )


def _normalize_deadline_iso(raw_value: Any) -> str | None:
    """
    Нормализует deadline_iso:
    - если модель вернула ISO с таймзоной — не конвертируем время, только форматируем;
    - если TZ нет — считаем локальной (LOCAL_TZ);
    - если указана только дата без времени — ставим 23:59 этого дня;
    - никаких 00:00 по умолчанию.
    """
    if raw_value is None or not isinstance(raw_value, str):
        return None

    s = raw_value.strip()
    if not s:
        return None

    # Поддержка "Z" в конце как UTC
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        logger.warning("deadline_iso is not valid ISO: %r", raw_value)
        return None

    # Определяем, была ли явная часть времени в исходной строке
    has_explicit_time = ("T" in s) or (" " in s)

    # Если в строке не было времени — ставим 23:59
    if not has_explicit_time:
        dt = dt.replace(hour=23, minute=59, second=0, microsecond=0)
    # Если было время, не меняем его

    # Если TZ нет — считаем, что это локальное время пользователя
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)

    # ВАЖНО: не делаем astimezone, чтобы не сдвигать время
    return dt.isoformat()


def _sanitize_target_hint(
    raw_input: str,
    hint: Optional[str],
    max_len: int = 120,
) -> Optional[str]:
    """
    Очищает target_task_hint, не споря с моделью:
    - оставляем только если это подстрока исходного текста (case-insensitive);
    - убираем кавычки и служебные символы по краям;
    - обрезаем по границе слова до max_len;
    - если связи с текстом нет — возвращаем None.
    """
    if not hint:
        return None

    candidate = hint.strip().strip(" «»\"'“”„;")
    if not candidate:
        return None

    raw_lower = raw_input.lower()
    cand_lower = candidate.lower()

    # Не подсовываем текст, которого не было в реплике пользователя
    if cand_lower not in raw_lower:
        return None

    if len(candidate) > max_len:
        cut = candidate[:max_len]
        candidate = cut.rsplit(" ", 1)[0] or cut

    return candidate or None


def _match_task_title_from_snapshot(
    tasks_snapshot: Optional[List[Tuple[int, str, Optional[str]]]],
    hint: Optional[str],
) -> Optional[str]:
    """
    Матч задачи по hint:
    1) если hint как подстрока встречается ровно в одной задаче — возвращаем её;
    2) иначе — fuzzy-матч с порогом и пересечением по содержательным словам;
    3) иначе — None.
    """
    if not tasks_snapshot or not hint:
        return None

    hint_lower = hint.lower()

    # 1) прямое вхождение
    exact = [text for _, text, _ in tasks_snapshot if hint_lower in text.lower()]
    if len(exact) == 1:
        return exact[0]

    # 2) fuzzy с пересечением токенов
    def _tokens(s: str) -> set[str]:
        parts = re.split(r"[\s,.;:!?\"'«»„“”()]+", s.lower())
        return {p for p in parts if len(p) >= 3}

    hint_tokens = _tokens(hint)
    if not hint_tokens:
        return None

    best_text: Optional[str] = None
    best_score: float = 0.0

    for _, text, _ in tasks_snapshot:
        text_tokens = _tokens(text)
        # если нет пересечения по смысловым словам — пропускаем
        if not (hint_tokens & text_tokens):
            continue

        score = difflib.SequenceMatcher(None, hint_lower, text.lower()).ratio()
        if score > best_score:
            best_score = score
            best_text = text

    return best_text if best_score >= 0.55 else None


def _validate_action(value: str) -> str:
    allowed = {"create", "complete", "reschedule", "delete", "rename", "show_active", "show_today", "show_tomorrow", "show_date", "unknown"}
    return value if value in allowed else "unknown"


def parse_user_input_multi(
    user_text: str,
    tasks_snapshot: Optional[List[Tuple[int, str, Optional[str]]]] = None,
    max_items: int = 5,
) -> List[TaskInterpretation]:
    """
    Тонкий адаптер над multi-ответом модели. Модель — источник истины,
    здесь только нормализация формата и бизнес-ограничения.
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

    # Бизнес-лимит по количеству операций
    if len(items) > MAX_ITEMS_PER_REQUEST:
        logger.warning("Too many items in multi request: %s", len(items))
        items = items[:MAX_ITEMS_PER_REQUEST]

    results: list[TaskInterpretation] = []

    # Посчитаем delete для простого анти-спама
    delete_count = sum(1 for it in items if isinstance(it, dict) and (it.get("action") or "").strip() == "delete")

    for item in items:
        if not isinstance(item, dict):
            continue

        action = _validate_action((item.get("action") or "unknown").strip())

        # Ограничение на массовые удаления
        if action == "delete" and delete_count > MAX_DELETE_WITHOUT_CONFIRM:
            action = "unknown"
            note = "delete_requires_confirmation"
        else:
            note = item.get("note")

        raw_frag = item.get("raw_input") or user_text

        ti_dict: Dict[str, Any] = {
            "action": action,
            "title": (item.get("title") or None),
            "deadline_iso": _normalize_deadline_iso(item.get("deadline_iso")),
            "target_task_hint": _sanitize_target_hint(raw_frag, item.get("target_task_hint")),
            "note": note,
            "language": item.get("language"),
            "raw_input": raw_frag,
        }

        # Дополнительный матч title по снапшоту только для целевых действий (без изменения смысла)
        if action in {"complete", "reschedule", "delete", "rename"} and not ti_dict["title"] and ti_dict["target_task_hint"]:
            matched_title = _match_task_title_from_snapshot(tasks_snapshot, ti_dict["target_task_hint"])
            if matched_title:
                ti_dict["title"] = matched_title

        try:
            ti = TaskInterpretation.model_validate(ti_dict)
            if ti.action == "create" and not ti.title:
                continue
            results.append(ti)
        except Exception:
            logger.exception("Failed to validate multi item: %r", ti_dict)
            continue

    return results


def parse_user_input(
    user_text: str,
    tasks_snapshot: Optional[List[Tuple[int, str, Optional[str]]]] = None,
) -> TaskInterpretation:
    """
    Single-режим: модель — источник истины, мы только нормализуем формат и проверяем бизнес-ограничения.
    """
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

    data["action"] = _validate_action(data.get("action", "unknown"))
    data["deadline_iso"] = _normalize_deadline_iso(data.get("deadline_iso"))
    data["target_task_hint"] = _sanitize_target_hint(user_text, data.get("target_task_hint"))

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

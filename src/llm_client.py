# src/llm_client.py

import json
import logging
import re
import difflib
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime, timedelta
from openai import OpenAI

from config import OPENAI_API_KEY, OPENAI_MODEL, FIXED_TZ_OFFSET
from prompts import (
    get_multi_parser_system_prompt,
    get_parser_system_prompt,
    get_reply_system_prompt,
)
from task_schema import TaskInterpretation
from time_utils import FIXED_TZ, now_local, normalize_deadline_iso, parse_deadline_iso

client = OpenAI(api_key=OPENAI_API_KEY)
LOCAL_TZ = FIXED_TZ

# Хелперы для времени суток и дат
MORNING_WORDS = {"утром", "утро", "morning"}
DAY_WORDS = {"днём", "днем", "день", "daytime", "afternoon", "day"}
EVENING_WORDS = {"вечером", "вечер", "evening"}
NIGHT_WORDS = {"ночью", "ночь", "night"}

TODAY_WORDS = {"сегодня", "today"}
TOMORROW_WORDS = {"завтра", "tomorrow"}
DAY_AFTER_TOMORROW_WORDS = {"послезавтра"}

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
                due_norm = normalize_deadline_iso(due) or due
                dt = datetime.fromisoformat(due_norm)
                d_str = dt.strftime("%Y-%m-%d %H:%M")
                lines.append(f"{i}. {text} (дедлайн: {d_str})")
            except Exception:
                lines.append(f"{i}. {text}")
        else:
            lines.append(f"{i}. {text}")

    return "Актуальные задачи пользователя:\n" + "\n".join(lines)


def build_system_prompt(now_str: str, tasks_block: str) -> str:
    return get_parser_system_prompt(now_str, FIXED_TZ_OFFSET, tasks_block)


def build_system_prompt_multi(now_str: str, tasks_block: str, max_items: int) -> str:
    return get_multi_parser_system_prompt(now_str, FIXED_TZ_OFFSET, tasks_block, max_items)


def _guess_deadline_from_text(raw_text: str) -> Optional[datetime]:
    """
    Грубый fallback, если модель не вернула валидный ISO,
    но в тексте явно есть день/время.
    """
    if not raw_text:
        return None

    t_lower = raw_text.lower()
    now = now_local()

    # --- относительное "через N ..." / "in N ..." ---
    # Примеры: "через 2 дня", "через час", "через 15 минут", "in 2 hours"
    rel = re.search(
        r"(?:\bчерез\b|\bin\b|\bafter\b)\s*(?:(\d+)\s*)?(минут(?:у|ы)?|минут|час(?:а|ов)?|дн(?:я|ей)|day(?:s)?|hour(?:s)?|minute(?:s)?)\b",
        t_lower,
    )
    if rel:
        num_s = rel.group(1)
        unit = (rel.group(2) or "").strip().lower()
        n = int(num_s) if num_s and num_s.isdigit() else 1
        if "мин" in unit or "minute" in unit:
            return now + timedelta(minutes=n)
        if "час" in unit or "hour" in unit:
            return now + timedelta(hours=n)
        if "дн" in unit or "day" in unit:
            return now + timedelta(days=n)

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
    weekday_idx: int | None = None
    weekday_is_mentioned = False
    for word, idx in WEEKDAY_RU.items():
        if word in t_lower:
            weekday_idx = idx
            weekday_is_mentioned = True
            break
    if target_date is None:
        for word, idx in WEEKDAY_EN.items():
            if word in t_lower:
                weekday_idx = idx
                weekday_is_mentioned = True
                break
    if weekday_idx is not None:
        delta = (weekday_idx - now.weekday() + 7) % 7
        # Правило: если сегодня тот же weekday —
        # - если время ещё впереди, используем сегодня
        # - если прошло, +7 дней
        h = hour if hour is not None else (part_of_day_hour if part_of_day_hour is not None else 23)
        mi = minute if hour is not None else (0 if part_of_day_hour is not None else 59)
        candidate_today = datetime(now.year, now.month, now.day, h, mi, tzinfo=LOCAL_TZ)
        if delta == 0 and candidate_today > now:
            target_date = now.date()
        else:
            delta = delta or 7
            target_date = now.date() + timedelta(days=delta)
    if target_date is None:
        if any(w in t_lower for w in TODAY_WORDS):
            target_date = now.date()
        elif any(w in t_lower for w in TOMORROW_WORDS):
            target_date = (now + timedelta(days=1)).date()
        elif any(w in t_lower for w in DAY_AFTER_TOMORROW_WORDS):
            target_date = (now + timedelta(days=2)).date()

    if target_date is None and hour is not None:
        target_date = now.date()

    if target_date is None:
        return None

    if hour is None:
        hour = part_of_day_hour if part_of_day_hour is not None else 23
        minute = 59 if part_of_day_hour is None else 0

    dt = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        hour,
        minute,
        tzinfo=LOCAL_TZ,
    )

    # Если формулировка относительная (сегодня / только время) и вышло в прошлом — сдвигаем в ближайшее будущее.
    if dt <= now and not weekday_is_mentioned and (
        any(w in t_lower for w in TODAY_WORDS)
        or (hour is not None and target_date == now.date() and not any(w in t_lower for w in TOMORROW_WORDS | DAY_AFTER_TOMORROW_WORDS))
    ):
        dt = dt + timedelta(days=1)

    return dt


def _normalize_deadline_iso(raw_value: Any) -> str | None:
    """
    Backward-совместимый враппер над центральной normalize_deadline_iso().
    """
    if raw_value is None:
        return None
    if isinstance(raw_value, datetime):
        return normalize_deadline_iso(raw_value.isoformat())
    if not isinstance(raw_value, str):
        return None
    norm = normalize_deadline_iso(raw_value)
    if norm is None:
        logger.warning("deadline_iso is not valid ISO: %r", raw_value)
    return norm


_DEADLINE_WORDS = ("дедлайн", "deadline", "срок", "due")
_CLEAR_DEADLINE_PATTERNS = (
    "убери дедлайн",
    "сними срок",
    "без срока",
    "убери срок",
    "пусть будет без дедлайна",
    "без дедлайна",
)
_ADD_DEADLINE_PATTERNS = (
    "добавь дедлайн",
    "поставь дедлайн",
    "добавь срок",
    "поставь срок",
)
_DELETE_ALLOWED_PATTERNS = (
    r"\bудали(?:ть)?\s+задачу\b",
    r"\bубери\s+задачу\b",
)
_RENAME_WORDS = ("переимен", "измени название", "rename task", "rename")
_COMPLETE_WORDS = (
    "сделал",
    "сделала",
    "готово",
    "закрыл",
    "закрыла",
    "выполнил",
    "выполнила",
    "отметь выполненной",
    "отметь выполненным",
    "done",
    "completed",
    "finished",
)


def _is_strict_date_phrase(text: str) -> bool:
    lower = (text or "").lower()
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", lower):
        return True
    if re.search(r"\b\d{1,2}\.\d{1,2}(\.\d{2,4})?\b", lower):
        return True
    if re.search(
        r"\b\d{1,2}\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\b",
        lower,
    ):
        return True
    return False


def _looks_relative_time_phrase(text: str) -> bool:
    lower = (text or "").lower()
    if "через" in lower or "in " in lower or "after " in lower:
        return True
    if any(w in lower for w in ("сегодня", "завтра", "послезавтра")):
        return True
    if any(w in lower for w in (list(WEEKDAY_RU.keys()) + list(WEEKDAY_EN.keys()))):
        return True
    if any(w in lower for w in ("утром", "днем", "днём", "вечером", "ночью")):
        return True
    # "в 18:00" / "at 18:00"
    if re.search(r"\b\d{1,2}:\d{2}\b", lower):
        return True
    return False


def _ensure_deadline_not_in_past(deadline_iso: str | None, raw_text: str, now: datetime) -> tuple[str | None, list[str]]:
    """
    Правило "никогда не уходить в прошлое" для относительных формулировок.
    Для строгих дат — не меняем, но возвращаем диагностический маркер.
    """
    fired: list[str] = []
    dt = parse_deadline_iso(deadline_iso)
    if not dt:
        return deadline_iso, fired
    if dt >= now:
        return deadline_iso, fired

    if _is_strict_date_phrase(raw_text):
        fired.append("deadline_in_past_strict")
        return deadline_iso, fired

    if not _looks_relative_time_phrase(raw_text):
        fired.append("deadline_in_past_unknown_phrase")
        return deadline_iso, fired

    guessed = _guess_deadline_from_text(raw_text)
    if guessed and guessed > now:
        fired.append("deadline_shifted_by_guess")
        return normalize_deadline_iso(guessed.isoformat()), fired

    bumped = dt
    for _ in range(0, 14):
        if bumped > now:
            break
        bumped = bumped + timedelta(days=1)
    fired.append("deadline_bumped_to_future")
    return normalize_deadline_iso(bumped.isoformat()), fired


def _apply_phrase_validator(raw_text: str, data: Dict[str, Any], now: datetime) -> tuple[Dict[str, Any], list[str]]:
    """
    Жёсткая валидация поверх сырого ответа LLM.
    Возвращает (data, fired_rules).
    """
    fired: list[str] = []
    lower = (raw_text or "").lower()

    has_deadline_words = any(w in lower for w in _DEADLINE_WORDS)
    has_clear_deadline = any(p in lower for p in _CLEAR_DEADLINE_PATTERNS)
    has_add_deadline = any(p in lower for p in _ADD_DEADLINE_PATTERNS)

    # 3.1 Clear-deadline phrases
    if has_clear_deadline:
        if data.get("action") == "delete":
            fired.append("clear_deadline_blocks_delete")
        data["action"] = "clear_deadline"
        data["deadline_iso"] = None
        data["title"] = None

    # 3.3 Add-deadline phrases
    if has_add_deadline and not has_clear_deadline:
        data["action"] = "add_deadline"
        if not data.get("deadline_iso"):
            guessed = _guess_deadline_from_text(raw_text)
            if guessed:
                data["deadline_iso"] = normalize_deadline_iso(guessed.isoformat())
                fired.append("add_deadline_guessed_deadline")
        if not data.get("deadline_iso"):
            data["action"] = "needs_clarification"
            data["note"] = "needs_deadline"
            fired.append("add_deadline_missing_deadline")

    # 3.4 Rename phrases
    if any(w in lower for w in _RENAME_WORDS) and data.get("action") not in {"create"}:
        if data.get("action") != "rename":
            fired.append("rename_forced")
        data["action"] = "rename"
        data["deadline_iso"] = None
        if not data.get("title"):
            data["action"] = "needs_clarification"
            data["note"] = "rename_needs_title"
            fired.append("rename_missing_title")

    # 3.5 Complete phrases
    if any(w in lower for w in _COMPLETE_WORDS) and not has_clear_deadline and not has_add_deadline:
        if data.get("action") != "complete":
            fired.append("complete_forced")
        data["action"] = "complete"
        data["deadline_iso"] = None

    # 3.2 Delete phrases (последним, чтобы не перебивать clear/add)
    if data.get("action") == "delete":
        allowed = any(re.search(pat, lower) for pat in _DELETE_ALLOWED_PATTERNS)
        if (not allowed) or has_deadline_words:
            data["action"] = "unknown"
            fired.append("delete_blocked_by_phrase")

    return data, fired


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
    allowed = {
        "create",
        "complete",
        "reschedule",
        "add_deadline",
        "clear_deadline",
        "delete",
        "rename",
        "show_active",
        "show_today",
        "show_tomorrow",
        "show_date",
        "needs_clarification",
        "unknown",
    }
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
    now = now_local()
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

        # Phrase validator (жёсткие правила поверх ответа LLM)
        ti_dict, fired_rules = _apply_phrase_validator(raw_frag, ti_dict, now)

        # Если дедлайн есть — нормализуем "не в прошлое" для относительных формулировок
        deadline_rules: list[str] = []
        if ti_dict.get("deadline_iso"):
            ti_dict["deadline_iso"], deadline_rules = _ensure_deadline_not_in_past(
                ti_dict.get("deadline_iso"),
                raw_frag,
                now,
            )

        # Если действие требует дедлайна, а его нет — просим уточнение
        if ti_dict.get("action") in {"reschedule", "add_deadline"} and not ti_dict.get("deadline_iso"):
            ti_dict["action"] = "needs_clarification"
            ti_dict["note"] = ti_dict.get("note") or "needs_deadline"
            fired_rules.append("missing_deadline_for_action")

        if fired_rules or deadline_rules:
            logger.info(
                "validator_diag %s",
                json.dumps(
                    {
                        "mode": "multi",
                        "raw_input": raw_frag,
                        "fired_rules": fired_rules,
                        "deadline_rules": deadline_rules,
                        "action_after": ti_dict.get("action"),
                        "deadline_iso_after": ti_dict.get("deadline_iso"),
                    },
                    ensure_ascii=False,
                ),
            )

        # Дополнительный матч title по снапшоту только для целевых действий (без изменения смысла)
        # ВАЖНО: НЕ делаем это для rename, чтобы не подставить старое название в title.
        if ti_dict.get("action") in {"complete", "reschedule", "add_deadline", "clear_deadline", "delete"} and not ti_dict["title"] and ti_dict["target_task_hint"]:
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
    now = now_local()
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

    # Phrase validator (жёсткие правила поверх ответа LLM)
    data, fired_rules = _apply_phrase_validator(user_text, data, now)

    # Если дедлайн есть — нормализуем "не в прошлое" для относительных формулировок
    deadline_rules: list[str] = []
    if data.get("deadline_iso"):
        data["deadline_iso"], deadline_rules = _ensure_deadline_not_in_past(
            data.get("deadline_iso"),
            user_text,
            now,
        )

    # Если действие требует дедлайна, а его нет — просим уточнение
    if data.get("action") in {"reschedule", "add_deadline"} and not data.get("deadline_iso"):
        data["action"] = "needs_clarification"
        data["note"] = data.get("note") or "needs_deadline"
        fired_rules.append("missing_deadline_for_action")

    # Если про выходные — показываем ближайшие субботу и воскресенье как два дня (через note)
    if data.get("action") in {"show_active", "show_today", "show_tomorrow", "show_date"}:
        lower = user_text.lower()
        if "выходных" in lower or "выходные" in lower or "weekend" in lower:
            data["action"] = "show_date"
            data["note"] = "weekend"
            data["deadline_iso"] = None

    if fired_rules or deadline_rules:
        logger.info(
            "validator_diag %s",
            json.dumps(
                {
                    "mode": "single",
                    "raw_input": user_text,
                    "fired_rules": fired_rules,
                    "deadline_rules": deadline_rules,
                    "action_after": data.get("action"),
                    "deadline_iso_after": data.get("deadline_iso"),
                },
                ensure_ascii=False,
            ),
        )

    return TaskInterpretation.model_validate(data)


def build_reply_prompt() -> str:
    return get_reply_system_prompt()


def _format_deadline_human(deadline_iso: Optional[str]) -> Optional[str]:
    """
    Превращает ISO-дедлайн в строку "дд.мм HH:MM" в локальной таймзоне.
    """
    if not deadline_iso:
        return None
    try:
        norm = normalize_deadline_iso(deadline_iso) or deadline_iso
        dt = datetime.fromisoformat(norm)
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

# src/llm_client.py

import json
import logging
import re
import difflib
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime, timedelta
from openai import OpenAI

from config import OPENAI_API_KEY, OPENAI_MODEL, DEFAULT_TIMEZONE
from prompts import (
    get_multi_parser_system_prompt,
    get_parser_system_prompt,
    get_reply_system_prompt,
)
from task_schema import TaskInterpretation
from time_utils import (
    DEFAULT_TIMEZONE as TIME_DEFAULT_TZ,
    now_in_tz,
    now_utc,
    normalize_deadline_to_utc,
    parse_deadline_iso,
    get_tz_offset_str,
    format_deadline_in_tz,
)

client = OpenAI(api_key=OPENAI_API_KEY)


logger = logging.getLogger(__name__)

# Максимум элементов в multi-ответе, которые готовы обработать в одном запросе
# Максимум элементов в multi-ответе (увеличено для голосовых списков)
MAX_ITEMS_PER_REQUEST = 50
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
                # due is already in UTC format, parse directly
                s = due.strip()
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                dt = datetime.fromisoformat(s)
                d_str = dt.strftime("%Y-%m-%d %H:%M")
                lines.append(f"{i}. {text} (дедлайн: {d_str})")
            except Exception:
                lines.append(f"{i}. {text}")
        else:
            lines.append(f"{i}. {text}")

    return "Актуальные задачи пользователя:\n" + "\n".join(lines)


def build_system_prompt(now_str: str, user_timezone: str, tasks_block: str) -> str:
    tz_offset = get_tz_offset_str(user_timezone)
    return get_parser_system_prompt(now_str, user_timezone, tz_offset, tasks_block)


def build_system_prompt_multi(now_str: str, user_timezone: str, tasks_block: str, max_items: int) -> str:
    tz_offset = get_tz_offset_str(user_timezone)
    return get_multi_parser_system_prompt(now_str, user_timezone, tz_offset, tasks_block, max_items)





def _normalize_deadline_to_utc(raw_value: Any, user_timezone: str) -> str | None:
    """
    Converts LLM-returned deadline to UTC for storage.
    
    Args:
        raw_value: Raw deadline_iso from LLM (in user's local timezone)
        user_timezone: User's IANA timezone string
    
    Returns:
        UTC ISO string (with Z suffix) or None
    """
    if raw_value is None:
        return None
    if isinstance(raw_value, datetime):
        raw_value = raw_value.isoformat()
    if not isinstance(raw_value, str):
        return None
    norm = normalize_deadline_to_utc(raw_value, user_timezone)
    if norm is None:
        logger.warning("deadline_iso is not valid ISO: %r", raw_value)
    return norm


# Legacy function for backward compatibility
def _normalize_deadline_iso(raw_value: Any) -> str | None:
    """DEPRECATED: Use _normalize_deadline_to_utc instead."""
    return _normalize_deadline_to_utc(raw_value, DEFAULT_TIMEZONE)


_DELETE_ALLOWED_PATTERNS = (
    r"\bудали(?:ть)?\s+задачу\b",
    r"\bубери\s+задачу\b",
)





def _apply_phrase_validator(raw_text: str, data: Dict[str, Any], now: datetime) -> tuple[Dict[str, Any], list[str]]:
    """
    Минимальная защита от деструктивных действий.
    Оставляем только проверку на delete, чтобы не удалить задачу случайно.
    """
    fired: list[str] = []
    lower = (raw_text or "").lower()

    if data.get("action") == "delete":
        # Проверяем, есть ли явные слова удаления в тексте
        allowed = any(re.search(pat, lower) for pat in _DELETE_ALLOWED_PATTERNS)
        if not allowed:
            # Если модель решила "удалить", но слов удаления нет — считаем это ошибкой модели
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
    user_timezone: str = DEFAULT_TIMEZONE,
) -> List[TaskInterpretation]:
    """
    Тонкий адаптер над multi-ответом модели. Модель — источник истины,
    здесь только нормализация формата и бизнес-ограничения.
    
    Args:
        user_text: User message text
        tasks_snapshot: Current active tasks
        max_items: Max items to parse
        user_timezone: User's IANA timezone for date interpretation
    """
    now = now_in_tz(user_timezone)
    now_str = now.isoformat()

    tasks_block = _format_tasks_for_prompt(tasks_snapshot)
    system_prompt = build_system_prompt_multi(now_str, user_timezone, tasks_block, max_items)

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
    logger.info("LLM Raw Multi Response: %s", raw)
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
            "deadline_iso": _normalize_deadline_to_utc(item.get("deadline_iso"), user_timezone),
            "target_task_hint": _sanitize_target_hint(raw_frag, item.get("target_task_hint")),
            "note": note,
            "language": item.get("language"),
            "raw_input": raw_frag,
        }

        # Phrase validator (минимальная защита)
        ti_dict, fired_rules = _apply_phrase_validator(raw_frag, ti_dict, now)

        # Если действие требует дедлайна, а его нет — просим уточнение
        if ti_dict.get("action") in {"reschedule", "add_deadline"} and not ti_dict.get("deadline_iso"):
            ti_dict["action"] = "needs_clarification"
            ti_dict["note"] = ti_dict.get("note") or "needs_deadline"
            fired_rules.append("missing_deadline_for_action")

        if fired_rules:
            logger.info(
                "validator_diag %s",
                json.dumps(
                    {
                        "mode": "multi",
                        "raw_input": raw_frag,
                        "fired_rules": fired_rules,
                        "action_after": ti_dict.get("action"),
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
    user_timezone: str = DEFAULT_TIMEZONE,
) -> TaskInterpretation:
    """
    Single-режим: модель — источник истины, мы только нормализуем формат и проверяем бизнес-ограничения.
    
    Args:
        user_text: User message text
        tasks_snapshot: Current active tasks
        user_timezone: User's IANA timezone for date interpretation
    """
    now = now_in_tz(user_timezone)
    now_str = now.isoformat()

    tasks_block = _format_tasks_for_prompt(tasks_snapshot)
    system_prompt = build_system_prompt(now_str, user_timezone, tasks_block)

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
    logger.info("LLM Raw Single Response: %s", raw)
    data: Dict[str, Any] = json.loads(raw)

    if "raw_input" not in data or not data["raw_input"]:
        data["raw_input"] = user_text

    data["action"] = _validate_action(data.get("action", "unknown"))
    data["deadline_iso"] = _normalize_deadline_to_utc(data.get("deadline_iso"), user_timezone)
    data["target_task_hint"] = _sanitize_target_hint(user_text, data.get("target_task_hint"))

    # Phrase validator (минимальная защита)
    data, fired_rules = _apply_phrase_validator(user_text, data, now)
    
    # Если дедлайн есть, но в прошлом — доверяем модели, но можем залогировать
    if data.get("deadline_iso"):
        dt = parse_deadline_iso(data.get("deadline_iso"))
        if dt and dt < now:
             logger.warning("LLM returned past deadline for %r", user_text)

    # Если действие требует дедлайна, а его нет — просим уточнение
    if data.get("action") in {"reschedule", "add_deadline"} and not data.get("deadline_iso"):
        data["action"] = "needs_clarification"
        data["note"] = data.get("note") or "needs_deadline"
        fired_rules.append("missing_deadline_for_action")

    if fired_rules:
        logger.info(
            "validator_diag %s",
            json.dumps(
                {
                    "mode": "single",
                    "raw_input": user_text,
                    "fired_rules": fired_rules,
                    "action_after": data.get("action"),
                },
                ensure_ascii=False,
            ),
        )

    return TaskInterpretation.model_validate(data)


def build_reply_prompt() -> str:
    return get_reply_system_prompt()


def _format_deadline_human(deadline_iso: Optional[str]) -> Optional[str]:
    """
    Превращает ISO-дедлайн в строку "дд.мм HH:MM".
    Assumes deadline_iso is in UTC format.
    """
    if not deadline_iso:
        return None
    try:
        # Parse UTC deadline directly
        s = deadline_iso.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
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

# src/bot/utils.py
"""Pure utility functions for the Telegram bot.

These functions should NOT import Application, Update, or handlers.
They operate on data and return results.
"""

import json
import logging
import re
from datetime import date
from typing import Optional

import db
from bot.constants import (
    GREETING_WORDS,
    MONTHS_RU,
    STOP_WORDS,
    TASK_VERB_HINTS,
    TIME_HINT_WORDS,
)
from llm_client import render_user_reply
from task_matching import MatchResult, match_task_from_snapshot
from time_utils import now_local, parse_deadline_iso, format_deadline_in_tz, DEFAULT_TIMEZONE

logger = logging.getLogger(__name__)


# ==== SAFE WRAPPERS =====

def safe_render_user_reply(event: dict) -> str:
    """Безопасный обёртчик над render_user_reply, чтобы не падать из-за LLM."""
    try:
        return render_user_reply(event)
    except Exception as e:
        logger.exception("render_user_reply failed: %s", e)
        return "Операцию сделал, но не смог красиво сформулировать ответ 🙂"


# ==== TEXT NORMALIZATION =====

def normalize_ru_word(w: str) -> str:
    """
    Грубая нормализация русских слов:
    'английский', 'английском', 'английскому' → 'англи'
    """
    w = w.lower()
    return re.sub(
        r"(ому|ему|ого|ими|ыми|ами|лях|ях|ах|ам|ой|ый|ий|ая|ое|ые|ую|ом|ев|ов|ей|ами?)$",
        "",
        w,
    )


def tokenize_meaningful(text: str) -> list[str]:
    """Токенизирует текст, отбрасывая стоп-слова."""
    tokens = re.findall(r"\w+", text.lower())
    out = []
    for t in tokens:
        if t in STOP_WORDS:
            continue
        norm = normalize_ru_word(t)
        if norm:
            out.append(norm)
    return out


# ==== TEXT DETECTION =====

def is_greeting_only(text: str) -> bool:
    """
    Определяет, является ли сообщение коротким приветствием без содержательной части.
    Простая эвристика: все слова должны быть из списка приветствий.
    """
    tokens = re.findall(r"\w+", text.lower())
    if not tokens:
        return False

    # если в тексте есть явные маркеры времени/действий — не считаем приветствием
    if any(t in TIME_HINT_WORDS for t in tokens) or any(t in TASK_VERB_HINTS for t in tokens):
        return False

    return all(tok in GREETING_WORDS for tok in tokens)


def is_deadline_like(text: str) -> bool:
    """
    Грубая эвристика: похоже ли сообщение на ответ с датой/временем,
    а не на новую задачу.
    """
    lower = text.lower()

    # если есть типичный глагол-задача → считаем, что это новая задача
    for v in TASK_VERB_HINTS:
        if v in lower:
            return False

    # есть ли маркеры времени/даты
    has_time_word = any(w in lower for w in TIME_HINT_WORDS)
    has_time_pattern = bool(re.search(r"\d{1,2}:\d{2}", lower))
    has_date_pattern = bool(re.search(r"\d{1,2}\.\d{1,2}(\.\d{2,4})?", lower))

    # хак: "в 9 вечера/утра/ночи" без двоеточия
    has_hour_with_part_of_day = bool(
        re.search(r"\b\d{1,2}\b", lower)
        and any(
            w in lower
            for w in [
                "вечер",
                "вечера",
                "вечером",
                "утро",
                "утра",
                "утром",
                "ночь",
                "ночи",
                "ночью",
            ]
        )
    )

    return has_time_word or has_time_pattern or has_date_pattern or has_hour_with_part_of_day


def detect_rename_intent(text: str) -> dict | None:
    """
    Пытается извлечь сигнал переименования задачи из фразы пользователя.
    Возвращает словарь {"old_hint": str | None, "new_title": str} или None.
    """
    raw = text.strip()
    lower = raw.lower()

    patterns = [
        # "вместо X задача называлась Y"
        r"вместо\s+\"?(.+?)\"?\s+.*?наз\w+\s+\"?(.+?)\"?$",
        # "переименуй X в/на Y"
        r"переимен\w*\s+(?:задачу\s+)?\"?(.+?)\"?\s+(?:в|на)\s+\"?(.+?)\"?$",
        # "измени/исправь X на Y"
        r"(?:измени|изменить|исправь)\s+(?:задачу\s+)?\"?(.+?)\"?\s+на\s+\"?(.+?)\"?$",
        # "поменяй X на Y" / "поменяем X на Y"
        r"(?:задачу\s+)?\"?(.+?)\"?\s+(?:давай\s+)?поменя\w*\s+на\s+\"?(.+?)\"?$",
    ]

    for pat in patterns:
        m = re.search(pat, lower, flags=re.IGNORECASE)
        if m and len(m.groups()) >= 2:
            old_hint = m.group(1).strip(" «»\"'""„")
            new_title = m.group(2).strip(" «»\"'""„")
            if new_title:
                return {"old_hint": old_hint or None, "new_title": new_title}

    # fallback: "поменяем на Y" — без старого названия, используем target_task_hint позже
    m = re.search(r"поменя\w*\s+(?:.*?\s+)?на\s+\"?(.+?)\"?$", lower, flags=re.IGNORECASE)
    if m:
        new_title = m.group(1).strip(" «»\"'""„")
        if new_title:
            # Попробуем вытащить старый хинт как всё до слова "помен"
            idx = lower.find("помен")
            old_part = lower[:idx].strip(" «»\"'""„")
            old_hint = old_part if old_part else None
            return {"old_hint": old_hint, "new_title": new_title}

    return None


# ==== DATE PARSING =====

def parse_explicit_date(text: str) -> date | None:
    """
    Пытается вытащить дату вида "9 декабря" из текста.
    Возвращает date или None.
    """
    lower = text.lower()
    m = re.search(
        r"\b(\d{1,2})\s+("
        r"января|февраля|марта|апреля|мая|июня|"
        r"июля|августа|сентября|октября|ноября|декабря"
        r")\b",
        lower,
    )
    if not m:
        return None

    day_str, month_word = m.groups()
    day = int(day_str)
    month = MONTHS_RU[month_word]

    now = now_local()
    year = now.year

    try:
        dt = date(year, month, day)
    except ValueError:
        return None

    # Если дата уже прошла в этом году — считаем, что речь о следующем
    if dt < now.date():
        try:
            dt = date(year + 1, month, day)
        except ValueError:
            return None

    return dt


# ==== FORMATTING =====

def format_deadline_human_local(deadline_iso: Optional[str], user_timezone: str = DEFAULT_TIMEZONE) -> Optional[str]:
    """Format deadline in user's timezone for display.
    
    Args:
        deadline_iso: ISO deadline string (can be UTC or any timezone)
        user_timezone: User's IANA timezone for display
    
    Returns:
        Formatted string like "30.12 15:00" in user's timezone
    """
    if not deadline_iso:
        return None
    # Use new timezone-aware formatting
    result = format_deadline_in_tz(deadline_iso, user_timezone)
    if result:
        return result
    # Fallback to legacy parsing
    try:
        dt = parse_deadline_iso(deadline_iso)
        if not dt:
            return None
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return None


# ==== TASK MATCHING HELPERS =====

def render_clarification_message(mr: MatchResult) -> str:
    """Формирует сообщение-уточнение когда задача не найдена."""
    base = "Я не уверен, какую задачу ты имел в виду. Напиши, пожалуйста, полное название задачи целиком, как оно есть в списке."
    if mr.top:
        opts = "\n".join([f"- {c.task_text}" for c in mr.top[:3]])
        return base + "\n\nВозможные варианты:\n" + opts
    return base


def match_task_or_none(
    tasks_snapshot,
    *,
    target_task_hint: str | None,
    raw_input: str,
    action: str,
) -> tuple[tuple[int, str] | None, MatchResult]:
    """Ищет задачу по хинту и логирует результат."""
    mr = match_task_from_snapshot(tasks_snapshot, target_task_hint, raw_input)
    logger.info(
        "task_match %s",
        json.dumps(
            {
                "action": action,
                "hint": target_task_hint,
                "raw_input": raw_input,
                "reason": mr.reason,
                "threshold": mr.threshold,
                "top": [{"task_id": c.task_id, "score": c.score, "text": c.task_text} for c in mr.top],
                "matched": {
                    "task_id": mr.matched.task_id,
                    "score": mr.matched.score,
                    "text": mr.matched.task_text,
                }
                if mr.matched
                else None,
            },
            ensure_ascii=False,
        ),
    )
    if mr.matched:
        return (mr.matched.task_id, mr.matched.task_text), mr
    return None, mr


async def find_task_by_hint(user_id: int, hint: str):
    """
    Пытается найти задачу по текстовой подсказке.
    Сначала точное вхождение, потом осторожный fuzzy с высоким порогом.
    """
    if not hint:
        return None
    tasks = await db.get_tasks(user_id)
    mr = match_task_from_snapshot(tasks, hint, raw_input=hint)
    if mr.matched:
        return (mr.matched.task_id, mr.matched.task_text)
    return None


async def filter_tasks_by_date(user_id: int, target_date, user_timezone: str = "Asia/Almaty") -> list[tuple[int, str, str | None]]:
    """
    Возвращает задачи, дедлайн которых совпадает с датой target_date (в локальной TZ пользователя).
    """
    tasks = await db.get_tasks(user_id)
    result = []
    
    # We need to import utc_to_local inside function or at top level. 
    # Since imports are usually at top, let's assume we update imports too.
    # But for now I'll use simple import here to avoid messing up file just for import
    from time_utils import utc_to_local, parse_deadline_iso
    
    for t_id, text, due in tasks:
        if not due:
            continue
        try:
            # Parse stored deadline (UTC)
            dt = parse_deadline_iso(due)
            if not dt:
                continue
            
            # Convert to user timezone
            local_dt = utc_to_local(dt, user_timezone)
            
            if local_dt.date() == target_date:
                result.append((t_id, text, due))
        except Exception:
            continue
    return result


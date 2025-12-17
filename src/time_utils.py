# src/time_utils.py
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

# ЕДИНАЯ ТАЙМЗОНА ДЛЯ ВСЕХ ПОЛЬЗОВАТЕЛЕЙ (фиксированный offset)
FIXED_TZ = timezone(timedelta(hours=5), name="+05:00")


def now_local() -> datetime:
    """Текущее время в фиксированной TZ (+05:00)."""
    return datetime.now(FIXED_TZ)


def now_local_iso() -> str:
    return now_local().isoformat()


def _has_explicit_time_part(s: str) -> bool:
    # ISO "YYYY-MM-DD" => без времени.
    # Любое наличие "T" или пробела после даты => время присутствует.
    return ("T" in s) or (re.search(r"\d{4}-\d{2}-\d{2}\s+\d", s) is not None)


def normalize_deadline_iso(deadline_iso: str | None) -> str | None:
    """
    Центральная нормализация дедлайна:
    - Парсит ISO (поддерживает суффикс Z).
    - Нормализует/принудительно выставляет фиксированный offset +05:00.
    - ВАЖНО ДЛЯ UX: сохраняем ЛОКАЛЬНОЕ время как намерение пользователя:
      если вход был +06:00 (или любой другой offset) — мы НЕ делаем сдвиг часов,
      а просто заменяем offset на +05:00.
    - Если времени не было (только дата) — ставим 23:59.
    """
    if deadline_iso is None:
        return None
    if not isinstance(deadline_iso, str):
        return None

    s = deadline_iso.strip()
    if not s:
        return None

    # Поддержка "Z" (UTC)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None

    # Если не было времени (только дата) — 23:59
    if not _has_explicit_time_part(s):
        dt = dt.replace(hour=23, minute=59, second=0, microsecond=0)

    # Если TZ нет — считаем локальной (+05:00).
    # Если TZ есть, но она не +05 — для UX трактуем как локальное время пользователя
    # и просто заменяем offset без сдвига часов.
    dt = dt.replace(tzinfo=FIXED_TZ)
    return dt.isoformat()


def parse_deadline_iso(deadline_iso: str | None) -> Optional[datetime]:
    """Парсит дедлайн после нормализации (удобно для сравнения/планирования)."""
    norm = normalize_deadline_iso(deadline_iso)
    if not norm:
        return None
    try:
        return datetime.fromisoformat(norm)
    except Exception:
        return None


_RU_MINUTE_WORDS = ("минута", "минуту", "минуты", "минут", "мин")
_RU_HOUR_WORDS = ("час", "часа", "часов", "ч")


def parse_offset_minutes(text: str) -> Optional[int]:
    """
    Парсит фразы вида "за 5 минут", "за полчаса", "за час".
    Возвращает количество минут или None.
    """
    if not text:
        return None
    lower = text.lower()

    if "полчас" in lower or "пол часа" in lower:
        m = re.search(r"\bза\s+пол\s*час", lower)
        if m:
            return 30

    if re.search(r"\bза\s+час\b", lower):
        return 60

    m = re.search(r"\bза\s+(\d{1,3})\s*(мин(?:ут[ауы]?|ут)?|м)\b", lower)
    if m:
        return int(m.group(1))

    m = re.search(r"\bза\s+(\d{1,3})\s*(час(?:а|ов)?|ч)\b", lower)
    if m:
        return int(m.group(1)) * 60

    # "5 минут" (без "за") — тоже иногда пишут, разрешим
    m = re.search(r"\b(\d{1,3})\s*(мин(?:ут[ауы]?|ут)?|м)\b", lower)
    if m:
        return int(m.group(1))

    m = re.search(r"\b(\d{1,3})\s*(час(?:а|ов)?|ч)\b", lower)
    if m:
        return int(m.group(1)) * 60

    return None


def parse_delay_minutes(text: str) -> Optional[int]:
    """
    Парсит фразы вида "через 5 минут", "через полчаса", "через час", "+30 мин".
    Возвращает количество минут или None.
    """
    if not text:
        return None
    lower = text.lower().strip()

    if "полчас" in lower or "пол часа" in lower:
        if re.search(r"\bчерез\s+пол\s*час", lower):
            return 30

    if re.search(r"\bчерез\s+час\b", lower):
        return 60

    m = re.search(r"\bчерез\s+(\d{1,3})\s*(мин(?:ут[ауы]?|ут)?|м)\b", lower)
    if m:
        return int(m.group(1))

    m = re.search(r"\bчерез\s+(\d{1,3})\s*(час(?:а|ов)?|ч)\b", lower)
    if m:
        return int(m.group(1)) * 60

    m = re.search(r"^\+\s*(\d{1,3})\s*(мин(?:ут[ауы]?|ут)?|м)\b", lower)
    if m:
        return int(m.group(1))

    m = re.search(r"^\+\s*(\d{1,3})\s*(час(?:а|ов)?|ч)\b", lower)
    if m:
        return int(m.group(1)) * 60

    return None


def parse_hhmm(text: str) -> Optional[tuple[int, int]]:
    """
    Парсит время в формате HH:MM (или H:MM).
    Возвращает (hour, minute) или None.
    """
    if not text:
        return None
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h, mi


def parse_ddmm(text: str) -> Optional[tuple[int, int, Optional[int]]]:
    """
    Парсит дату в формате DD.MM или DD.MM.YYYY.
    Возвращает (day, month, year|None) или None.
    """
    if not text:
        return None
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b", text)
    if not m:
        return None
    d = int(m.group(1))
    mo = int(m.group(2))
    y_raw = m.group(3)
    y: Optional[int] = None
    if y_raw:
        y = int(y_raw)
        if y < 100:
            y += 2000
    if not (1 <= d <= 31 and 1 <= mo <= 12):
        return None
    return d, mo, y


def parse_datetime_from_text(
    text: str,
    *,
    now: datetime,
    base_date: Optional[date] = None,
) -> Optional[datetime]:
    """
    Детерминированный разбор "точного времени" для UX:
    - "через N минут/час" → now + delta
    - "DD.MM HH:MM" (или с годом) → конкретная дата/время
    - "HH:MM" → на base_date (если задан) иначе сегодня; если получилось в прошлом — +1 день
    Возвращает datetime в TZ now.tzinfo или None.
    """
    if not text:
        return None

    delay_min = parse_delay_minutes(text)
    if delay_min is not None:
        return now + timedelta(minutes=delay_min)

    hhmm = parse_hhmm(text)
    ddmm = parse_ddmm(text)

    tz = now.tzinfo or FIXED_TZ

    # DD.MM(.YYYY) + HH:MM
    if ddmm and hhmm:
        day, month, year = ddmm
        h, mi = hhmm
        y = year or now.year
        try:
            dt = datetime(y, month, day, h, mi, tzinfo=tz)
        except ValueError:
            return None
        # если год не указан и дата уже прошла — считаем следующий год
        if year is None and dt < now:
            try:
                dt2 = datetime(now.year + 1, month, day, h, mi, tzinfo=tz)
                if dt2 > now:
                    return dt2
            except ValueError:
                return dt
        return dt

    # Только HH:MM
    if hhmm and not ddmm:
        h, mi = hhmm
        d = base_date or now.date()
        dt = datetime(d.year, d.month, d.day, h, mi, tzinfo=tz)
        if dt <= now:
            dt = dt + timedelta(days=1)
        return dt

    return None



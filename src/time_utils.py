# src/time_utils.py
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[import,no-redef]

# ======== LEGACY: Fixed offset for backward compatibility ========
# DEPRECATED: These are kept only for third-party code that might import them.
# Internal code now uses get_tz(DEFAULT_TIMEZONE) for dynamic timezone resolution.
FIXED_TZ = timezone(timedelta(hours=5), name="+05:00")  # DEPRECATED
LOCAL_TZ = FIXED_TZ  # DEPRECATED alias

# Default timezone for new users (IANA string)
DEFAULT_TIMEZONE = "Asia/Almaty"

# UTC timezone constant
UTC = timezone.utc


# ======== TIMEZONE-AWARE FUNCTIONS (NEW) ========

def get_tz(tz_name: str) -> ZoneInfo | timezone:
    """Get ZoneInfo for IANA timezone name, with fallback to default."""
    if not tz_name:
        tz_name = DEFAULT_TIMEZONE
    try:
        return ZoneInfo(tz_name)
    except Exception:
        # Fallback to default timezone
        try:
            return ZoneInfo(DEFAULT_TIMEZONE)
        except Exception:
            # Ultimate fallback to fixed offset
            return FIXED_TZ


def now_utc() -> datetime:
    """Current time in UTC."""
    return datetime.now(UTC)


def now_in_tz(tz_name: str) -> datetime:
    """Current time in specified timezone."""
    tz = get_tz(tz_name)
    return datetime.now(tz)


def local_to_utc(dt: datetime, tz_name: str) -> datetime:
    """Convert naive or local datetime to UTC.
    
    If dt is naive, assumes it's in the specified timezone.
    If dt has tzinfo, converts it to UTC.
    """
    tz = get_tz(tz_name)
    if dt.tzinfo is None:
        # Naive datetime - assume it's in user's timezone
        dt = dt.replace(tzinfo=tz)
    # Convert to UTC
    return dt.astimezone(UTC)


def utc_to_local(dt: datetime, tz_name: str) -> datetime:
    """Convert datetime to user's local timezone.
    
    If dt is naive, assumes it's UTC.
    """
    tz = get_tz(tz_name)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz)


def normalize_deadline_to_utc(deadline_iso: str | None, tz_name: str) -> str | None:
    """Parse ISO deadline string and convert to UTC.
    
    - Parses ISO string (supports 'Z' suffix)
    - If no time part, defaults to 23:59:00
    - Interprets datetime in user's timezone if no offset in string
    - Returns UTC ISO string
    """
    if deadline_iso is None:
        return None
    if not isinstance(deadline_iso, str):
        return None

    s = deadline_iso.strip()
    if not s:
        return None

    # Support "Z" (UTC)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None

    # If no time part (only date) - set to 23:59
    if not _has_explicit_time_part(s):
        dt = dt.replace(hour=23, minute=59, second=0, microsecond=0)

    # If datetime is naive, interpret as user's local timezone
    if dt.tzinfo is None:
        tz = get_tz(tz_name)
        dt = dt.replace(tzinfo=tz)

    # Convert to UTC and return ISO string
    utc_dt = dt.astimezone(UTC)
    return utc_dt.isoformat().replace("+00:00", "Z")


def format_deadline_in_tz(utc_iso: str | None, tz_name: str, fmt: str = "%d.%m %H:%M") -> str | None:
    """Format UTC deadline for display in user's timezone.
    
    Args:
        utc_iso: ISO string in UTC (with 'Z' suffix or +00:00)
        tz_name: User's IANA timezone name
        fmt: strftime format string
    
    Returns:
        Formatted string in user's timezone, or None if parsing fails.
    """
    if not utc_iso:
        return None
    
    s = utc_iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    
    # Convert to user's timezone
    local_dt = utc_to_local(dt, tz_name)
    return local_dt.strftime(fmt)


def get_tz_offset_str(tz_name: str) -> str:
    """Get current UTC offset string for timezone (e.g., '+05:00')."""
    tz = get_tz(tz_name)
    now = datetime.now(tz)
    offset = now.utcoffset()
    if offset is None:
        return "+00:00"
    total_seconds = int(offset.total_seconds())
    hours, remainder = divmod(abs(total_seconds), 3600)
    minutes = remainder // 60
    sign = "+" if total_seconds >= 0 else "-"
    return f"{sign}{hours:02d}:{minutes:02d}"


# ======== LEGACY FUNCTIONS (for backward compatibility) ========

def now_local() -> datetime:
    """Текущее время в дефолтной таймзоне.
    
    DEPRECATED: Use now_in_tz(tz_name) instead.
    """
    return datetime.now(get_tz(DEFAULT_TIMEZONE))


def now_local_iso() -> str:
    """DEPRECATED: Use now_utc().isoformat() instead."""
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

    # Если TZ нет — считаем локальной (default timezone).
    # Если TZ есть, но она другая — для UX трактуем как локальное время пользователя
    # и просто заменяем offset без сдвига часов.
    dt = dt.replace(tzinfo=get_tz(DEFAULT_TIMEZONE))
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

    tz = now.tzinfo or get_tz(DEFAULT_TIMEZONE)

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


def compute_remind_at_from_offset(due_iso: str, offset_min: int) -> str | None:
    """
    Считает remind_at = due_at - offset_min (в минутах).
    Если получилось в прошлом — напомним "почти сразу" (через ~10 секунд).
    Возвращает ISO в фиксированной TZ (+05:00) или None.
    """
    try:
        due_dt = parse_deadline_iso(due_iso)
        if not due_dt:
            return None
        now = now_local()
        remind_dt = due_dt - timedelta(minutes=max(int(offset_min), 0))
        if remind_dt <= now:
            remind_dt = now + timedelta(seconds=10)
        return normalize_deadline_iso(remind_dt.isoformat())
    except Exception:
        return None



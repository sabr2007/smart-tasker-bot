from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
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



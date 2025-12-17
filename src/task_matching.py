# src/task_matching.py
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple


TaskSnapshot = Tuple[int, str, Optional[str]]


def _normalize_text(s: str) -> str:
    s = (s or "").lower().strip()
    s = s.strip(" \t\r\n\"'«»“”„")
    s = re.sub(r"[^\w\s]+", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _extract_quoted_hint(raw_text: str) -> Optional[str]:
    """
    Если пользователь явно указал задачу в кавычках, берём это как более строгий hint.
    Поддерживаем: "..." и «...».
    """
    if not raw_text:
        return None
    m = re.findall(r"«([^»]{2,200})»", raw_text)
    if m:
        # берём самое длинное, чтобы уменьшить неоднозначность
        return max((x.strip() for x in m), key=len, default=None)
    m = re.findall(r"\"([^\"]{2,200})\"", raw_text)
    if m:
        return max((x.strip() for x in m), key=len, default=None)
    return None


def _tokenize(s: str) -> List[str]:
    s = _normalize_text(s)
    toks = re.findall(r"\w+", s, flags=re.UNICODE)
    return [t for t in toks if len(t) >= 2]


def _token_dice(a_tokens: Sequence[str], b_tokens: Sequence[str]) -> float:
    if not a_tokens or not b_tokens:
        return 0.0
    a = set(a_tokens)
    b = set(b_tokens)
    inter = len(a & b)
    return (2.0 * inter) / (len(a) + len(b))


def _similarity(a: str, b: str) -> float:
    na = _normalize_text(a)
    nb = _normalize_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    # базовая строковая близость
    s1 = difflib.SequenceMatcher(None, na, nb).ratio()
    # близость по токенам (устойчивее к перестановкам слов)
    s2 = _token_dice(_tokenize(na), _tokenize(nb))
    return max(s1, s2)


@dataclass(frozen=True)
class MatchCandidate:
    task_id: int
    task_text: str
    score: float


@dataclass(frozen=True)
class MatchResult:
    matched: Optional[MatchCandidate]
    top: List[MatchCandidate]
    threshold: float
    reason: str  # ok / low_score / ambiguous / empty_hint / no_tasks


def match_task_from_snapshot(
    tasks_snapshot: Optional[Iterable[TaskSnapshot]],
    target_task_hint: Optional[str],
    raw_input: str,
    *,
    threshold: float = 0.75,
    quoted_threshold: float = 0.85,
    ambiguity_delta: float = 0.05,
    top_k: int = 3,
) -> MatchResult:
    """
    Детерминированный матчинг target_task_hint к существующим задачам.
    Стратегия:
    - нормализация строк
    - exact match
    - contains match
    - fuzzy similarity (SequenceMatcher + token dice)
    Порог выше, если пользователь указал задачу в кавычках.
    """
    if not tasks_snapshot:
        return MatchResult(matched=None, top=[], threshold=threshold, reason="no_tasks")

    quoted = _extract_quoted_hint(raw_input)
    hint = quoted or target_task_hint or ""
    hint_norm = _normalize_text(hint)
    if not hint_norm:
        return MatchResult(matched=None, top=[], threshold=(quoted_threshold if quoted else threshold), reason="empty_hint")

    thr = quoted_threshold if quoted else threshold

    candidates: List[MatchCandidate] = []
    for task_id, task_text, _due in tasks_snapshot:
        task_norm = _normalize_text(task_text)
        if not task_norm:
            continue
        if hint_norm == task_norm:
            candidates.append(MatchCandidate(task_id=task_id, task_text=task_text, score=1.0))
            continue
        if hint_norm in task_norm or task_norm in hint_norm:
            # contains: почти наверняка, но не 1.0
            candidates.append(MatchCandidate(task_id=task_id, task_text=task_text, score=0.90))
            continue
        score = _similarity(hint_norm, task_norm)
        if score > 0:
            candidates.append(MatchCandidate(task_id=task_id, task_text=task_text, score=score))

    candidates.sort(key=lambda c: c.score, reverse=True)
    top = candidates[: max(top_k, 0)]

    if not top:
        return MatchResult(matched=None, top=[], threshold=thr, reason="low_score")

    best = top[0]
    second = top[1] if len(top) > 1 else None
    if best.score < thr:
        return MatchResult(matched=None, top=top, threshold=thr, reason="low_score")
    if second and (best.score - second.score) < ambiguity_delta:
        return MatchResult(matched=None, top=top, threshold=thr, reason="ambiguous")

    return MatchResult(matched=best, top=top, threshold=thr, reason="ok")



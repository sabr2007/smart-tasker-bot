# src/main.py
import asyncio
import logging
from typing import Optional
from datetime import datetime, time as dtime, timedelta, date
import difflib
import re
import os
import json

from telegram import (
    Update,
    Message,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CallbackQueryHandler,
    CommandHandler,
    filters,
    ContextTypes,
)

from config import TELEGRAM_BOT_TOKEN
from llm_client import (
    parse_user_input,
    parse_user_input_multi,
    render_user_reply,
    transcribe_audio,
)
from task_schema import TaskInterpretation
import db  
from time_utils import (
    FIXED_TZ,
    now_local,
    now_local_iso,
    normalize_deadline_iso,
    parse_deadline_iso,
    parse_offset_minutes,
    parse_delay_minutes,
    parse_datetime_from_text,
)
from task_matching import match_task_from_snapshot, MatchResult

# ===== КОНСТАНТЫ =====
ADMIN_USER_ID = 6113692933
LOCAL_TZ = FIXED_TZ

# Флаг: автоматом обрабатываем голос (True) или только показываем, что услышали (False)
ENABLE_VOICE_AUTO_HANDLE = True

# ==== КОНСТАНТЫ ДЛЯ УТОЧНЕНИЯ ДЕДЛАЙНА =====

NO_DEADLINE_PHRASES = {
    "нет",
    "не надо",
    "без дедлайна",
    "не нужен",
    "не нужно",
    "без срока",
}

NO_REMINDER_PHRASES = {
    "нет",
    "не надо",
    "не нужно",
    "без напоминания",
    "не напоминай",
    "не напоминать",
}

TIME_HINT_WORDS = [
    "сегодня",
    "завтра",
    "послезавтра",
    "понедельник",
    "вторник",
    "среду",
    "среда",
    "четверг",
    "пятницу",
    "пятница",
    "субботу",
    "суббота",
    "воскресенье",
    "через",
    "минут",
    "минуту",
    "час",
    "часа",
    "вечером",
    "вечер",
    "вечера",
    "утром",
    "утро",
    "утра",
    "днем",
    "днём",
    "ночью",
    "ночь",
    "ночи",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]

TASK_VERB_HINTS = [
    "купить",
    "сделать",
    "сходить",
    "выучить",
    "скачать",
    "помыть",
    "позвонить",
    "отправить",
    "написать",
    "доделать",
    "сдать",
    "прочитать",
    "решить",
]

STOP_WORDS = {
    "по",
    "про",
    "к",
    "в",
    "на",
    "за",
    "до",
    "от",
    "с",
    "со",
    "без",
    "для",
}

GREETING_WORDS = {
    "привет",
    "приветик",
    "хай",
    "hi",
    "hello",
    "салам",
    "саламалейкум",
    "салют",
    "здорова",
    "здравствуйте",
    "добрый",
    "добрыйдень",
    "доброе",
    "утро",
    "вечер",
}

# ЛОГИ
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)
# Урезаем болтливость httpx/httpcore (телеграм и OpenAI спамят в INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def safe_render_user_reply(event: dict) -> str:
    """Безопасный обёртчик над render_user_reply, чтобы не падать из-за LLM."""
    try:
        return render_user_reply(event)
    except Exception as e:
        logger.exception("render_user_reply failed: %s", e)
        return "Операцию сделал, но не смог красиво сформулировать ответ 🙂"


# ==== КЛАВИАТУРЫ =====

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["Показать задачи", "Еще"]],
    resize_keyboard=True,
)

EXTRA_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Архив задач", "Инструкция"],
        ["Назад"],
    ],
    resize_keyboard=True,
)


# Краткая инструкция для пользователя
INSTRUCTION_TEXT = (
    "Пиши задачи обычным языком — бот сам достанет текст и дедлайн.\n\n"
    "• Статус/планы: «что завтра по задачам», «что у меня на сегодня».\n"
    "• Выполнение: «я сделал/сдал/сходил/позвонил/дочитал…».\n"
    "• Перенос: «перенеси/сдвинь/измени задачу … на …».\n"
    "• Переименование: «переименуй задачу X на Y».\n"
    "• Это бета-версия бота, если сталкиваетесь с проблемами обратитесь к @sabrval"
)


# ==== ВСПОМОГАТЕЛЬНЫЕ ШТУКИ ДЛЯ ПОИСКА ЗАДАЧ =====

def _normalize_ru_word(w: str) -> str:
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


def _tokenize_meaningful(text: str) -> list[str]:
    tokens = re.findall(r"\w+", text.lower())
    out = []
    for t in tokens:
        if t in STOP_WORDS:
            continue
        norm = _normalize_ru_word(t)
        if norm:
            out.append(norm)
    return out


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


def detect_rename_intent(text: str):
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
            old_hint = m.group(1).strip(" «»\"'“”„")
            new_title = m.group(2).strip(" «»\"'“”„")
            if new_title:
                return {"old_hint": old_hint or None, "new_title": new_title}

    # fallback: "поменяем на Y" — без старого названия, используем target_task_hint позже
    m = re.search(r"поменя\w*\s+(?:.*?\s+)?на\s+\"?(.+?)\"?$", lower, flags=re.IGNORECASE)
    if m:
        new_title = m.group(1).strip(" «»\"'“”„")
        if new_title:
            # Попробуем вытащить старый хинт как всё до слова "помен"
            idx = lower.find("помен")
            old_part = lower[:idx].strip(" «»\"'“”„")
            old_hint = old_part if old_part else None
            return {"old_hint": old_hint, "new_title": new_title}

    return None


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


def _render_clarification_message(mr: MatchResult) -> str:
    base = "Я не уверен, какую задачу ты имел в виду. Напиши, пожалуйста, полное название задачи целиком, как оно есть в списке."
    if mr.top:
        opts = "\n".join([f"- {c.task_text}" for c in mr.top[:3]])
        return base + "\n\nВозможные варианты:\n" + opts
    return base


def _match_task_or_none(
    tasks_snapshot,
    *,
    target_task_hint: str | None,
    raw_input: str,
    action: str,
) -> tuple[tuple[int, str] | None, MatchResult]:
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


def _format_deadline_human_local(deadline_iso: Optional[str]) -> Optional[str]:
    """Локальный формат дедлайна для коротких ответов."""
    if not deadline_iso:
        return None
    try:
        dt = parse_deadline_iso(deadline_iso)
        if not dt:
            return None
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return None


async def filter_tasks_by_date(user_id: int, target_date) -> list[tuple[int, str, str | None]]:
    """
    Возвращает задачи, дедлайн которых совпадает с датой target_date (в локальной TZ).
    """
    tasks = await db.get_tasks(user_id)
    result = []
    for t_id, text, due in tasks:
        if not due:
            continue
        try:
            dt = parse_deadline_iso(due)
            if not dt:
                continue
        except Exception:
            continue
        if dt.date() == target_date:
            result.append((t_id, text, due))
    return result


MONTHS_RU = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


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


# ==== ХЭЛПЕРЫ ДЛЯ НАПОМИНАНИЙ И СПИСКОВ =====

async def send_tasks_list(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """
    Отправляет список активных задач + inline-кнопку
    «Отметить задачу выполненной» + возвращает нижнее меню.
    """
    tasks = await db.get_tasks(user_id)
    now = now_local()

    if not tasks:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Спи, отдыхай! Задач нет. 🏝",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    with_due: list[str] = []
    without_due: list[str] = []

    for tid, txt, due in tasks:
        if due:
            try:
                dt = parse_deadline_iso(due)
                if not dt:
                    raise ValueError("invalid due")
                d_str = dt.strftime("%d.%m %H:%M")
                overdue = dt < now
                suffix = f"(до {d_str}" + (", просрочено🚨)" if overdue else ")")
                with_due.append(f"{len(with_due) + 1}. {txt} {suffix}")
            except Exception:
                with_due.append(f"{len(with_due) + 1}. {txt}")
        else:
            without_due.append(f"{len(without_due) + 1}. {txt}")

    parts: list[str] = ["📋 <b>Твои задачи:</b>"]

    if with_due:
        parts.append("")
        parts.append("Задачи с дедлайном:")
        parts.extend(with_due)

    if with_due and without_due:
        parts.append("")
        parts.append("---")

    if without_due:
        parts.append("")
        parts.append("Задачи без дедлайна:")
        parts.extend(without_due)

    text = "\n".join(parts)

    # 1) сообщение со списком + inline-кнопка
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Отметить задачу выполненной",
                        callback_data="mark_done_menu",
                    )
                ]
            ]
        ),
    )


async def send_archive_list(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """
    Отправляет архив выполненных задач.
    """
    tasks = await db.get_archived_tasks(user_id)
    if not tasks:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Архив пуст 🙂",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    lines: list[str] = []
    for i, (_tid, txt, _due, completed_at) in enumerate(tasks, 1):
        if completed_at:
            try:
                dt = parse_deadline_iso(completed_at)
                if not dt:
                    raise ValueError("invalid completed_at")
                c_str = dt.strftime("%d.%m %H:%M")
                lines.append(f"{i}. ✅ {txt} — выполнено {c_str}")
            except Exception:
                lines.append(f"{i}. ✅ {txt}")
        else:
            lines.append(f"{i}. ✅ {txt}")

    text = "🗂 <b>Архив выполненных задач:</b>\n\n" + "\n".join(lines)
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Очистить архив",
                        callback_data="clear_archive",
                    )
                ]
            ]
        ),
    )


def cancel_task_reminder(task_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Удаляет job напоминания по id задачи.
    Имя job-а: f"reminder:{task_id}".
    """
    if not context.job_queue:
        return

    jobs = context.job_queue.get_jobs_by_name(f"reminder:{task_id}")
    for job in jobs:
        job.schedule_removal()


def _reminder_choice_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("за 5 минут", callback_data=f"remind_set:{task_id}:5"),
                InlineKeyboardButton("за 30 минут", callback_data=f"remind_set:{task_id}:30"),
            ],
            [
                InlineKeyboardButton("за 1 час", callback_data=f"remind_set:{task_id}:60"),
                InlineKeyboardButton("в дедлайн", callback_data=f"remind_set:{task_id}:0"),
            ],
            [InlineKeyboardButton("не напоминать", callback_data=f"remind_set:{task_id}:off")],
        ]
    )


def _snooze_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Выполнено ✅", callback_data=f"done_task:{task_id}"),
                InlineKeyboardButton("Отложить ⏳", callback_data=f"snooze_prompt:{task_id}"),
            ],
        ]
    )


def _snooze_choice_keyboard(task_id: int) -> InlineKeyboardMarkup:
    """Инлайн-выбор длительности отложенного напоминания."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("+5 мин", callback_data=f"snooze:{task_id}:5"),
                InlineKeyboardButton("+30 мин", callback_data=f"snooze:{task_id}:30"),
                InlineKeyboardButton("+1 час", callback_data=f"snooze:{task_id}:60"),
            ]
        ]
    )


def _compute_remind_at_from_offset(due_iso: str, offset_min: int) -> str | None:
    """
    Считает remind_at = due_at - offset_min (в минутах).
    Если получилось в прошлом — напомним "почти сразу" (через ~10 секунд).
    """
    try:
        due_dt = parse_deadline_iso(due_iso)
        if not due_dt:
            return None
        now = now_local()
        remind_dt = due_dt - timedelta(minutes=max(offset_min, 0))
        if remind_dt <= now:
            remind_dt = now + timedelta(seconds=10)
        return normalize_deadline_iso(remind_dt.isoformat())
    except Exception:
        return None


async def send_task_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Job-функция: отправляет напоминание по задаче.
    Ожидает в job.data: {"task_id": int, "text": str}
    """
    job = context.job
    if not job:
        return

    data = job.data or {}
    task_id = data.get("task_id")
    text = data.get("text") or "задача"
    chat_id = job.chat_id
    try:
        tid = int(task_id)
    except Exception:
        tid = 0

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "⏰ Напоминание:\n\n"
            f"{text}\n\n"
            "Если хочешь задачу отложить — нажми на кнопку или отправь точное время текстом "
            "(например, «через 30 минут» или «в 18:10»)."
        ),
        reply_markup=_snooze_keyboard(tid) if tid > 0 else None,
    )


async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ежедневный дайджест: в 07:30 отправляет всем список активных задач.
    """
    user_ids = await db.get_users_with_active_tasks()
    if not user_ids:
        return

    for uid in user_ids:
        await send_tasks_list(chat_id=uid, user_id=uid, context=context)


def schedule_task_reminder(
    job_queue,
    task_id: int,
    task_text: str,
    deadline_iso: str | None,
    chat_id: int,
    *,
    remind_at_iso: str | None = None,
):
    """
    Ставит напоминание в job_queue, если дедлайн в будущем и данные валидны.
    Используется как при создании/переносе задач, так и при восстановлении после рестарта.
    """
    when_iso = remind_at_iso or deadline_iso
    if not job_queue or not when_iso:
        return

    try:
        dt = parse_deadline_iso(when_iso)
        if not dt:
            return
    except Exception:
        return

    now = now_local()
    if dt <= now:
        return

    delay = (dt - now).total_seconds()
    job_queue.run_once(
        send_task_reminder,
        when=timedelta(seconds=delay),
        chat_id=chat_id,
        name=f"reminder:{task_id}",
        data={"task_id": task_id, "text": task_text},
    )


async def restore_reminders(job_queue):
    """
    После рестарта бота восстанавливает напоминания по активным задачам с будущими дедлайнами.
    """
    if not job_queue:
        return

    now_iso = now_local_iso()
    tasks = await db.get_active_tasks_with_future_remind(now_iso)
    for task_id, user_id, text, due_at, remind_at, _offset_min in tasks:
        schedule_task_reminder(
            job_queue,
            task_id,
            text,
            deadline_iso=due_at,
            chat_id=user_id,
            remind_at_iso=remind_at,
        )

    # fallback: дедлайн в будущем, но remind_at ещё не задан
    fallback = await db.get_active_tasks_with_future_due_without_remind(now_iso)
    for task_id, user_id, text, due_at in fallback:
        schedule_task_reminder(job_queue, task_id, text, deadline_iso=due_at, chat_id=user_id)


async def restore_reminders_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запускается один раз при старте, чтобы восстановить напоминания из БД."""
    if not context.job_queue:
        return
    await restore_reminders(context.job_queue)


# ==== ОСНОВНОЙ ХЭНДЛЕР ТЕКСТА =====

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    ai_result_preparsed: TaskInterpretation | None = None

    # Логируем исходный текст пользователя (после возможной подстановки из голосового)
    logger.info("Incoming text from user %s (chat %s): %r", user_id, chat_id, text)

    # --- 0. Обработка "кнопок" (нижняя клавиатура) ---
    if text == "Показать задачи":
        await send_tasks_list(chat_id, user_id, context)
        return

    if text == "Еще":
        await update.message.reply_text(
            "Дополнительные функции:",
            reply_markup=EXTRA_KEYBOARD,
        )
        return

    if text == "Архив задач":
        await send_archive_list(chat_id, user_id, context)
        return

    if text == "Инструкция":
        await update.message.reply_text(
            INSTRUCTION_TEXT,
            reply_markup=EXTRA_KEYBOARD,
        )
        return

    if text == "Назад":
        await update.message.reply_text(
            "Возвращаюсь в главное меню.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # --- Приветствие / онбординг ---
    if is_greeting_only(text):
        await update.message.reply_text(
            "Привет! Я умный таск-менеджер: превращаю свободные фразы в задачи с дедлайнами. "
            "Нажми «Инструкция» или просто напиши задачу — я добавлю её в список.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # --- 1. Проверка: не ждём ли мы сейчас уточнение дедлайна по прошлой задаче ---
    pending = context.user_data.get("pending_deadline")
    pending_reschedule = context.user_data.get("pending_reschedule")
    if pending:
        lower = text.lower().strip()

        if lower in NO_DEADLINE_PHRASES:
            context.user_data.pop("pending_deadline", None)
            await update.message.reply_text(
                "Ок, оставляю задачу без дедлайна.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        try:
            parsed = parse_user_input(text, tasks_snapshot=await db.get_tasks(user_id))
        except Exception:
            context.user_data.pop("pending_deadline", None)
            await update.message.reply_text(
                "Я не смог нормально понять срок, оставляю задачу без дедлайна.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        service_actions = {"unknown", "show_active", "show_today", "show_tomorrow", "show_date"}
        meaningful_actions = {"create", "complete", "delete", "reschedule", "add_deadline", "clear_deadline", "rename"}

        # Вариант А: пользователь дал только дату/время → уточнение дедлайна
        if (
            parsed.deadline_iso
            and parsed.title is None
            and parsed.target_task_hint is None
            and parsed.action in service_actions
        ):
            task_id = pending["task_id"]
            task_text = pending["text"]

            due_norm = normalize_deadline_iso(parsed.deadline_iso)
            await db.update_task_due(user_id, task_id, due_norm)
            # дефолтное напоминание: в дедлайн
            await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=due_norm, remind_offset_min=0)

            dt = parse_deadline_iso(due_norm)
            new_time = dt.strftime("%d.%m %H:%M") if dt else "непонятное время"

            schedule_task_reminder(
                context.job_queue,
                task_id=task_id,
                task_text=task_text,
                deadline_iso=due_norm,
                chat_id=chat_id,
                remind_at_iso=due_norm,
            )

            await update.message.reply_text(
                f"⏰ Добавил дедлайн для «{task_text}»: {new_time}",
                reply_markup=MAIN_KEYBOARD,
            )
            context.user_data.pop("pending_deadline", None)
            return

        # Вариант Б: новая осмысленная команда → выходим из pending и продолжаем общий пайплайн
        if parsed.action in meaningful_actions and (parsed.title or parsed.target_task_hint):
            context.user_data.pop("pending_deadline", None)
            ai_result_preparsed = parsed
        else:
            # fallback: считаем это новой командой, но без дедлайна к прошлой
            context.user_data.pop("pending_deadline", None)
            ai_result_preparsed = parsed

    if pending_reschedule:
        lower = text.lower().strip()

        if lower in NO_DEADLINE_PHRASES:
            context.user_data.pop("pending_reschedule", None)
            await update.message.reply_text(
                "Ок, перенос отменяю, дедлайн не меняю.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        try:
            parsed = parse_user_input(text, tasks_snapshot=await db.get_tasks(user_id))
        except Exception:
            context.user_data.pop("pending_reschedule", None)
            await update.message.reply_text(
                "Не смог понять новую дату, перенос отменён.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        service_actions = {"unknown", "show_active", "show_today", "show_tomorrow", "show_date"}
        meaningful_actions = {"create", "complete", "delete", "reschedule", "add_deadline", "clear_deadline", "rename"}

        if (
            parsed.deadline_iso
            and parsed.title is None
            and parsed.target_task_hint is None
            and parsed.action in service_actions
        ):
            task_id = pending_reschedule["task_id"]
            task_text = pending_reschedule["text"]

            cancel_task_reminder(task_id, context)
            new_due = normalize_deadline_iso(parsed.deadline_iso)
            await db.update_task_due(user_id, task_id, new_due)

            # пересчитываем напоминание с учётом сохранённого offset (если есть)
            _remind_at, offset_min, _due_at_db, _task_text_db = await db.get_task_reminder_settings(user_id, task_id)
            if offset_min is None:
                new_remind_at = new_due
            else:
                new_remind_at = _compute_remind_at_from_offset(new_due, offset_min) if new_due else None
            await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=new_remind_at, remind_offset_min=offset_min)

            schedule_task_reminder(
                context.job_queue,
                task_id=task_id,
                task_text=task_text,
                deadline_iso=new_due,
                chat_id=chat_id,
                remind_at_iso=new_remind_at,
            )

            dt = parse_deadline_iso(new_due)
            new_time = dt.strftime("%d.%m %H:%M") if dt else "непонятное время"
            await update.message.reply_text(
                f"🔄 Перенёс «{task_text}» на {new_time}",
                reply_markup=MAIN_KEYBOARD,
            )
            context.user_data.pop("pending_reschedule", None)
            return

        if parsed.action in meaningful_actions and (parsed.title or parsed.target_task_hint):
            context.user_data.pop("pending_reschedule", None)
            ai_result_preparsed = parsed
        else:
            context.user_data.pop("pending_reschedule", None)
            ai_result_preparsed = parsed

    # --- 1.1 Напоминания: ждём выбор "за сколько" или "отложить" ---
    pending_reminder_choice = context.user_data.get("pending_reminder_choice")
    if pending_reminder_choice:
        task_id = pending_reminder_choice.get("task_id")
        if isinstance(task_id, int):
            lower = text.lower().strip()

            remind_at, remind_offset_min, due_at, task_text_db = await db.get_task_reminder_settings(user_id, task_id)
            deadline_dt = parse_deadline_iso(due_at) if due_at else None

            if lower in NO_REMINDER_PHRASES:
                cancel_task_reminder(task_id, context)
                await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=None, remind_offset_min=None)
                context.user_data.pop("pending_reminder_choice", None)
                await update.message.reply_text("Ок, не буду напоминать по этой задаче.", reply_markup=MAIN_KEYBOARD)
                return

            offset_min = parse_offset_minutes(text)
            if offset_min is not None:
                if not due_at:
                    context.user_data.pop("pending_reminder_choice", None)
                    await update.message.reply_text(
                        "У этой задачи пока нет дедлайна — напоминание не настроить.",
                        reply_markup=MAIN_KEYBOARD,
                    )
                    return

                new_remind_at = _compute_remind_at_from_offset(due_at, offset_min)
                if not new_remind_at:
                    await update.message.reply_text(
                        "Не смог понять время напоминания. Выбери кнопку или напиши «за 30 минут».",
                        reply_markup=MAIN_KEYBOARD,
                    )
                    return

                cancel_task_reminder(task_id, context)
                await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=new_remind_at, remind_offset_min=offset_min)
                schedule_task_reminder(
                    context.job_queue,
                    task_id=task_id,
                    task_text=task_text_db or "задача",
                    deadline_iso=due_at,
                    chat_id=chat_id,
                    remind_at_iso=new_remind_at,
                )
                context.user_data.pop("pending_reminder_choice", None)
                await update.message.reply_text(f"Ок, напомню за {offset_min} мин.", reply_markup=MAIN_KEYBOARD)
                return

            now = now_local()
            base_date = deadline_dt.date() if deadline_dt else None
            dt = parse_datetime_from_text(text, now=now, base_date=base_date)
            if dt:
                if deadline_dt and dt > deadline_dt:
                    await update.message.reply_text(
                        "Это время позже дедлайна. Напиши время ДО дедлайна (например, «за 30 минут» или «в 08:30»).",
                        reply_markup=MAIN_KEYBOARD,
                    )
                    return
                if dt <= now:
                    await update.message.reply_text("Время должно быть в будущем. Попробуй ещё раз.", reply_markup=MAIN_KEYBOARD)
                    return
                remind_iso = normalize_deadline_iso(dt.isoformat())
                cancel_task_reminder(task_id, context)
                await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=remind_iso, remind_offset_min=None)
                schedule_task_reminder(
                    context.job_queue,
                    task_id=task_id,
                    task_text=task_text_db or "задача",
                    deadline_iso=due_at,
                    chat_id=chat_id,
                    remind_at_iso=remind_iso,
                )
                context.user_data.pop("pending_reminder_choice", None)
                await update.message.reply_text(
                    f"Ок, напомню в {dt.strftime('%d.%m %H:%M')}.",
                    reply_markup=MAIN_KEYBOARD,
                )
                return

            if is_deadline_like(text):
                await update.message.reply_text(
                    "Не понял. Выбери кнопку или напиши, например, «за 30 минут» / «за 1 час» / «в 08:30».",
                    reply_markup=MAIN_KEYBOARD,
                )
                return

            # не похоже на выбор времени → считаем это новой командой, снимаем режим
            context.user_data.pop("pending_reminder_choice", None)

    pending_snooze = context.user_data.get("pending_snooze")
    if pending_snooze:
        task_id = pending_snooze.get("task_id")
        if isinstance(task_id, int):
            lower = text.lower().strip()
            if lower in NO_REMINDER_PHRASES:
                context.user_data.pop("pending_snooze", None)
                await update.message.reply_text("Ок.", reply_markup=MAIN_KEYBOARD)
                return

            now = now_local()
            delay_min = parse_delay_minutes(text)
            if delay_min is None:
                # часто пишут просто "30 минут" — тоже трактуем как delay
                delay_min = parse_offset_minutes(text)

            dt = None
            if delay_min is not None:
                dt = now + timedelta(minutes=max(delay_min, 0))
            else:
                dt = parse_datetime_from_text(text, now=now, base_date=now.date())

            if not dt or dt <= now:
                if is_deadline_like(text):
                    await update.message.reply_text(
                        "Не понял. Напиши, например, «через 5 минут», «через 30 минут» или «в 18:10».",
                        reply_markup=MAIN_KEYBOARD,
                    )
                    return
                context.user_data.pop("pending_snooze", None)
            else:
                _remind_at, offset_min, due_at, task_text_db = await db.get_task_reminder_settings(user_id, task_id)
                remind_iso = normalize_deadline_iso(dt.isoformat())
                cancel_task_reminder(task_id, context)
                await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=remind_iso, remind_offset_min=offset_min)
                schedule_task_reminder(
                    context.job_queue,
                    task_id=task_id,
                    task_text=task_text_db or "задача",
                    deadline_iso=due_at,
                    chat_id=chat_id,
                    remind_at_iso=remind_iso,
                )
                context.user_data.pop("pending_snooze", None)
                await update.message.reply_text(
                    f"Ок, отложил напоминание до {dt.strftime('%d.%m %H:%M')}.",
                    reply_markup=MAIN_KEYBOARD,
                )
                return

    # --- 2. ИИ-парсинг обычного текста ---
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Быстрая эвристика: если спрашивают "что/есть ли на завтра/сегодня/конкретную дату"
    lower_text = text.lower()
    question_like = any(
        q in lower_text
        for q in ["что у меня", "что по", "есть ли", "что на", "какие задачи", "есть что-то"]
    )

    if question_like:
        now = now_local()
        target_date = None

        if "завтра" in lower_text:
            target_date = (now + timedelta(days=1)).date()
        elif "сегодня" in lower_text or "на сегодня" in lower_text:
            target_date = now.date()
        else:
            # пробуем вытащить явную дату типа "9 декабря"
            explicit = parse_explicit_date(lower_text)
            if explicit:
                target_date = explicit

        if target_date:
            tasks_for_day = await filter_tasks_by_date(user_id, target_date)
            if tasks_for_day:
                lines = []
                for i, (tid, txt, due) in enumerate(tasks_for_day, 1):
                    try:
                        dt = parse_deadline_iso(due)
                        if not dt:
                            raise ValueError("invalid due")
                        d_str = dt.strftime("%d.%m %H:%M")
                        lines.append(f"{i}. {txt} (до {d_str})")
                    except Exception:
                        lines.append(f"{i}. {txt}")
                await update.message.reply_text(
                    "📌 Задачи на выбранный день:\n\n" + "\n".join(lines),
                    reply_markup=MAIN_KEYBOARD,
                )
                return
            else:
                await update.message.reply_text(
                    "На этот день задач нет 🙂",
                    reply_markup=MAIN_KEYBOARD,
                )
                return

    tasks_snapshot = await db.get_tasks(user_id)

    # --- Авто-роутинг single vs multi ---#
    ai_result: Optional[TaskInterpretation] = ai_result_preparsed
    multi_results: list[TaskInterpretation] = []

    if ai_result_preparsed is None:
        lower_for_route = text.lower()
        multi_markers = (";", "\n")
        has_separator = any(m in text for m in multi_markers) or ("," in text and len(text) > 40)
        has_connectors = any(w in lower_for_route for w in (" и ", " потом ", " затем ", " также ", " ещё "))
        route_multi = has_separator or has_connectors

        logger.info(
            "parser_route %s",
            json.dumps(
                {
                    "user_id": user_id,
                    "text": text,
                    "route": "multi" if route_multi else "single",
                    "signals": {
                        "has_separator": has_separator,
                        "has_connectors": has_connectors,
                    },
                },
                ensure_ascii=False,
            ),
        )

        if route_multi:
            try:
                multi_results = parse_user_input_multi(text, tasks_snapshot=tasks_snapshot)
            except Exception as e:
                logger.exception("parse_user_input_multi failed for user %s: %s", user_id, e)

            if multi_results:
                logger.info(
                    "Multi-parsed %d items for user %s: %s",
                    len(multi_results),
                    user_id,
                    [m.model_dump() for m in multi_results],
                )
        else:
            try:
                ai_result = parse_user_input(text, tasks_snapshot=tasks_snapshot)
            except Exception as e:
                logger.exception("parse_user_input failed for user %s: %s", user_id, e)
                await update.message.reply_text(
                    f"🤯 Мозг сломался: {e}",
                    reply_markup=MAIN_KEYBOARD,
                )
                return

    # Батч включаем, если LLM реально вернул несколько действий (или хотя бы одно полезное)
    supported_actions_multi = {
        "create",
        "complete",
        "reschedule",
        "add_deadline",
        "clear_deadline",
        "delete",
        "rename",
        "needs_clarification",
        "unknown",
    }
    if multi_results and all(m.action in supported_actions_multi for m in multi_results):
        # локальная рабочая копия снапшота, чтобы учитывать create/rename внутри одного сообщения
        tasks_snapshot_work = list(tasks_snapshot)
        created_lines: list[str] = []
        completed_lines: list[str] = []
        rescheduled_lines: list[str] = []
        add_deadline_lines: list[str] = []
        clear_deadline_lines: list[str] = []
        deleted_lines: list[str] = []
        renamed_lines: list[str] = []
        not_found_lines: list[str] = []
        clarification_lines: list[str] = []
        needs_deadline_lines: list[str] = []
        needs_reschedule_deadline_lines: list[str] = []
        pending_deadline_data: dict | None = None
        pending_reschedule_data: dict | None = None

        for item in multi_results:
            if item.action in {"unknown"}:
                continue
            if item.action == "needs_clarification":
                clarification_lines.append("• нужно уточнение по одной из задач — напиши название полностью.")
                continue

            if item.action == "create":
                task_text = item.title or item.raw_input
                norm_due = normalize_deadline_iso(item.deadline_iso)
                task_id = await db.add_task(
                    user_id,
                    task_text,
                    norm_due,
                )
                tasks_snapshot_work.append((task_id, task_text, norm_due))

                # ставим напоминание только если дедлайн есть и в будущем
                if item.deadline_iso:
                    schedule_task_reminder(
                        context.job_queue,
                        task_id=task_id,
                        task_text=task_text,
                        deadline_iso=norm_due,
                        chat_id=chat_id,
                    )

                human_deadline = _format_deadline_human_local(item.deadline_iso)
                if human_deadline:
                    created_lines.append(f"• создано: {task_text} (до {human_deadline})")
                else:
                    created_lines.append(f"• создано: {task_text}")
                    # если дедлайна нет — предложим уточнить (как в single-режиме)
                    if pending_deadline_data is None:
                        pending_deadline_data = {"task_id": task_id, "text": task_text}
                        needs_deadline_lines.append(
                            f"• для «{task_text}» укажи срок (например, «завтра 18:00» или «нет»)"
                        )

            elif item.action == "complete":
                target, mr = _match_task_or_none(
                    tasks_snapshot_work,
                    target_task_hint=item.target_task_hint,
                    raw_input=item.raw_input,
                    action=item.action,
                )
                if not target:
                    clarification_lines.append(_render_clarification_message(mr))
                    continue

                task_id, task_text = target
                cancel_task_reminder(task_id, context)
                await db.set_task_done(user_id, task_id)
                completed_lines.append(f"• выполнена: {task_text}")

            elif item.action in {"reschedule", "add_deadline"}:
                target, mr = _match_task_or_none(
                    tasks_snapshot_work,
                    target_task_hint=item.target_task_hint,
                    raw_input=item.raw_input,
                    action=item.action,
                )
                if not target:
                    clarification_lines.append(_render_clarification_message(mr))
                    continue
                task_id, task_text = target

                if not item.deadline_iso:
                    if pending_reschedule_data is None:
                        pending_reschedule_data = {"task_id": task_id, "text": task_text}
                        needs_reschedule_deadline_lines.append(
                            f"• для срока по «{task_text}» укажи дату/время (например, «завтра 18:00» или «нет»)"
                        )
                    continue

                cancel_task_reminder(task_id, context)
                new_due = normalize_deadline_iso(item.deadline_iso)
                await db.update_task_due(user_id, task_id, new_due)
                tasks_snapshot_work = [(tid, txt, (new_due if tid == task_id else due)) for (tid, txt, due) in tasks_snapshot_work]

                # пересчитываем remind_at с учётом сохранённого offset (если есть)
                _remind_at, offset_min, _due_db, task_text_db = await db.get_task_reminder_settings(user_id, task_id)
                if offset_min is None:
                    new_remind_at = new_due
                else:
                    new_remind_at = _compute_remind_at_from_offset(new_due, offset_min) if new_due else None
                await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=new_remind_at, remind_offset_min=offset_min)

                schedule_task_reminder(
                    context.job_queue,
                    task_id=task_id,
                    task_text=task_text,
                    deadline_iso=new_due,
                    chat_id=chat_id,
                    remind_at_iso=new_remind_at,
                )
                human_deadline = _format_deadline_human_local(item.deadline_iso)
                if item.action == "add_deadline":
                    add_deadline_lines.append(
                        f"• добавил дедлайн: {task_text}" + (f" → {human_deadline}" if human_deadline else "")
                    )
                else:
                    rescheduled_lines.append(
                        f"• перенёс: {task_text}" + (f" → {human_deadline}" if human_deadline else "")
                    )

            elif item.action == "clear_deadline":
                target, mr = _match_task_or_none(
                    tasks_snapshot_work,
                    target_task_hint=item.target_task_hint,
                    raw_input=item.raw_input,
                    action=item.action,
                )
                if not target:
                    clarification_lines.append(_render_clarification_message(mr))
                    continue
                task_id, task_text = target
                cancel_task_reminder(task_id, context)
                await db.update_task_due(user_id, task_id, None)
                await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=None, remind_offset_min=None)
                tasks_snapshot_work = [(tid, txt, (None if tid == task_id else due)) for (tid, txt, due) in tasks_snapshot_work]
                clear_deadline_lines.append(f"• убрал дедлайн: {task_text}")

            elif item.action == "rename":
                target, mr = _match_task_or_none(
                    tasks_snapshot_work,
                    target_task_hint=item.target_task_hint,
                    raw_input=item.raw_input,
                    action=item.action,
                )
                if not target:
                    clarification_lines.append(_render_clarification_message(mr))
                    continue
                if not item.title:
                    clarification_lines.append("• для переименования нужно новое название.")
                    continue
                task_id, _task_text = target
                await db.update_task_text(user_id, task_id, item.title)
                tasks_snapshot_work = [(tid, (item.title if tid == task_id else txt), due) for (tid, txt, due) in tasks_snapshot_work]
                renamed_lines.append(f"• переименовал: {item.title}")

            elif item.action == "delete":
                target, mr = _match_task_or_none(
                    tasks_snapshot_work,
                    target_task_hint=item.target_task_hint,
                    raw_input=item.raw_input,
                    action=item.action,
                )
                if not target:
                    clarification_lines.append(_render_clarification_message(mr))
                    continue
                task_id, task_text = target
                cancel_task_reminder(task_id, context)
                await db.delete_task(user_id, task_id)
                deleted_lines.append(f"• удалена: {task_text}")

        parts: list[str] = []
        if created_lines:
            parts.append("Добавил задачи:")
            parts.extend(created_lines)
        if completed_lines:
            if parts:
                parts.append("")
            parts.append("Отметил выполненными:")
            parts.extend(completed_lines)
        if rescheduled_lines:
            if parts:
                parts.append("")
            parts.append("Перенёс дедлайны:")
            parts.extend(rescheduled_lines)
        if add_deadline_lines:
            if parts:
                parts.append("")
            parts.append("Добавил дедлайны:")
            parts.extend(add_deadline_lines)
        if clear_deadline_lines:
            if parts:
                parts.append("")
            parts.append("Убрал дедлайны:")
            parts.extend(clear_deadline_lines)
        if renamed_lines:
            if parts:
                parts.append("")
            parts.append("Переименовал задачи:")
            parts.extend(renamed_lines)
        if deleted_lines:
            if parts:
                parts.append("")
            parts.append("Удалил задачи:")
            parts.extend(deleted_lines)
        if clarification_lines:
            if parts:
                parts.append("")
            parts.append("Нужно уточнение:")
            parts.extend(clarification_lines[:3])
        if needs_deadline_lines:
            if parts:
                parts.append("")
            parts.append("Нужен дедлайн:")
            parts.extend(needs_deadline_lines)
        if needs_reschedule_deadline_lines:
            if parts:
                parts.append("")
            parts.append("Нужна дата для переноса:")
            parts.extend(needs_reschedule_deadline_lines)
        if not_found_lines:
            if parts:
                parts.append("")
            parts.append("Не смог сопоставить:")
            parts.extend(not_found_lines)

        reply_text = "\n".join(parts) if parts else "Ничего не сделал."
        await update.message.reply_text(reply_text, reply_markup=MAIN_KEYBOARD)

        # включаем режим уточнения дедлайна / переноса только если он ещё не активен
        if pending_deadline_data and "pending_deadline" not in context.user_data:
            context.user_data["pending_deadline"] = pending_deadline_data
        if pending_reschedule_data and "pending_reschedule" not in context.user_data:
            context.user_data["pending_reschedule"] = pending_reschedule_data
        return

    if len(multi_results) == 1 and multi_results[0].action in supported_actions_multi:
        ai_result = multi_results[0]

    # Если шли по multi и ничего не получили — fallback в single
    if ai_result is None and not multi_results:
        try:
            ai_result = parse_user_input(text, tasks_snapshot=tasks_snapshot)
        except Exception as e:
            logger.exception("parse_user_input failed for user %s: %s", user_id, e)
            await update.message.reply_text(
                f"🤯 Мозг сломался: {e}",
                reply_markup=MAIN_KEYBOARD,
            )
            return

    # Логируем ответ парсера для дальнейшего дебага
    logger.info("Parsed intent for user %s: %s", user_id, ai_result.model_dump())

    # Предохранитель от массовых действий типа "очистить список задач"
    MASS_CLEAR_HINTS = [
        "очистить список задач",
        "очисти список задач",
        "очистить список",
        "очисти список",
        "очистить все задачи",
        "очисти все задачи",
        "удали все задачи",
        "удалить все задачи",
        "убери все задачи",
        "убери всё из списка",
        "очистить задачи",
        "очисти задачи",
        "очистить список дел",
        "очисти список дел",
    ]

    if ai_result.action in ["complete", "delete"] and any(
        phrase in lower_text for phrase in MASS_CLEAR_HINTS
    ):
        await update.message.reply_text(
            "Пока я не умею очищать все задачи разом — могу помогать закрывать их по одной 🙂",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # --- 3. Переименование задачи через модель ---
    if ai_result.action == "rename":
        target_hint = ai_result.target_task_hint or ai_result.raw_input
        if not ai_result.title:
            await update.message.reply_text(
                "Мне нужно новое название задачи, но модель его не вернула.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        target, mr = _match_task_or_none(
            tasks_snapshot,
            target_task_hint=ai_result.target_task_hint,
            raw_input=ai_result.raw_input,
            action=ai_result.action,
        )
        if not target:
            await update.message.reply_text(_render_clarification_message(mr), reply_markup=MAIN_KEYBOARD)
            return

        task_id, _task_text = target
        await db.update_task_text(user_id, task_id, ai_result.title)
        await update.message.reply_text(
            f"✏️ Переименовал задачу: <b>{ai_result.title}</b>",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # --- 4. Маршрутизация действий ---

    # СОЗДАНИЕ
    if ai_result.action == "create":
        task_text = ai_result.title or ai_result.raw_input

        task_id = await db.add_task(
            user_id,
            task_text,
            normalize_deadline_iso(ai_result.deadline_iso),
        )

        # Если у задачи есть дедлайн — предложим выбрать, за сколько напомнить (inline + текст)
        if ai_result.deadline_iso:
            due_norm = normalize_deadline_iso(ai_result.deadline_iso)
            human_deadline = _format_deadline_human_local(due_norm) or "непонятное время"

            # дефолт: напоминание "в дедлайн" (remind_at = due_at)
            if due_norm:
                schedule_task_reminder(
                    context.job_queue,
                    task_id=task_id,
                    task_text=task_text,
                    deadline_iso=due_norm,
                    chat_id=chat_id,
                    remind_at_iso=due_norm,
                )

            context.user_data["pending_reminder_choice"] = {"task_id": task_id}
            await update.message.reply_text(
                f"Задача «{task_text}» добавлена! Дедлайн установлен на {human_deadline}. "
                "За сколько вам напомнить о ней?\n\n"
                "Нажми на кнопку либо отправь точное время текстом (например, «за 30 минут» или «в 08:30»).",
                reply_markup=_reminder_choice_keyboard(task_id),
            )
            return

        event = {
            "type": "task_created",
            "task_text": task_text,
            "deadline_iso": normalize_deadline_iso(ai_result.deadline_iso),
            "prev_deadline_iso": None,
            "num_active_tasks": len(await db.get_tasks(user_id)),
            "language": "ru",
            "extra": {},
        }

        reply_text = safe_render_user_reply(event)

        await update.message.reply_text(
            reply_text,
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )

        # дедлайна нет → включаем режим уточнения
        context.user_data["pending_deadline"] = {
            "task_id": task_id,
            "text": task_text,
        }
        await update.message.reply_text(
            "🕒 Хочешь указать срок? Можешь ответить так: «завтра», «в понедельник», «завтра в 18:00». "
            "Если дедлайн не нужен — напиши «нет» или «без дедлайна».",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # ВЫПОЛНЕНИЕ / УДАЛЕНИЕ
    elif ai_result.action in ["complete", "delete"]:
        target, mr = _match_task_or_none(
            tasks_snapshot,
            target_task_hint=ai_result.target_task_hint,
            raw_input=ai_result.raw_input,
            action=ai_result.action,
        )
        if not target:
            await update.message.reply_text(_render_clarification_message(mr), reply_markup=MAIN_KEYBOARD)
            return

        task_id, task_text = target
        # отменяем напоминание, если было
        cancel_task_reminder(task_id, context)

        if ai_result.action == "complete":
            await db.set_task_done(user_id, task_id)
            event = {
                "type": "task_completed",
                "task_text": task_text,
                "deadline_iso": None,
                "prev_deadline_iso": None,
                "num_active_tasks": len(await db.get_tasks(user_id)),
                "language": "ru",
                "extra": {},
            }
        else:
            await db.delete_task(user_id, task_id)
            event = {
                "type": "task_deleted",
                "task_text": task_text,
                "deadline_iso": None,
                "prev_deadline_iso": None,
                "num_active_tasks": len(await db.get_tasks(user_id)),
                "language": "ru",
                "extra": {},
            }

        reply_text = safe_render_user_reply(event)
        await update.message.reply_text(
            reply_text,
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )

    # ПЕРЕНОС
    elif ai_result.action in ["reschedule", "add_deadline"]:
        target, mr = _match_task_or_none(
            tasks_snapshot,
            target_task_hint=ai_result.target_task_hint,
            raw_input=ai_result.raw_input,
            action=ai_result.action,
        )
        if not target:
            await update.message.reply_text(_render_clarification_message(mr), reply_markup=MAIN_KEYBOARD)
            return

        task_id, task_text = target
        if not ai_result.deadline_iso:
            await update.message.reply_text(
                "🤔 Я понял, что надо перенести, но не понял НА КОГДА. Напиши, например: «завтра в 18:00» или «в понедельник». Если передумал — скажи «нет».",
                reply_markup=MAIN_KEYBOARD,
            )
            context.user_data["pending_reschedule"] = {
                "task_id": task_id,
                "text": task_text,
            }
            return

        # снимаем старое напоминание
        cancel_task_reminder(task_id, context)

        prev_task = await db.get_task(user_id, task_id)
        prev_deadline = prev_task[2] if prev_task else None

        new_due = normalize_deadline_iso(ai_result.deadline_iso)
        await db.update_task_due(user_id, task_id, new_due)

        # пересчитываем следующее напоминание, сохраняя "за сколько" (offset), если оно задано
        _remind_at, offset_min, _due_at_db, task_text_db = await db.get_task_reminder_settings(user_id, task_id)
        if offset_min is None:
            new_remind_at = new_due
        else:
            new_remind_at = _compute_remind_at_from_offset(new_due, offset_min) if new_due else None
        await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=new_remind_at, remind_offset_min=offset_min)

        # ставим новое напоминание
        schedule_task_reminder(
            context.job_queue,
            task_id=task_id,
            task_text=task_text,
            deadline_iso=new_due,
            chat_id=chat_id,
            remind_at_iso=new_remind_at,
        )

        event = {
            "type": "task_rescheduled",
            "task_text": task_text,
            "deadline_iso": new_due,
            "prev_deadline_iso": prev_deadline,
            "num_active_tasks": len(await db.get_tasks(user_id)),
            "language": "ru",
            "extra": {},
        }
        reply_text = safe_render_user_reply(event)
        await update.message.reply_text(
            reply_text,
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    elif ai_result.action == "clear_deadline":
        target, mr = _match_task_or_none(
            tasks_snapshot,
            target_task_hint=ai_result.target_task_hint,
            raw_input=ai_result.raw_input,
            action=ai_result.action,
        )
        if not target:
            await update.message.reply_text(_render_clarification_message(mr), reply_markup=MAIN_KEYBOARD)
            return
        task_id, task_text = target
        cancel_task_reminder(task_id, context)
        prev_task = await db.get_task(user_id, task_id)
        prev_deadline = prev_task[2] if prev_task else None
        await db.update_task_due(user_id, task_id, None)
        await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=None, remind_offset_min=None)
        event = {
            "type": "task_rescheduled",
            "task_text": task_text,
            "deadline_iso": None,
            "prev_deadline_iso": prev_deadline,
            "num_active_tasks": len(await db.get_tasks(user_id)),
            "language": "ru",
            "extra": {"action": "clear_deadline"},
        }
        reply_text = safe_render_user_reply(event)
        await update.message.reply_text(
            reply_text,
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )

    elif ai_result.action == "needs_clarification":
        await update.message.reply_text(
            "Я не уверен, что именно нужно сделать. Напиши, пожалуйста, полное название задачи целиком, как оно есть в списке.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # ПОКАЗАТЬ ЗАДАЧИ (через текст, а не кнопку)
    elif ai_result.action in ["show_active", "show_today", "show_tomorrow", "show_date"]:
        target_date = None
        weekend_mode = False
        if ai_result.action == "show_today":
            target_date = now_local().date()
        elif ai_result.action == "show_tomorrow":
            target_date = (now_local() + timedelta(days=1)).date()
        elif ai_result.action == "show_date" and ai_result.deadline_iso:
            try:
                dt = parse_deadline_iso(ai_result.deadline_iso)
                target_date = dt.date() if dt else None
            except Exception:
                target_date = None
        if ai_result.action == "show_date" and getattr(ai_result, "note", None) == "weekend":
            weekend_mode = True

        if target_date:
            if weekend_mode:
                # показываем ближайшие субботу и воскресенье
                today = now_local().date()
                weekday = today.weekday()  # 0=Mon
                days_to_sat = (5 - weekday) % 7
                days_to_sun = (6 - weekday) % 7
                sat_date = today + timedelta(days=days_to_sat)
                sun_date = today + timedelta(days=days_to_sun)

                parts = []
                for label, d in [("Суббота", sat_date), ("Воскресенье", sun_date)]:
                    tasks_for_day = await filter_tasks_by_date(user_id, d)
                    if tasks_for_day:
                        lines = []
                        for i, (tid, txt, due) in enumerate(tasks_for_day, 1):
                            try:
                                dt = parse_deadline_iso(due)
                                if not dt:
                                    raise ValueError("invalid due")
                                d_str = dt.strftime("%d.%m %H:%M")
                                lines.append(f"{i}. {txt} (до {d_str})")
                            except Exception:
                                lines.append(f"{i}. {txt}")
                        parts.append(f"📌 {label}:\n" + "\n".join(lines))
                if parts:
                    await update.message.reply_text(
                        "\n\n".join(parts),
                        reply_markup=MAIN_KEYBOARD,
                    )
                else:
                    await update.message.reply_text(
                        "На выходных задач нет 🙂",
                        reply_markup=MAIN_KEYBOARD,
                    )
            else:
                tasks_for_day = await filter_tasks_by_date(user_id, target_date)
                if tasks_for_day:
                    lines = []
                    for i, (tid, txt, due) in enumerate(tasks_for_day, 1):
                        try:
                            dt = parse_deadline_iso(due)
                            if not dt:
                                raise ValueError("invalid due")
                            d_str = dt.strftime("%d.%m %H:%M")
                            lines.append(f"{i}. {txt} (до {d_str})")
                        except Exception:
                            lines.append(f"{i}. {txt}")
                    await update.message.reply_text(
                        "📌 Задачи на выбранный день:\n\n" + "\n".join(lines),
                        reply_markup=MAIN_KEYBOARD,
                    )
                else:
                    await update.message.reply_text(
                        "На этот день задач нет 🙂",
                        reply_markup=MAIN_KEYBOARD,
                    )
        else:
            await send_tasks_list(chat_id, user_id, context)

        tasks_now = await db.get_tasks(user_id)
        event = {
            "type": "show_tasks" if tasks_now else "no_tasks",
            "task_text": None,
            "deadline_iso": None,
            "prev_deadline_iso": None,
            "num_active_tasks": len(tasks_now),
            "language": "ru",
            "extra": {"mode": ai_result.action},
        }
        reply_text = safe_render_user_reply(event)
        await update.message.reply_text(
            reply_text,
            reply_markup=MAIN_KEYBOARD,
        )

    # НЕПОНЯТНО
    elif ai_result.action == "unknown":
        event = {
            "type": "error",
            "task_text": None,
            "deadline_iso": None,
            "prev_deadline_iso": None,
            "num_active_tasks": len(await db.get_tasks(user_id)),
            "language": "ru",
            "extra": {"reason": "unknown_intent"},
        }
        reply_text = safe_render_user_reply(event)
        await update.message.reply_text(
            reply_text,
            reply_markup=MAIN_KEYBOARD,
        )


# ==== CALLBACK-ХЭНДЛЕРЫ ДЛЯ INLINE-КНОПОК =====

async def on_mark_done_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Нажали кнопку "Отметить задачу выполненной" под списком задач.
    Показываем список задач как inline-кнопки.
    """
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    tasks = await db.get_tasks(user_id)

    if not tasks:
        await query.edit_message_text("Активных задач нет 🙂")
        return

    keyboard: list[list[InlineKeyboardButton]] = []
    for task_id, text, _ in tasks:
        label = text if len(text) <= 30 else text[:27] + "..."
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"✅ {label}",
                    callback_data=f"done_task:{task_id}",
                )
            ]
        )

    await query.edit_message_reply_markup(
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def on_mark_done_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Пользователь выбрал конкретную задачу для отметки как выполненной.
    """
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    try:
        _, task_id_str = data.split(":", maxsplit=1)
        task_id = int(task_id_str)
    except Exception:
        return

    user_id = query.from_user.id

    # отменяем напоминание
    cancel_task_reminder(task_id, context)

    # найдём текст задачи, чтобы красиво показать
    tasks = await db.get_tasks(user_id)
    task_text = None
    for tid, txt, _ in tasks:
        if tid == task_id:
            task_text = txt
            break

    await db.set_task_done(user_id, task_id)

    if task_text:
        await query.edit_message_text(
            f"👍 Задача «{task_text}» отмечена выполненной.",
        )
    else:
        await query.edit_message_text("👍 Задача отмечена выполненной.")

    # отправим обновлённый список задач + меню
    await send_tasks_list(query.message.chat_id, user_id, context)


async def on_clear_archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Пользователь нажал кнопку очистки архива выполненных задач.
    """
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    await db.clear_archived_tasks(user_id)

    await query.edit_message_text("Архив очищен 🙂")


async def on_remind_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Inline-выбор "за сколько напомнить" после создания задачи с дедлайном.
    callback_data: remind_set:{task_id}:{5|30|60|0|off}
    """
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    try:
        _, task_id_str, val = data.split(":", maxsplit=2)
        task_id = int(task_id_str)
    except Exception:
        return

    user_id = query.from_user.id
    chat_id = query.message.chat_id if query.message else user_id

    _remind_at, _offset_min, due_at, task_text = await db.get_task_reminder_settings(user_id, task_id)
    if not due_at:
        await query.edit_message_text("У этой задачи нет дедлайна — напоминание не настроить.")
        context.user_data.pop("pending_reminder_choice", None)
        return

    if val == "off":
        cancel_task_reminder(task_id, context)
        await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=None, remind_offset_min=None)
        await query.edit_message_text("Ок, не буду напоминать по этой задаче.")
        context.user_data.pop("pending_reminder_choice", None)
        return

    try:
        offset_min = int(val)
    except Exception:
        return

    new_remind_at = _compute_remind_at_from_offset(due_at, offset_min)
    if not new_remind_at:
        await query.edit_message_text("Не смог настроить напоминание. Попробуй выбрать другую опцию.")
        return

    cancel_task_reminder(task_id, context)
    await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=new_remind_at, remind_offset_min=offset_min)
    schedule_task_reminder(
        context.job_queue,
        task_id=task_id,
        task_text=task_text or "задача",
        deadline_iso=due_at,
        chat_id=chat_id,
        remind_at_iso=new_remind_at,
    )

    await query.edit_message_text(f"Ок, напомню за {offset_min} мин.")
    context.user_data.pop("pending_reminder_choice", None)


async def on_snooze_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Пользователь нажал "Отложить ⏳" в напоминании.
    """
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    try:
        _, task_id_str = data.split(":", maxsplit=1)
        task_id = int(task_id_str)
    except Exception:
        return

    # Запоминаем контекст для ввода текстом и показываем клавиатуру выбора.
    context.user_data["pending_snooze"] = {"task_id": task_id}
    keyboard = _snooze_choice_keyboard(task_id)

    if query.message:
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception:
            await query.message.reply_text(
                "На сколько отложить напоминание?",
                reply_markup=keyboard,
            )


async def on_snooze_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Быстрое отложение из inline-кнопок напоминания.
    callback_data: snooze:{task_id}:{5|30|60}
    """
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    try:
        _, task_id_str, minutes_str = data.split(":", maxsplit=2)
        task_id = int(task_id_str)
        minutes = int(minutes_str)
    except Exception:
        return

    user_id = query.from_user.id
    chat_id = query.message.chat_id if query.message else user_id

    now = now_local()
    dt = now + timedelta(minutes=max(minutes, 0))
    remind_iso = normalize_deadline_iso(dt.isoformat())

    _remind_at, offset_min, due_at, task_text = await db.get_task_reminder_settings(user_id, task_id)
    cancel_task_reminder(task_id, context)
    await db.update_task_reminder_settings(user_id, task_id, remind_at_iso=remind_iso, remind_offset_min=offset_min)
    schedule_task_reminder(
        context.job_queue,
        task_id=task_id,
        task_text=task_text or "задача",
        deadline_iso=due_at,
        chat_id=chat_id,
        remind_at_iso=remind_iso,
    )

    if query.message:
        await query.message.reply_text(f"Ок, отложил на {minutes} мин.", reply_markup=MAIN_KEYBOARD)


# ==== ОБРАБОТКА ГОЛОСОВЫХ СООБЩЕНИЙ =====


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает голосовое сообщение:
    - скачивает файл
    - отправляет в OpenAI на транскриб
    - подменяет текст сообщения на транскриб
    - передаёт в ту же логику, что и обычный текст (handle_message).
    """
    if not update.message or not update.message.voice:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    voice = update.message.voice

    temp_path = None
    try:
        file = await context.bot.get_file(voice.file_id)
        temp_path = f"/tmp/voice_{user_id}_{voice.file_unique_id}.ogg"
        await file.download_to_drive(temp_path)

        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        await context.bot.send_message(
            chat_id=chat_id,
            text="Секунду, расшифровываю голосовое...",
            reply_markup=MAIN_KEYBOARD,
        )

        text = transcribe_audio(temp_path)
        if not text or len(text.strip()) < 2:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Не смог нормально разобрать голосовое. Попробуй ещё раз или напиши текстом 🙂",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        logger.info(
            "Whisper transcript for user %s (chat %s): %r", user_id, chat_id, text
        )

        if not ENABLE_VOICE_AUTO_HANDLE:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Я услышал из голосового:\n\n«{text}»\n\nМожешь отправить это текстом или скорректировать 🙂",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        # Создаём новый Update с текстовым сообщением, чтобы пройти обычный pipeline
        msg_dict = update.message.to_dict()
        msg_dict["text"] = text
        msg_dict.pop("voice", None)
        new_message = Message.de_json(msg_dict, context.bot)
        new_update = Update(update.update_id, message=new_message)

        await handle_message(new_update, context)

    except Exception as e:
        logger.exception("Error while processing voice message from %s: %s", user_id, e)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Что-то пошло не так с голосовым. Попробуй ещё раз или напиши текстом 🙂",
            reply_markup=MAIN_KEYBOARD,
        )
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


# ==== АДМИН-КОМАНДЫ =====

async def cmd_dumpdb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Эта команда только для админа.")
        return

    db_path = db.DB_PATH if hasattr(db, "DB_PATH") else "tasks.db"
    if not os.path.exists(db_path):
        await update.message.reply_text("Файл базы данных не найден.")
        return

    await update.message.reply_document(
        document=open(db_path, "rb"),
        filename=os.path.basename(db_path),
        caption="Дамп базы задач",
    )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Эта команда только для админа.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /broadcast текст сообщения")
        return

    text = " ".join(context.args)
    user_ids = await db.get_users_with_active_tasks()
    if not user_ids:
        await update.message.reply_text("Нет пользователей с активными задачами.")
        return

    sent = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception as e:
            logger.warning(f"Не удалось отправить broadcast пользователю {uid}: {e}")

    await update.message.reply_text(f"Broadcast отправлен {sent} пользователям.")


# ==== MAIN =====

def main():
    asyncio.run(db.init_db())

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # текстовые сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # голосовые сообщения
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))

    # inline-кнопки
    app.add_handler(CallbackQueryHandler(on_mark_done_menu, pattern=r"^mark_done_menu$"))
    app.add_handler(CallbackQueryHandler(on_mark_done_select, pattern=r"^done_task:\d+$"))
    app.add_handler(CallbackQueryHandler(on_remind_set, pattern=r"^remind_set:\d+:(?:off|0|5|30|60)$"))
    app.add_handler(CallbackQueryHandler(on_snooze_prompt, pattern=r"^snooze_prompt:\d+$"))
    app.add_handler(CallbackQueryHandler(on_snooze_quick, pattern=r"^snooze:\d+:(?:5|30|60)$"))
    app.add_handler(CallbackQueryHandler(on_clear_archive, pattern=r"^clear_archive$"))

    # команды админа
    app.add_handler(CommandHandler("dumpdb", cmd_dumpdb))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    # --- УТРЕННИЙ ДАЙДЖЕСТ 07:30 ---
    if app.job_queue:
        app.job_queue.run_daily(
            send_daily_digest,
            time=dtime(hour=7, minute=30, tzinfo=LOCAL_TZ),
            name="daily_digest",
        )
        # восстановим напоминания для задач с будущими дедлайнами (после старта event loop)
        app.job_queue.run_once(restore_reminders_job, when=0, name="restore_reminders_init")

    print("AI Smart-Tasker запущен... 🚀")
    app.run_polling()


if __name__ == "__main__":
    main()
# src/main.py
import logging
from typing import Optional
from datetime import datetime, time as dtime, timedelta, date
from zoneinfo import ZoneInfo
import difflib
import re
import os

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

from config import TELEGRAM_BOT_TOKEN, DEFAULT_TIMEZONE
from llm_client import (
    parse_user_input,
    parse_user_input_multi,
    render_user_reply,
    transcribe_audio,
)
from task_schema import TaskInterpretation
import db  

# ===== КОНСТАНТЫ =====
ADMIN_USER_ID = 6113692933
LOCAL_TZ = ZoneInfo(DEFAULT_TIMEZONE)

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


def find_task_by_hint(user_id: int, hint: str):
    """
    Пытается найти задачу по текстовой подсказке.
    Сначала точное вхождение, потом осторожный fuzzy с высоким порогом.
    """
    if not hint:
        return None

    tasks = db.get_tasks(user_id)
    hint_lower = hint.lower().strip()

    # 1) прямое вхождение подстроки — считаем уверенным совпадением
    candidates: list[tuple[int, str]] = []
    for t_id, t_text, _ in tasks:
        if hint_lower in t_text.lower():
            candidates.append((t_id, t_text))

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # потом можно сделать диалог уточнения, пока считаем, что это амбиг
        return None

    # 2) fuzzy по нормализованным словам
    hint_tokens = _tokenize_meaningful(hint_lower)
    if not hint_tokens:
        return None

    best: tuple[int, str] | None = None
    best_score = 0.0
    best_overlap = 0
    for t_id, t_text, _ in tasks:
        task_tokens = _tokenize_meaningful(t_text)
        if not task_tokens:
            continue

        overlap = len(set(hint_tokens) & set(task_tokens))
        if overlap == 0:
            continue  # нет общих смысловых слов — пропускаем

        task_join = " ".join(task_tokens)
        hint_join = " ".join(hint_tokens)
        score = difflib.SequenceMatcher(None, hint_join, task_join).ratio()
        if score > best_score:
            best_score = score
            best_overlap = overlap
            best = (t_id, t_text)

    # строгий порог уверенности
    if best and best_score >= 0.75 and best_overlap >= 1:
        return best

    return None


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
        dt = datetime.fromisoformat(deadline_iso).astimezone(LOCAL_TZ)
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return None


def filter_tasks_by_date(user_id: int, target_date) -> list[tuple[int, str, str | None]]:
    """
    Возвращает задачи, дедлайн которых совпадает с датой target_date (в локальной TZ).
    """
    tasks = db.get_tasks(user_id)
    result = []
    for t_id, text, due in tasks:
        if not due:
            continue
        try:
            dt = datetime.fromisoformat(due).astimezone(LOCAL_TZ)
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

    now = datetime.now(LOCAL_TZ)
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
    tasks = db.get_tasks(user_id)

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
                dt = datetime.fromisoformat(due).astimezone(LOCAL_TZ)
                d_str = dt.strftime("%d.%m %H:%M")
                with_due.append(f"{len(with_due) + 1}. {txt} (до {d_str})")
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
    tasks = db.get_archived_tasks(user_id)
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
                dt = datetime.fromisoformat(completed_at).astimezone(LOCAL_TZ)
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

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Выполнено ✅",
                    callback_data=f"done_task:{task_id}",
                )
            ]
        ]
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏰ Напоминание:\n\n{text}",
        reply_markup=keyboard,
    )


async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ежедневный дайджест: в 07:30 отправляет всем список активных задач.
    """
    user_ids = db.get_users_with_active_tasks()
    if not user_ids:
        return

    for uid in user_ids:
        await send_tasks_list(chat_id=uid, user_id=uid, context=context)


def schedule_task_reminder(job_queue, task_id: int, task_text: str, deadline_iso: str | None, chat_id: int):
    """
    Ставит напоминание в job_queue, если дедлайн в будущем и данные валидны.
    Используется как при создании/переносе задач, так и при восстановлении после рестарта.
    """
    if not job_queue or not deadline_iso:
        return

    try:
        dt = datetime.fromisoformat(deadline_iso).astimezone(LOCAL_TZ)
    except Exception:
        return

    now = datetime.now(LOCAL_TZ)
    if dt <= now:
        return

    delay = (dt - now).total_seconds()
    job_queue.run_once(
        send_task_reminder,
        when=delay,
        chat_id=chat_id,
        name=f"reminder:{task_id}",
        data={"task_id": task_id, "text": task_text},
    )


def restore_reminders(job_queue):
    """
    После рестарта бота восстанавливает напоминания по активным задачам с будущими дедлайнами.
    """
    if not job_queue:
        return

    now_iso = datetime.now(LOCAL_TZ).isoformat()
    tasks = db.get_active_tasks_with_future_due(now_iso)

    for task_id, user_id, text, due_at in tasks:
        schedule_task_reminder(job_queue, task_id, text, due_at, chat_id=user_id)


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
            parsed = parse_user_input(text, tasks_snapshot=db.get_tasks(user_id))
        except Exception:
            context.user_data.pop("pending_deadline", None)
            await update.message.reply_text(
                "Я не смог нормально понять срок, оставляю задачу без дедлайна.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        service_actions = {"unknown", "show_active", "show_today", "show_tomorrow", "show_date"}
        meaningful_actions = {"create", "complete", "delete", "reschedule", "rename"}

        # Вариант А: пользователь дал только дату/время → уточнение дедлайна
        if (
            parsed.deadline_iso
            and parsed.title is None
            and parsed.target_task_hint is None
            and parsed.action in service_actions
        ):
            task_id = pending["task_id"]
            task_text = pending["text"]

            db.update_task_due(user_id, task_id, parsed.deadline_iso)

            dt = datetime.fromisoformat(parsed.deadline_iso).astimezone(LOCAL_TZ)
            new_time = dt.strftime("%d.%m %H:%M")

            schedule_task_reminder(
                context.job_queue,
                task_id=task_id,
                task_text=task_text,
                deadline_iso=parsed.deadline_iso,
                chat_id=chat_id,
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
            parsed = parse_user_input(text, tasks_snapshot=db.get_tasks(user_id))
        except Exception:
            context.user_data.pop("pending_reschedule", None)
            await update.message.reply_text(
                "Не смог понять новую дату, перенос отменён.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        service_actions = {"unknown", "show_active", "show_today", "show_tomorrow", "show_date"}
        meaningful_actions = {"create", "complete", "delete", "reschedule", "rename"}

        if (
            parsed.deadline_iso
            and parsed.title is None
            and parsed.target_task_hint is None
            and parsed.action in service_actions
        ):
            task_id = pending_reschedule["task_id"]
            task_text = pending_reschedule["text"]

            cancel_task_reminder(task_id, context)
            db.update_task_due(user_id, task_id, parsed.deadline_iso)

            schedule_task_reminder(
                context.job_queue,
                task_id=task_id,
                task_text=task_text,
                deadline_iso=parsed.deadline_iso,
                chat_id=chat_id,
            )

            dt = datetime.fromisoformat(parsed.deadline_iso).astimezone(LOCAL_TZ)
            new_time = dt.strftime("%d.%m %H:%M")
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

    # --- 2. ИИ-парсинг обычного текста ---
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Быстрая эвристика: если спрашивают "что/есть ли на завтра/сегодня/конкретную дату"
    lower_text = text.lower()
    question_like = any(
        q in lower_text
        for q in ["что у меня", "что по", "есть ли", "что на", "какие задачи", "есть что-то"]
    )

    if question_like:
        now = datetime.now(LOCAL_TZ)
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
            tasks_for_day = filter_tasks_by_date(user_id, target_date)
            if tasks_for_day:
                lines = []
                for i, (tid, txt, due) in enumerate(tasks_for_day, 1):
                    try:
                        dt = datetime.fromisoformat(due).astimezone(LOCAL_TZ)
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

    tasks_snapshot = db.get_tasks(user_id)

    # --- Попытка батч-парсинга нескольких действий (create/complete/...) ---#
    ai_result: Optional[TaskInterpretation] = ai_result_preparsed
    multi_results: list[TaskInterpretation] = []

    if ai_result_preparsed is None:
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

    # Батч включаем, если есть хотя бы 1 структурированный элемент из допустимых action
    supported_actions = {"create", "complete", "reschedule", "delete", "rename"}
    if multi_results and all(m.action in supported_actions for m in multi_results):
        created_lines: list[str] = []
        completed_lines: list[str] = []
        rescheduled_lines: list[str] = []
        deleted_lines: list[str] = []
        renamed_lines: list[str] = []
        not_found_lines: list[str] = []
        needs_deadline_lines: list[str] = []
        needs_reschedule_deadline_lines: list[str] = []
        pending_deadline_data: dict | None = None
        pending_reschedule_data: dict | None = None

        for item in multi_results:
            if item.action == "create":
                task_text = item.title or item.raw_input
                task_id = db.add_task(
                    user_id,
                    task_text,
                    item.deadline_iso,
                )

                # ставим напоминание только если дедлайн есть и в будущем
                if item.deadline_iso:
                    schedule_task_reminder(
                        context.job_queue,
                        task_id=task_id,
                        task_text=task_text,
                        deadline_iso=item.deadline_iso,
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
                target = find_task_by_hint(user_id, item.target_task_hint or "")
                if not target:
                    not_found_lines.append(
                        f"• не нашёл задачу для: {item.target_task_hint or 'этого фрагмента'}"
                    )
                    continue

                task_id, task_text = target
                cancel_task_reminder(task_id, context)
                db.set_task_done(user_id, task_id)
                completed_lines.append(f"• выполнена: {task_text}")

            elif item.action == "reschedule":
                target = find_task_by_hint(user_id, item.target_task_hint or "")
                if not target:
                    not_found_lines.append(
                        f"• не нашёл задачу для переноса: {item.target_task_hint or 'этого фрагмента'}"
                    )
                    continue
                task_id, task_text = target
                if not item.deadline_iso:
                    # не стираем дедлайн, просим уточнить дату, как в single-режиме
                    if pending_reschedule_data is None:
                        pending_reschedule_data = {"task_id": task_id, "text": task_text}
                        needs_reschedule_deadline_lines.append(
                            f"• для переноса «{task_text}» укажи новую дату/время (например, «завтра 18:00» или «нет»)"
                        )
                    continue

                db.update_task_due(user_id, task_id, item.deadline_iso)
                if item.deadline_iso:
                    schedule_task_reminder(
                        context.job_queue,
                        task_id=task_id,
                        task_text=task_text,
                        deadline_iso=item.deadline_iso,
                        chat_id=chat_id,
                    )
                human_deadline = _format_deadline_human_local(item.deadline_iso)
                rescheduled_lines.append(
                    f"• перенёс: {task_text}" + (f" → {human_deadline}" if human_deadline else "")
                )

            elif item.action == "rename":
                target = find_task_by_hint(user_id, item.target_task_hint or "")
                if not target or not item.title:
                    not_found_lines.append(
                        f"• не нашёл задачу для переименования: {item.target_task_hint or 'этого фрагмента'}"
                    )
                    continue
                task_id, _task_text = target
                db.update_task_text(user_id, task_id, item.title)
                renamed_lines.append(f"• переименовал: {item.title}")

            elif item.action == "delete":
                target = find_task_by_hint(user_id, item.target_task_hint or "")
                if not target:
                    not_found_lines.append(
                        f"• не нашёл задачу для удаления: {item.target_task_hint or 'этого фрагмента'}"
                    )
                    continue
                task_id, task_text = target
                cancel_task_reminder(task_id, context)
                db.delete_task(user_id, task_id)
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

    if len(multi_results) == 1 and multi_results[0].action in supported_actions:
        ai_result = multi_results[0]

    if ai_result is None:
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

        target = find_task_by_hint(user_id, target_hint)
        if not target:
            await update.message.reply_text(
                f"🤷‍♂️ Не нашел задачу, похожую на «{target_hint or 'это'}». Попробуй точнее.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        task_id, _task_text = target
        db.update_task_text(user_id, task_id, ai_result.title)
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

        task_id = db.add_task(
            user_id,
            task_text,
            ai_result.deadline_iso,
        )

        event = {
            "type": "task_created",
            "task_text": task_text,
            "deadline_iso": ai_result.deadline_iso,
            "prev_deadline_iso": None,
            "num_active_tasks": len(db.get_tasks(user_id)),
            "language": "ru",
            "extra": {},
        }

        reply_text = safe_render_user_reply(event)

        await update.message.reply_text(
            reply_text,
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )

        # есть дедлайн → сразу ставим напоминание
        if ai_result.deadline_iso:
            schedule_task_reminder(
                context.job_queue,
                task_id=task_id,
                task_text=task_text,
                deadline_iso=ai_result.deadline_iso,
                chat_id=chat_id,
            )
            return

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
        target = find_task_by_hint(user_id, ai_result.target_task_hint or "")
        if not target:
            event = {
                "type": "task_not_found",
                "task_text": None,
                "deadline_iso": None,
                "prev_deadline_iso": None,
                "num_active_tasks": len(db.get_tasks(user_id)),
                "language": "ru",
                "extra": {"user_query": ai_result.target_task_hint},
            }
            reply_text = safe_render_user_reply(event)
            await update.message.reply_text(reply_text, reply_markup=MAIN_KEYBOARD)
            return

        task_id, task_text = target
        # отменяем напоминание, если было
        cancel_task_reminder(task_id, context)

        if ai_result.action == "complete":
            db.set_task_done(user_id, task_id)
            event = {
                "type": "task_completed",
                "task_text": task_text,
                "deadline_iso": None,
                "prev_deadline_iso": None,
                "num_active_tasks": len(db.get_tasks(user_id)),
                "language": "ru",
                "extra": {},
            }
        else:
            db.delete_task(user_id, task_id)
            event = {
                "type": "task_deleted",
                "task_text": task_text,
                "deadline_iso": None,
                "prev_deadline_iso": None,
                "num_active_tasks": len(db.get_tasks(user_id)),
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
    elif ai_result.action == "reschedule":
        target = find_task_by_hint(user_id, ai_result.target_task_hint or "")
        if not target:
            event = {
                "type": "task_not_found",
                "task_text": None,
                "deadline_iso": None,
                "prev_deadline_iso": None,
                "num_active_tasks": len(db.get_tasks(user_id)),
                "language": "ru",
                "extra": {"user_query": ai_result.target_task_hint},
            }
            reply_text = safe_render_user_reply(event)
            await update.message.reply_text(reply_text, reply_markup=MAIN_KEYBOARD)
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

        prev_task = db.get_task(user_id, task_id)
        prev_deadline = prev_task[2] if prev_task else None

        db.update_task_due(user_id, task_id, ai_result.deadline_iso)

        # ставим новое напоминание
        schedule_task_reminder(
            context.job_queue,
            task_id=task_id,
            task_text=task_text,
            deadline_iso=ai_result.deadline_iso,
            chat_id=chat_id,
        )

        event = {
            "type": "task_rescheduled",
            "task_text": task_text,
            "deadline_iso": ai_result.deadline_iso,
            "prev_deadline_iso": prev_deadline,
            "num_active_tasks": len(db.get_tasks(user_id)),
            "language": "ru",
            "extra": {},
        }
        reply_text = safe_render_user_reply(event)

        await update.message.reply_text(
            reply_text,
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )

    # ПОКАЗАТЬ ЗАДАЧИ (через текст, а не кнопку)
    elif ai_result.action in ["show_active", "show_today", "show_tomorrow", "show_date"]:
        target_date = None
        weekend_mode = False
        if ai_result.action == "show_today":
            target_date = datetime.now(LOCAL_TZ).date()
        elif ai_result.action == "show_tomorrow":
            target_date = (datetime.now(LOCAL_TZ) + timedelta(days=1)).date()
        elif ai_result.action == "show_date" and ai_result.deadline_iso:
            try:
                target_date = datetime.fromisoformat(ai_result.deadline_iso).astimezone(LOCAL_TZ).date()
            except Exception:
                target_date = None
        if ai_result.action == "show_date" and getattr(ai_result, "note", None) == "weekend":
            weekend_mode = True

        if target_date:
            if weekend_mode:
                # показываем ближайшие субботу и воскресенье
                today = datetime.now(LOCAL_TZ).date()
                weekday = today.weekday()  # 0=Mon
                days_to_sat = (5 - weekday) % 7
                days_to_sun = (6 - weekday) % 7
                sat_date = today + timedelta(days=days_to_sat)
                sun_date = today + timedelta(days=days_to_sun)

                parts = []
                for label, d in [("Суббота", sat_date), ("Воскресенье", sun_date)]:
                    tasks_for_day = filter_tasks_by_date(user_id, d)
                    if tasks_for_day:
                        lines = []
                        for i, (tid, txt, due) in enumerate(tasks_for_day, 1):
                            try:
                                dt = datetime.fromisoformat(due).astimezone(LOCAL_TZ)
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
                tasks_for_day = filter_tasks_by_date(user_id, target_date)
                if tasks_for_day:
                    lines = []
                    for i, (tid, txt, due) in enumerate(tasks_for_day, 1):
                        try:
                            dt = datetime.fromisoformat(due).astimezone(LOCAL_TZ)
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

        tasks_now = db.get_tasks(user_id)
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
            "num_active_tasks": len(db.get_tasks(user_id)),
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
    tasks = db.get_tasks(user_id)

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
    tasks = db.get_tasks(user_id)
    task_text = None
    for tid, txt, _ in tasks:
        if tid == task_id:
            task_text = txt
            break

    db.set_task_done(user_id, task_id)

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
    db.clear_archived_tasks(user_id)

    await query.edit_message_text("Архив очищен 🙂")


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
    user_ids = db.get_users_with_active_tasks()
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
    db.init_db()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # текстовые сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # голосовые сообщения
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))

    # inline-кнопки
    app.add_handler(CallbackQueryHandler(on_mark_done_menu, pattern=r"^mark_done_menu$"))
    app.add_handler(CallbackQueryHandler(on_mark_done_select, pattern=r"^done_task:\d+$"))
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
        # восстановим напоминания для задач с будущими дедлайнами
        restore_reminders(app.job_queue)

    print("AI Smart-Tasker запущен... 🚀")
    app.run_polling()


if __name__ == "__main__":
    main()
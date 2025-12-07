# src/main.py
import logging
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
import difflib
import re
import os

from telegram import (
    Update,
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
from llm_client import parse_user_input
from task_schema import TaskInterpretation
import db  # твой db.py

# ===== КОНСТАНТЫ =====
ADMIN_USER_ID = 6113692933
LOCAL_TZ = ZoneInfo(DEFAULT_TIMEZONE)

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
    "воскресенье",
    "через",
    "минут",
    "минуту",
    "час",
    "часа",
    "вечером",
    "утром",
    "днем",
    "днём",
    "ночью",
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

# ЛОГИ
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==== КЛАВИАТУРЫ =====

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["Показать задачи", "Еще"]],
    resize_keyboard=True,
)

EXTRA_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Архив задач"],
        ["Назад"],
    ],
    resize_keyboard=True,
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
        # "переименуй X в Y"
        r"переимен\w*\s+(?:задачу\s+)?\"?(.+?)\"?\s+в\s+\"?(.+?)\"?$",
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
    Сначала точное вхождение, потом похожие слова (fuzzy).
    """
    if not hint:
        return None

    tasks = db.get_tasks(user_id)
    hint_lower = hint.lower().strip()

    # 1) прямое вхождение подстроки
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
    for t_id, t_text, _ in tasks:
        task_tokens = _tokenize_meaningful(t_text)
        if not task_tokens:
            continue

        # пересечение смысловых токенов
        overlap = len(set(hint_tokens) & set(task_tokens))
        if overlap >= 2:
            return (t_id, t_text)

        # fuzzy по объединённой строке нормализованных слов
        task_join = " ".join(task_tokens)
        hint_join = " ".join(hint_tokens)
        score = difflib.SequenceMatcher(None, hint_join, task_join).ratio()
        if score > best_score:
            best_score = score
            best = (t_id, t_text)

    if best and best_score >= 0.55:
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

    return has_time_word or has_time_pattern or has_date_pattern


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

    lines: list[str] = []
    for i, (tid, txt, due) in enumerate(tasks, 1):
        if due:
            try:
                dt = datetime.fromisoformat(due).astimezone(LOCAL_TZ)
                d_str = dt.strftime("%d.%m %H:%M")
                lines.append(f"{i}. {txt} (до {d_str})")
            except Exception:
                lines.append(f"{i}. {txt}")
        else:
            lines.append(f"{i}. {txt}")

    text = "📋 <b>Твои задачи:</b>\n\n" + "\n".join(lines)

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

    # 2) отдельное сообщение, чтобы вернуть нижнюю клавиатуру
    await context.bot.send_message(
        chat_id=chat_id,
        text="Меню",
        reply_markup=MAIN_KEYBOARD,
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
        reply_markup=MAIN_KEYBOARD,
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

    if text == "Назад":
        await update.message.reply_text(
            "Возвращаюсь в главное меню.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # --- 1. Проверка: не ждём ли мы сейчас уточнение дедлайна по прошлой задаче ---
    pending = context.user_data.get("pending_deadline")
    pending_reschedule = context.user_data.get("pending_reschedule")
    if pending:
        lower = text.lower().strip()

        # 1) пользователь явно говорит, что дедлайн не нужен
        if lower in NO_DEADLINE_PHRASES:
            context.user_data.pop("pending_deadline", None)
            await update.message.reply_text(
                "Ок, оставляю задачу без дедлайна.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        # 2) сообщение похоже на дату/время → пробуем вытащить дедлайн через LLM
        if is_deadline_like(text):
            try:
                parsed = parse_user_input(text)
            except Exception:
                context.user_data.pop("pending_deadline", None)
                await update.message.reply_text(
                    "Я не смог нормально понять срок, оставляю задачу без дедлайна.",
                    reply_markup=MAIN_KEYBOARD,
                )
                return

            if parsed.deadline_iso:
                task_id = pending["task_id"]
                task_text = pending["text"]

                # обновляем дедлайн в базе
                db.update_task_due(user_id, task_id, parsed.deadline_iso)

                dt = datetime.fromisoformat(parsed.deadline_iso).astimezone(LOCAL_TZ)
                new_time = dt.strftime("%d.%m %H:%M")

                # ставим напоминание, если дедлайн в будущем
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
            else:
                # LLM не смог выдать нормальный ISO — безопасно отпускаем без дедлайна
                context.user_data.pop("pending_deadline", None)
                await update.message.reply_text(
                    "Не получилось разобрать дату, оставляю задачу без дедлайна.",
                    reply_markup=MAIN_KEYBOARD,
                )
                return

        # 3) сюда попадаем, если текст НЕ похож на ответ про срок
        #    → считаем, что пользователь уже ушёл к новой задаче
        #    старую оставляем без дедлайна и обрабатываем это сообщение как обычное
        context.user_data.pop("pending_deadline", None)
        # дальше пойдёт обычный парсинг через ИИ

    if pending_reschedule:
        lower = text.lower().strip()

        if lower in NO_DEADLINE_PHRASES:
            context.user_data.pop("pending_reschedule", None)
            await update.message.reply_text(
                "Ок, перенос отменяю, дедлайн не меняю.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        if is_deadline_like(text):
            try:
                parsed = parse_user_input(text)
            except Exception:
                context.user_data.pop("pending_reschedule", None)
                await update.message.reply_text(
                    "Не смог понять новую дату, перенос отменён.",
                    reply_markup=MAIN_KEYBOARD,
                )
                return

            if parsed.deadline_iso:
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
            else:
                context.user_data.pop("pending_reschedule", None)
                await update.message.reply_text(
                    "Не получилось разобрать дату, перенос отменён.",
                    reply_markup=MAIN_KEYBOARD,
                )
                return

        # если не похоже на дату — прекращаем режим переноса
        context.user_data.pop("pending_reschedule", None)

    # --- 2. ИИ-парсинг обычного текста ---
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Быстрая эвристика: если спрашивают "что/есть ли на завтра/сегодня"
    lower_text = text.lower()
    question_like = any(q in lower_text for q in ["что у меня", "что по", "есть ли", "что на", "какие задачи", "есть что-то"])
    if question_like and any(w in lower_text for w in ["завтра", "сегодня", "утром", "вечером", "днем", "днём"]):
        target_date = None
        now = datetime.now(LOCAL_TZ)
        if "завтра" in lower_text:
            target_date = (now + timedelta(days=1)).date()
        elif "сегодня" in lower_text:
            target_date = now.date()
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

    try:
        ai_result: TaskInterpretation = parse_user_input(text)
    except Exception as e:
        await update.message.reply_text(
            f"🤯 Мозг сломался: {e}",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # --- 3. Попытка распознать переименование задачи (пока без отдельного action) ---
    rename_intent = detect_rename_intent(text)
    if rename_intent:
        target_hint = (
            rename_intent["old_hint"]
            or ai_result.target_task_hint
            or ai_result.title
            or ""
        )
        target = find_task_by_hint(user_id, target_hint)
        if not target:
            await update.message.reply_text(
                f"🤷‍♂️ Не нашел задачу, похожую на «{target_hint or 'это'}». Попробуй точнее.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        task_id, _task_text = target
        new_title = rename_intent["new_title"]
        db.update_task_text(user_id, task_id, new_title)
        await update.message.reply_text(
            f"✏️ Переименовал задачу: <b>{new_title}</b>",
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

        response = f"✅ <b>Создано:</b> {task_text}"
        # есть дедлайн → сразу показываем и ставим напоминание
        if ai_result.deadline_iso:
            dt = datetime.fromisoformat(ai_result.deadline_iso).astimezone(LOCAL_TZ)
            date_str = dt.strftime("%d.%m %H:%M")
            response += f"\n⏰ <b>Дедлайн:</b> {date_str}"

            schedule_task_reminder(
                context.job_queue,
                task_id=task_id,
                task_text=task_text,
                deadline_iso=ai_result.deadline_iso,
                chat_id=chat_id,
            )

            await update.message.reply_text(
                response,
                parse_mode="HTML",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        # дедлайна нет → включаем режим уточнения
        await update.message.reply_text(
            response
            + "\n\n"
            + "🕒 Хочешь указать, к какому сроку это сделать?\n"
              "• Можешь ответить так: «завтра», «в понедельник», «завтра в 18:00».\n"
              "• Если дедлайн не нужен — напиши «без дедлайна» или «нет».",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )

        # запоминаем, по какой задаче мы ждём срок
        context.user_data["pending_deadline"] = {
            "task_id": task_id,
            "text": task_text,
        }
        return

    # ВЫПОЛНЕНИЕ / УДАЛЕНИЕ
    elif ai_result.action in ["complete", "delete"]:
        target = find_task_by_hint(user_id, ai_result.target_task_hint or "")
        if not target:
            await update.message.reply_text(
                f"🤷‍♂️ Не нашел задачу, похожую на «{ai_result.target_task_hint}». Попробуй точнее.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        task_id, task_text = target
        # отменяем напоминание, если было
        cancel_task_reminder(task_id, context)

        if ai_result.action == "complete":
            db.set_task_done(user_id, task_id)
            await update.message.reply_text(
                f"👍 Отметил выполненным: <b>{task_text}</b>",
                parse_mode="HTML",
                reply_markup=MAIN_KEYBOARD,
            )
        else:
            db.delete_task(user_id, task_id)
            await update.message.reply_text(
                f"🗑 Удалил задачу: <b>{task_text}</b>",
                parse_mode="HTML",
                reply_markup=MAIN_KEYBOARD,
            )

    # ПЕРЕНОС
    elif ai_result.action == "reschedule":
        target = find_task_by_hint(user_id, ai_result.target_task_hint or "")
        if not target:
            await update.message.reply_text(
                f"🤷‍♂️ Не нашел задачу «{ai_result.target_task_hint}» для переноса.",
                reply_markup=MAIN_KEYBOARD,
            )
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

        db.update_task_due(user_id, task_id, ai_result.deadline_iso)

        dt = datetime.fromisoformat(ai_result.deadline_iso).astimezone(LOCAL_TZ)
        new_time = dt.strftime("%d.%m %H:%M")

        # ставим новое напоминание
        schedule_task_reminder(
            context.job_queue,
            task_id=task_id,
            task_text=task_text,
            deadline_iso=ai_result.deadline_iso,
            chat_id=chat_id,
        )

        await update.message.reply_text(
            f"🔄 Перенес «{task_text}» на <b>{new_time}</b>",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )

    # ПОКАЗАТЬ ЗАДАЧИ (через текст, а не кнопку)
    elif ai_result.action in ["show_active", "show_today"]:
        # фильтр по "today" сделаем позже, пока просто все активные
        await send_tasks_list(chat_id, user_id, context)

    # НЕПОНЯТНО
    elif ai_result.action == "unknown":
        await update.message.reply_text(
            "Я умею только в задачи. Попроси меня напомнить о чем-нибудь! 🤖",
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

    # inline-кнопки
    app.add_handler(CallbackQueryHandler(on_mark_done_menu, pattern=r"^mark_done_menu$"))
    app.add_handler(CallbackQueryHandler(on_mark_done_select, pattern=r"^done_task:\d+$"))

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

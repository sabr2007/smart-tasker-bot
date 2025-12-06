# src/main.py
import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import difflib
import re

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
    filters,
    ContextTypes,
)

from config import TELEGRAM_BOT_TOKEN, DEFAULT_TIMEZONE
from llm_client import parse_user_input
from task_schema import TaskInterpretation
import db  # твой db.py

# ЛОГИ
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo(DEFAULT_TIMEZONE)

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
        r"(ому|ему|ого|ими|ыми|ами|ях|ах|ам|ой|ый|ий|ая|ое|ые|ую|ом|ев|ов|ей|ам|ами|ях)$",
        "",
        w,
    )


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
    hint_norm = _normalize_ru_word(hint_lower)
    if not hint_norm:
        return None

    best: tuple[int, str] | None = None
    best_score = 0.0

    for t_id, t_text, _ in tasks:
        words = re.findall(r"\w+", t_text.lower())
        for w in words:
            w_norm = _normalize_ru_word(w)
            if not w_norm:
                continue
            score = difflib.SequenceMatcher(None, hint_norm, w_norm).ratio()
            if score > best_score:
                best_score = score
                best = (t_id, t_text)

    if best and best_score >= 0.7:
        return best

    return None


# ==== ОТДЕЛЬНЫЕ ХЭЛПЕРЫ ДЛЯ ВЫВОДА СПИСКОВ =====

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

    # Кнопка "Выполнено" — используем уже существующий обработчик done_task
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
        # личный чат в TG = user_id
        await send_tasks_list(chat_id=uid, user_id=uid, context=context)


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

    # --- 1. ИИ-парсинг обычного текста ---
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        ai_result: TaskInterpretation = parse_user_input(text)
    except Exception as e:
        await update.message.reply_text(
            f"🤯 Мозг сломался: {e}",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # --- 2. Маршрутизация действий ---

    # СОЗДАНИЕ
    if ai_result.action == "create":
        task_id = db.add_task(
            user_id,
            ai_result.title or ai_result.raw_input,
            ai_result.deadline_iso,
        )

        response = f"✅ <b>Создано:</b> {ai_result.title or ai_result.raw_input}"
        if ai_result.deadline_iso:
            dt = datetime.fromisoformat(ai_result.deadline_iso).astimezone(LOCAL_TZ)
            date_str = dt.strftime("%d.%m %H:%M")
            response += f"\n⏰ <b>Дедлайн:</b> {date_str}"

            # --- ставим напоминание, если дедлайн в будущем ---
            now = datetime.now(LOCAL_TZ)
            if context.job_queue and dt > now:
                delay = (dt - now).total_seconds()
                context.job_queue.run_once(
                    send_task_reminder,
                    when=delay,
                    chat_id=chat_id,
                    name=f"reminder:{task_id}",
                    data={"task_id": task_id, "text": ai_result.title or ai_result.raw_input},
                )

        await update.message.reply_text(
            response,
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )


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
        # --- отменяем напоминание, если было ---
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
                "🤔 Я понял, что надо перенести, но не понял НА КОГДА.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        # снимаем старое напоминание
        cancel_task_reminder(task_id, context)

        db.update_task_due(user_id, task_id, ai_result.deadline_iso)

        dt = datetime.fromisoformat(ai_result.deadline_iso).astimezone(LOCAL_TZ)
        new_time = dt.strftime("%d.%m %H:%M")

        # ставим новое напоминание
        now = datetime.now(LOCAL_TZ)
        if context.job_queue and dt > now:
            delay = (dt - now).total_seconds()
            context.job_queue.run_once(
                send_task_reminder,
                when=delay,
                chat_id=chat_id,
                name=f"reminder:{task_id}",
                data={"task_id": task_id, "text": task_text},
            )

        await update.message.reply_text(
            f"🔄 Перенес «{task_text}» на <b>{new_time}</b>",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )

    # ПОКАЗАТЬ ЗАДАЧИ (через текст, а не кнопку)
    elif ai_result.action in ["show_active", "show_today"]:
        # пока без фильтрации по "today" — просто выводим активные
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


# ==== MAIN =====

def main():
    db.init_db()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # текстовые сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # inline-кнопки
    app.add_handler(CallbackQueryHandler(on_mark_done_menu, pattern=r"^mark_done_menu$"))
    app.add_handler(CallbackQueryHandler(on_mark_done_select, pattern=r"^done_task:\d+$"))

    # --- УТРЕННИЙ ДАЙДЖЕСТ 07:30 ---
    if app.job_queue:
        app.job_queue.run_daily(
            send_daily_digest,
            time=dtime(hour=7, minute=30, tzinfo=LOCAL_TZ),
            name="daily_digest",
        )

    print("AI Smart-Tasker запущен... 🚀")
    app.run_polling()


if __name__ == "__main__":
    main()


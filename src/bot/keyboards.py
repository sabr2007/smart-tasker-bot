# src/bot/keyboards.py
"""All keyboard definitions for the Telegram bot."""

from telegram import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config import WEBAPP_URL


# ==== ГЛАВНАЯ КЛАВИАТУРА =====

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [
            KeyboardButton("Покажи задачи"),
            KeyboardButton("Открыть панель задач", web_app=WebAppInfo(url=WEBAPP_URL)),
        ],
    ],
    resize_keyboard=True,
)


# ==== INLINE КЛАВИАТУРЫ ДЛЯ SNOOZE =====

def snooze_keyboard(task_id: int) -> InlineKeyboardMarkup:
    """Клавиатура напоминания: Выполнено / Отложить."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Выполнено ✅", callback_data=f"done_task:{task_id}"),
                InlineKeyboardButton("Отложить ⏳", callback_data=f"snooze_prompt:{task_id}"),
            ],
        ]
    )


def snooze_choice_keyboard(task_id: int) -> InlineKeyboardMarkup:
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


# ==== INLINE КЛАВИАТУРЫ ДЛЯ СПИСКА ЗАДАЧ =====

def mark_done_menu_keyboard() -> InlineKeyboardMarkup:
    """Кнопка под списком задач."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Отметить задачу выполненной",
                    callback_data="mark_done_menu",
                )
            ]
        ]
    )


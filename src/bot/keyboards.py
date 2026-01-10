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
            KeyboardButton("📋 Панель задач", web_app=WebAppInfo(url=WEBAPP_URL)),
        ],
    ],
    resize_keyboard=True,
)


# ==== INLINE КЛАВИАТУРЫ ДЛЯ SNOOZE =====

def snooze_keyboard(task_id: int) -> InlineKeyboardMarkup:
    """Клавиатура напоминания: один ряд с быстрыми действиями."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Готово ✓", callback_data=f"done_task:{task_id}"),
                InlineKeyboardButton("+15м", callback_data=f"snooze:{task_id}:15"),
                InlineKeyboardButton("+1ч", callback_data=f"snooze:{task_id}:60"),
                InlineKeyboardButton("Завтра", callback_data=f"snooze:{task_id}:tomorrow"),
            ],
        ]
    )


def snooze_choice_keyboard(task_id: int) -> InlineKeyboardMarkup:
    """Резервная клавиатура (для совместимости)."""
    return snooze_keyboard(task_id)


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


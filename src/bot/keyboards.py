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


# ==== INLINE КЛАВИАТУРЫ ДЛЯ НАПОМИНАНИЙ =====

def reminder_compact_keyboard(task_id: int) -> InlineKeyboardMarkup:
    """Компактная клавиатура: только кнопка изменения."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Изменить", callback_data=f"remind_expand:{task_id}"),
            ]
        ]
    )


def reminder_choice_keyboard(task_id: int) -> InlineKeyboardMarkup:
    """Полная клавиатура выбора времени."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("В дедлайн", callback_data=f"remind_set:{task_id}:0"),
                InlineKeyboardButton("За 15 мин", callback_data=f"remind_set:{task_id}:15"),
            ],
            [
                InlineKeyboardButton("За 1 час", callback_data=f"remind_set:{task_id}:60"),
                InlineKeyboardButton("За 3 часа", callback_data=f"remind_set:{task_id}:180"),
            ],
            [
                InlineKeyboardButton("За 24 часа", callback_data=f"remind_set:{task_id}:1440"),
                InlineKeyboardButton("Без напоминания", callback_data=f"remind_set:{task_id}:off"),
            ],
        ]
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


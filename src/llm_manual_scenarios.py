# tests/llm_manual_scenarios.py

"""
Локальный прогон LLM-парсера без Telegram.

Запуск:
    python -m tests.llm_manual_scenarios

Что делает:
- Берёт набор фиктивных задач (как будто они уже есть у пользователя).
- Прогоняет по 30–40 фраз пользователя через:
    - parse_user_input_multi (батч-парсер),
    - parse_user_input (обычный парсер).
- Печатает структурированный результат в консоль.

Цель:
- Увидеть, где парсер ошибается:
    - путает create/reschedule/complete/delete/show/unknown;
    - не ставит дедлайны;
    - не понимает “добавь дедлайн”;
    - не делит много задач из одного сообщения;
    - странно трактует планы/факты (“надо” vs “сделал”).
"""

from __future__ import annotations

import os
import json
from dataclasses import dataclass
from datetime import datetime
from typing import List, Tuple, Optional

from llm_client import (
    client,
    OPENAI_MODEL,
    LOCAL_TZ,
    _format_tasks_for_prompt,
    build_system_prompt,
    build_system_prompt_multi,
    parse_user_input,
    parse_user_input_multi,
)


# === Фиктивный список задач, как будто это db.get_tasks(user_id) ===
# Формат тот же: List[Tuple[id, text, due_at_iso]]
TASKS_SNAPSHOT: List[Tuple[int, str, Optional[str]]] = [
    (1, "калкулус 2 онлайн", "2025-12-09T09:55:00+05:00"),
    (2, "проконтролировать распечатки", "2025-12-09T11:00:00+05:00"),
    (3, "тимбилдинг правления", "2025-12-09T14:00:00+05:00"),
    (4, "провести игру", "2025-12-13T15:30:00+05:00"),
    (5, "купить молоко", None),
    (6, "Подготовить презентацию про организацию", "2025-12-12T00:00:00+05:00"),
    (7, "Проверить, как делается распределение на игру", None),
]


@dataclass
class Scenario:
    id: str
    text: str


MULTI_SCENARIOS: List[Scenario] = [
    Scenario(
        id="multi_01_create_rename_reschedule",
        text="создай задачу купить HDMI кабель завтра вечером; переименуй задачу «калкулус 2 онлайн» в «лекция по калкулусу»; перенеси задачу про распечатки на пятницу 10:00",
    ),
    Scenario(
        id="multi_02_add_deadline_and_remove",
        text="добавь дедлайн к задаче про тимбилдинг правления на субботу 14:00; убери дедлайн у задачи «провести игру»; перенеси задачу про презентацию на вторник 11:30",
    ),
    Scenario(
        id="multi_03_create_and_complete",
        text="создай задачу заказать мерч к понедельнику; я уже проверил распределение на игру; удали задачу про старый чат с участниками",
    ),
    Scenario(
        id="multi_04_rename_and_set_deadline",
        text="переименуй задачу «Подготовить презентацию про организацию» в «слайды для правления»; добавь дедлайн к задаче про игру на воскресенье 16:00; создай задачу протестировать звук сегодня днём",
    ),
    Scenario(
        id="multi_05_shift_and_remove_deadline",
        text="сдвинь дедлайн по задаче «калкулус 2 онлайн» на пятницу 09:00; убери дедлайн по задаче про распечатки; создай задачу купить призы к субботе утром",
    ),
    Scenario(
        id="multi_06_two_renames_one_create",
        text="переименуй задачу «голосование для стажеров» в «опрос стажёров»; переименуй задачу «Добавить капитанов в чаты» в «добавить капитанов в Telegram»; создай задачу обновить правила игры к среде",
    ),
    Scenario(
        id="multi_07_complete_reschedule_rename",
        text="я провёл тимбилдинг; перенеси задачу «провести игру» на субботу 18:00; переименуй задачу «Проверить, как делается распределение на игру» в «проверить распределение ролей»",
    ),
    Scenario(
        id="multi_08_add_deadline_and_create",
        text="поставь дедлайн задаче про голосование для стажеров на пятницу 20:00; создай задачу подготовить вопросы для опроса сегодня вечером; создай задачу проверить микрофоны к субботе 12:00",
    ),
    Scenario(
        id="multi_09_remove_deadline_and_complete",
        text="убери дедлайн у задачи «Написать полную повестку собрания»; отметь задачу «Заказать подарки» выполненной; создай задачу отправить благодарности спикерам завтра утром",
    ),
    Scenario(
        id="multi_10_reschedule_add_deadline_delete",
        text="перенеси задачу «голосование в группе для стажеров» на четверг 19:00; добавь дедлайн к задаче «скачать игру» на сегодня 22:00; удали задачу про старый план тимбилдинга",
    ),
    Scenario(
        id="multi_11_mixed_relative_times",
        text="перенеси задачу про калкулус на послезавтра утром; добавь дедлайн к задаче про тимбилдинг через два дня вечером; создай задачу собрать обратную связь через час",
    ),
    Scenario(
        id="multi_12_rename_and_clear_deadline",
        text="переименуй задачу «провести интеллектум игру» в «интеллектум для стажёров»; убери дедлайн у задачи «голосование для стажеров»; создай задачу распечатать бейджи завтра днём",
    ),
    Scenario(
        id="multi_13_complete_and_add_deadline",
        text="закрыл задачу «Добавить капитанов в чаты»; поставь дедлайн задаче «Провести контроль стажеров» на пятницу 18:30; создай задачу проверить фотоотчёт к воскресенью",
    ),
    Scenario(
        id="multi_14_create_reschedule_delete",
        text="создай задачу обновить шаблон повестки к вторнику; перенеси задачу «Провести собрание» на субботу 13:00; удали задачу про старую рассылку",
    ),
    Scenario(
        id="multi_15_deadline_add_remove_rename",
        text="добавь дедлайн к задаче «скачать игру» на завтра 09:00; убери дедлайн у задачи «все придуманные мною задачи для телеграм-бота. дедлайн до 11 декабря.»; переименуй задачу «провести тимбилдинг» в «тимбилдинг с наставниками»",
    ),
]


SINGLE_SCENARIOS: List[Scenario] = [
    Scenario(id="single_01_create_deadline", text="надо купить штатив завтра к 19:00"),
    Scenario(id="single_02_create_no_deadline", text="добавь задачу проверить свет в переговорке"),
    Scenario(id="single_03_add_deadline", text="поставь дедлайн задаче про презентацию на вторник 11:00"),
    Scenario(id="single_04_remove_deadline", text="убери дедлайн у задачи про распечатки"),
    Scenario(id="single_05_reschedule", text="перенеси задачу «Провести собрание» на субботу 15:00"),
    Scenario(id="single_06_complete", text="я уже сделал задачу по контролю стажеров"),
    Scenario(id="single_07_delete", text="удали задачу про старую рассылку"),
    Scenario(id="single_08_rename", text="переименуй задачу «голосование для стажеров» в «опрос стажёров онлайн»"),
    Scenario(id="single_09_rename_no_quotes", text="измени название задачи про калкулус на «подготовка к коллокуиму»"),
    Scenario(id="single_10_add_deadline_words", text="добавь срок к задаче про тимбилдинг на пятницу вечером"),
    Scenario(id="single_11_remove_deadline_words", text="сними срок с задачи про игру, пусть будет без дедлайна"),
    Scenario(id="single_12_reschedule_relative", text="сдвинь задачу про повестку на послезавтра утром"),
    Scenario(id="single_13_reschedule_exact", text="перенеси задачу про презентацию на 25 декабря 09:30"),
    Scenario(id="single_14_create_relative_minutes", text="через 15 минут напомни отправить отчёт"),
    Scenario(id="single_15_create_evening", text="создай задачу собрать отзывы сегодня вечером"),
    Scenario(id="single_16_complete_with_hint", text="задачу про скачивание игры сделал, отметь выполненной"),
    Scenario(id="single_17_delete_with_hint", text="убери задачу про покупку призов"),
    Scenario(id="single_18_add_deadline_weekday", text="добавь дедлайн к задаче про игру на воскресенье 13:00"),
    Scenario(id="single_19_remove_deadline_plain", text="оставь задачу про повестку без срока"),
    Scenario(id="single_20_rename_short", text="rename task про повестку на финальную повестку"),
    Scenario(id="single_21_create_no_time", text="создай задачу заказать воду для мероприятия"),
    Scenario(id="single_22_create_with_date_only", text="добавь задачу собрать бюджеты к 28 декабря"),
    Scenario(id="single_23_reschedule_remove_to_null", text="убери дедлайн у задачи «Провести контроль стажеров»"),
    Scenario(id="single_24_add_deadline_time_only", text="поставь задаче про тимбилдинг время в 18:45"),
    Scenario(id="single_25_complete_done", text="задачу «Написать полную повестку собрания» я уже сделал"),
]


def print_task_snapshot(out) -> None:
    print("=" * 80, file=out)
    print("ТЕКУЩИЙ СНИМОК ЗАДАЧ (TASKS_SNAPSHOT):", file=out)
    for tid, txt, due in TASKS_SNAPSHOT:
        print(f"- [{tid}] {txt!r}  due_at={due}", file=out)
    print("=" * 80, file=out)
    print("", file=out)


def call_model_multi_raw(user_text: str, tasks_snapshot) -> dict:
    now = datetime.now(LOCAL_TZ)
    now_str = now.isoformat()
    tasks_block = _format_tasks_for_prompt(tasks_snapshot)
    system_prompt = build_system_prompt_multi(now_str, tasks_block, max_items=5)

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
    )

    raw = response.choices[0].message.content
    return json.loads(raw)


def call_model_single_raw(user_text: str, tasks_snapshot) -> dict:
    now = datetime.now(LOCAL_TZ)
    now_str = now.isoformat()
    tasks_block = _format_tasks_for_prompt(tasks_snapshot)
    system_prompt = build_system_prompt(now_str, tasks_block)

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
    )

    raw = response.choices[0].message.content
    return json.loads(raw)


def run_multi_scenarios(tasks_snapshot, out) -> None:
    total = len(MULTI_SCENARIOS)
    for idx, sc in enumerate(MULTI_SCENARIOS, start=1):
        print(f"[{idx}/{total}]", file=out)
        print("-" * 80, file=out)
        print(f"Сценарий: {sc.id}", file=out)
        print(f"Текст: {sc.text!r}\n", file=out)

        # RAW multi
        try:
            raw_multi = call_model_multi_raw(sc.text, tasks_snapshot)
            print("RAW MODEL (multi):", file=out)
            print(json.dumps(raw_multi, ensure_ascii=False, indent=2), file=out)
        except Exception as e:
            print(f"RAW MODEL (multi) ERROR: {e}", file=out)

        # Parsed multi
        try:
            multi_results = parse_user_input_multi(
                sc.text,
                tasks_snapshot=tasks_snapshot,
                max_items=5,
            )
            print("\nparse_user_input_multi →", len(multi_results), "items:", file=out)
            for i, item in enumerate(multi_results, start=1):
                print(f"  [{i}] {item!r}", file=out)
        except Exception as e:
            print(f"parse_user_input_multi ERROR: {e}", file=out)

        # Single on the same text (for reference)
        try:
            single_result = parse_user_input(
                sc.text,
                tasks_snapshot=tasks_snapshot,
            )
            print("\nОдин результат parse_user_input:", file=out)
            print(f"  {single_result!r}", file=out)
        except Exception as e:
            print(f"parse_user_input (single) ERROR: {e}", file=out)

        print("", file=out)


def run_single_scenarios(tasks_snapshot, out) -> None:
    total = len(SINGLE_SCENARIOS)
    for idx, sc in enumerate(SINGLE_SCENARIOS, start=1):
        print(f"[{idx}/{total}]", file=out)
        print("-" * 80, file=out)
        print(f"Single-сценарий: {sc.id}", file=out)
        print(f"Текст: {sc.text!r}\n", file=out)

        # RAW single
        try:
            raw_single = call_model_single_raw(sc.text, tasks_snapshot)
            print("RAW MODEL (single):", file=out)
            print(json.dumps(raw_single, ensure_ascii=False, indent=2), file=out)
        except Exception as e:
            print(f"RAW MODEL (single) ERROR: {e}", file=out)

        # Parsed single
        try:
            res = parse_user_input(sc.text, tasks_snapshot=tasks_snapshot)
            print("\nparse_user_input →", file=out)
            print(f"  {res!r}", file=out)
        except Exception as e:
            print(f"parse_user_input ERROR: {e}", file=out)

        print("", file=out)


def main() -> None:
    os.makedirs("test_runs", exist_ok=True)
    outfile = os.path.join("test_runs", f"tests_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")

    with open(outfile, "w", encoding="utf-8") as out:
        print(f"Результаты будут сохранены в {outfile}", file=out)
        print("", file=out)

        print_task_snapshot(out)

        print(f"Запускаю {len(MULTI_SCENARIOS)} multi-сценариев...", file=out)
        run_multi_scenarios(TASKS_SNAPSHOT, out)

        print("\n" + "=" * 80, file=out)
        print(f"Запускаю {len(SINGLE_SCENARIOS)} single-сценариев...", file=out)
        run_single_scenarios(TASKS_SNAPSHOT, out)

        print("Готово. Посмотри глазами, где парсер ведёт себя странно ✨", file=out)


if __name__ == "__main__":
    main()

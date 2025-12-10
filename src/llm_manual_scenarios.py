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
        id="multi_01_three_creates_simple",
        text="создай: купить молоко завтра вечером; дописать конспект по калкулусу к пятнице; подготовить план игры к субботе 12:00",
    ),
    Scenario(
        id="multi_02_numbered_creates",
        text="1) выучить 20 слов по английскому к среде; 2) разобрать папку с документами в воскресенье днём; 3) дописать отчёт по истории до понедельника",
    ),
    Scenario(
        id="multi_03_mix_create_complete",
        text="я доделал конспект по истории; закрыл задачу с регистрацией на игру; добавь задачу подготовить вопросы для правления к среде 21:00",
    ),
    Scenario(
        id="multi_04_two_reschedule_one_create",
        text="перенеси задачу про тимбилдинг правления на следующую субботу 14:00; перенеси задачу про распечатки на завтра к 10:30; создай задачу продумать тайминг игры до пятницы",
    ),
    Scenario(
        id="multi_05_delete_and_create",
        text="удали задачу про старый проект по боту; удали задачу про старый тимбилдинг; добавь задачу протестировать новую версию бота сегодня ночью",
    ),
    Scenario(
        id="multi_06_reschedule_existing_and_new",
        text="перенеси «калкулус 2 онлайн» на завтра в 08:30; перенеси задачу «провести игру» на воскресенье 16:00; создай задачу купить призы к субботе",
    ),
    Scenario(
        id="multi_07_complete_and_create",
        text="презентацию про организацию сделал; распечатки тоже проконтролировал; добавь задачу выложить пост про организацию в инсту к четвергу 19:00",
    ),
    Scenario(
        id="multi_08_three_creates_parts_of_day",
        text="сделать уборку в комнате сегодня вечером; собрать вещи на выезд в пятницу утром; приготовить список продуктов к воскресенью днём",
    ),
    Scenario(
        id="multi_09_two_reschedules_one_delete",
        text="перенеси задачу про распределение на игру на понедельник 13:00; перенеси дедлайн по презентации правления на вторник 11:00; удали задачу про старый чат с участниками",
    ),
    Scenario(
        id="multi_10_complete_complete_create",
        text="я сходил на тимбилдинг правления; провёл игру для первокурсников; добавь задачу собрать обратную связь от ребят до субботы",
    ),
    Scenario(
        id="multi_11_creates_with_relative_time",
        text="создай: разобрать почту через час; написать план на неделю сегодня днём; подготовить список покупок через два дня к вечеру",
    ),
    Scenario(
        id="multi_12_mixed_weekdays",
        text="сделать черновик отчёта к среде; купить продукты к четвергу 20:00; позвонить бабушке в воскресенье вечером",
    ),
    Scenario(
        id="multi_13_two_resched_with_dates",
        text="перенеси дедлайн по курсовой на 25 декабря 23:59; сдвинь задачу по мидтерму на 10 января 09:00; добавь задачу найти статьи в библиотеке завтра утром",
    ),
    Scenario(
        id="multi_14_two_deletes_one_create",
        text="удали задачу про покупку молока; удали задачу про старые распечатки; создай задачу купить молоко и хлеб к субботе утром",
    ),
    Scenario(
        id="multi_15_rename_and_two_creates",
        text="переименуй задачу «Подготовить презентацию про организацию» в «Презентация для правления»; добавь задачу проверить слайды завтра вечером; добавь задачу репетировать речь в субботу днём",
    ),
    Scenario(
        id="multi_16_resched_calc_and_game",
        text="перенеси задачу по «калкулус 2 онлайн» на пятницу 09:00; добавь задачу повторить лекцию по линейной алгебре в четверг вечером; перенеси задачу про игру на субботу 18:00",
    ),
    Scenario(
        id="multi_17_three_creates_no_time",
        text="создай: записаться к врачу до понедельника; подготовить список вопросов к встрече до вторника; собрать документы к пятнице",
    ),
    Scenario(
        id="multi_18_complete_reschedule_create",
        text="я сделал задачу «Проверить, как делается распределение на игру»; перенеси дедлайн по презентации про организацию на понедельник 10:00; добавь задачу проверить звук и проектор к субботе",
    ),
    Scenario(
        id="multi_19_two_resched_existing",
        text="перенеси задачу «купить молоко» на завтра утром; перенеси задачу «проконтролировать распечатки» на сегодня к 13:00; добавь задачу заказать ещё бумаги до конца недели",
    ),
    Scenario(
        id="multi_20_completes_and_delete",
        text="закрыл все задачи по распечаткам; тимбилдинг правления прошёл; удали задачу про старый план тимбилдинга",
    ),
]


SINGLE_SCENARIOS: List[Scenario] = [
    Scenario(
        id="single_01_create_with_time",
        text="надо купить молоко завтра в 19:00",
    ),
    Scenario(
        id="single_02_create_no_deadline",
        text="нужно придумать вопросы для следующей игры",
    ),
    Scenario(
        id="single_03_complete_presentation",
        text="я сделал презентацию про организацию",
    ),
    Scenario(
        id="single_04_reschedule_prints",
        text="перенеси задачу про распечатки на завтра утром",
    ),
    Scenario(
        id="single_05_delete_teambuilding",
        text="удали задачу про тимбилдинг правления",
    ),
    Scenario(
        id="single_06_create_bank_visit",
        text="добавь задачу сходить в банк в пятницу днём",
    ),
    Scenario(
        id="single_07_create_accounting",
        text="надо к понедельнику собрать все чеки и отправить отчёт в бухгалтерию",
    ),
    Scenario(
        id="single_08_create_midterm",
        text="хочу подготовиться к мидтерму по калкулусу к воскресенью вечером",
    ),
    Scenario(
        id="single_09_complete_linear_algebra",
        text="домашку по линейной алгебре уже сделал, можешь отметить выполненной",
    ),
    Scenario(
        id="single_10_resched_coursework",
        text="сдвинь дедлайн по курсовой на 15 декабря",
    ),
    Scenario(
        id="single_11_create_passport",
        text="добавь задачу забрать паспорт 9 декабря днём",
    ),
    Scenario(
        id="single_12_create_relative_minutes",
        text="через 20 минут надо вспомнить про созвон с куратором",
    ),
    Scenario(
        id="single_13_delete_milk_task",
        text="удали задачу про купить молоко из списка",
    ),
    Scenario(
        id="single_14_resched_calc2",
        text="перенеси «калкулус 2 онлайн» на субботу 10:00",
    ),
    Scenario(
        id="single_15_complete_distribution",
        text="сделал задачу по распределению на игру, пометь её выполненной",
    ),
    Scenario(
        id="single_16_create_gym",
        text="добавь задачу сходить в спортзал завтра вечером",
    ),
    Scenario(
        id="single_17_resched_game",
        text="сдвинь задачу про игру на следующую среду 18:30",
    ),
    Scenario(
        id="single_18_create_history_report",
        text="надо к пятнице подготовить отчёт по истории",
    ),
    Scenario(
        id="single_19_complete_notes",
        text="я наконец-то доделал конспект по лекции, можно закрыть задачу",
    ),
    Scenario(
        id="single_20_delete_old_bot_project",
        text="удали задачу про старый проект по боту",
    ),
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

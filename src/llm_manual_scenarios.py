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
from datetime import datetime
from typing import List, Tuple, Optional, Callable

from llm_client import parse_user_input, parse_user_input_multi
from task_schema import TaskInterpretation


# === Фиктивный список задач, как будто это db.get_tasks(user_id) ===
# Формат тот же: List[Tuple[id, text, due_at_iso]]
TASKS_SNAPSHOT: List[Tuple[int, str, Optional[str]]] = [
    (1, "калкулус 2 онлайн.", "2025-12-09T09:55:00+05:00"),
    (2, "проконтролировать распечатки", "2025-12-09T11:00:00+05:00"),
    (3, "тимбилдинг правления", "2025-12-09T14:00:00+05:00"),
    (4, "провести игру", "2025-12-13T15:30:00+05:00"),
    (5, "купить молоко", None),
    (6, "Подготовить презентацию про организацию", "2025-12-12T00:00:00+05:00"),
    (7, "Проверить, как делается распределение на игру", None),
]


class Scenario:
    def __init__(
        self,
        name: str,
        text: str,
        use_multi: bool = True,
    ) -> None:
        self.name = name
        self.text = text
        self.use_multi = use_multi


SCENARIOS: List[Scenario] = [
    # === CREATE (single) ===
    Scenario(name="create_1_no_deadline", text="купить хлеб"),
    Scenario(name="create_2_today_time", text="в 18:00 встреча с Димой"),
    Scenario(name="create_3_friday_date_only", text="надо сдать отчёт по истории в пятницу"),
    Scenario(name="create_4_tomorrow_morning", text="завтра утром нужно дописать конспект"),
    Scenario(name="create_5_en_schedule_zoom", text="schedule a zoom call tomorrow at 10am"),
    Scenario(name="create_6_typo_report", text="надо дсделать отчот по матану до завтра"),
    Scenario(name="create_7_evening_run", text="вечером пробежать 5 километров"),
    Scenario(name="create_8_daytime_clean", text="в субботу днём уборка квартиры"),
    Scenario(name="create_9_k_monday_9", text="надо приготовить презентацию к понедельнику 9:00"),
    Scenario(name="create_10_k_friday", text="к пятнице подготовить план встречи"),
    Scenario(name="create_11_in_three_days", text="через три дня закончить шпаргалку"),
    Scenario(name="create_12_wed_evening", text="подготовить доклад до среды вечером"),
    Scenario(name="create_13_monday_book_hall", text="в понедельник забронировать зал"),
    Scenario(name="create_14_day_after_tomorrow_morning", text="послезавтра в 7 утра пробежка"),
    Scenario(name="create_15_wed_morning_call", text="в среду утром созвон с командой"),
    Scenario(name="create_16_en_book_flight", text="book flight on December 20 at 8pm"),

    # === MULTI (create/complete mix) ===
    Scenario(name="multi_1_two_simple", text="купить молоко и яйца, а ещё написать отчёт до завтра", use_multi=True),
    Scenario(name="multi_2_three_times", text="собрание завтра в 10; звонок в 14; починить проект вечером", use_multi=True),
    Scenario(name="multi_3_numbered", text="1) купить чай 2) отправить отчёт к понедельнику 3) позвонить сестре в 18:30", use_multi=True),
    Scenario(name="multi_4_en_three", text="finish math assignment by Friday; book doctor appointment Monday 9am; send team update tonight", use_multi=True),
    Scenario(name="multi_5_mixed_ru_en", text="поставь zoom call на завтра 9:00, send invoice by Friday, и купить корм коту сегодня", use_multi=True),
    Scenario(name="multi_6_deadlines_mixed", text="доклад до пятницы; оплатить счета завтра; купить торт к субботе 15:00", use_multi=True),
    Scenario(name="multi_7_multi_delete", text="удали задачу про молоко и удали задачу про отчёт", use_multi=True),
    Scenario(name="multi_8_two_facts_complete", text="презентацию сделал и отчёт закрыл", use_multi=True),
    Scenario(name="multi_9_two_tasks_diff_deadlines", text="создай задачу купить книги к пятнице и записаться к стоматологу завтра в 11:00", use_multi=True),
    Scenario(name="multi_10_mixed_deadlines", text="сделать презентацию до понедельника, оплатить коммуналку сегодня, приготовить ужин вечером", use_multi=True),

    # === COMPLETE (single) ===
    Scenario(name="complete_1_simple", text="я сделал отчёт по истории", use_multi=False),
    Scenario(name="complete_2_en_finished", text="I finished calculus homework", use_multi=False),
    Scenario(name="complete_3_pronoun_with_hint", text="эту задачу с распечатками закрыл", use_multi=False),

    # === DELETE (single) ===
    Scenario(name="delete_1_simple", text="удали задачу купить молоко", use_multi=False),
    Scenario(name="delete_2_pronoun", text="убери её про билеты", use_multi=False),
    Scenario(name="delete_3_en", text="delete the task about team meeting", use_multi=False),

    # === RESCHEDULE / ADD DEADLINE (single) ===
    Scenario(name="resched_1_add_deadline", text="добавь дедлайн для задачи презентация завтра в 10", use_multi=False),
    Scenario(name="resched_2_move_call", text="перенеси созвон с тимлидом на пятницу 15:00", use_multi=False),
    Scenario(name="resched_3_pronoun_only", text="добавь ей дедлайн завтра", use_multi=False),

    # === SHOW ===
    Scenario(name="show_1_today", text="что у меня на сегодня по задачам?", use_multi=False),
    Scenario(name="show_2_tomorrow", text="что у меня завтра?", use_multi=False),
    Scenario(name="show_3_specific_date", text="что у меня 12 декабря?", use_multi=False),
    Scenario(name="show_4_active", text="покажи активные задачи", use_multi=False),
    Scenario(name="show_5_weekend", text="что у меня на выходных?", use_multi=False),

    # === UNKNOWN ===
    Scenario(name="unknown_1_weather", text="какая завтра погода?", use_multi=False),
]


def print_task_snapshot(emit: Callable[[str], None]) -> None:
    emit("=" * 80)
    emit("ТЕКУЩИЙ СНИМОК ЗАДАЧ (TASKS_SNAPSHOT):")
    for tid, txt, due in TASKS_SNAPSHOT:
        emit(f"- [{tid}] {txt!r}  due_at={due}")
    emit("=" * 80)
    emit()


def pretty_print_single(result: TaskInterpretation, emit: Callable[[str], None]) -> None:
    data = result.model_dump()
    emit("  Один результат parse_user_input:")
    for k in ["action", "title", "deadline_iso", "target_task_hint", "language", "raw_input"]:
        emit(f"    {k}: {data.get(k)!r}")
    emit()


def pretty_print_multi(results: list[TaskInterpretation], emit: Callable[[str], None]) -> None:
    if not results:
        emit("  parse_user_input_multi → [] (ничего не нашёл)")
        return

    emit(f"  parse_user_input_multi → {len(results)} items:")
    for i, item in enumerate(results, start=1):
        data = item.model_dump()
        emit(f"    [{i}] action={data.get('action')!r}, title={data.get('title')!r}, deadline_iso={data.get('deadline_iso')!r}")
    emit()


def run_scenario(s: Scenario, emit: Callable[[str], None]) -> None:
    emit("-" * 80)
    emit(f"Сценарий: {s.name}")
    emit(f"Текст: {s.text!r}")
    emit()

    # 1) пробуем multi, если разрешено
    if s.use_multi:
        try:
            multi_results = parse_user_input_multi(
                s.text,
                tasks_snapshot=TASKS_SNAPSHOT,
                max_items=5,
            )
        except Exception as e:
            emit(f"  [ОШИБКА] parse_user_input_multi упал: {e}")
            multi_results = []
        pretty_print_multi(multi_results, emit)

    # 2) обычный парсер
    try:
        single_result = parse_user_input(
            s.text,
            tasks_snapshot=TASKS_SNAPSHOT,
        )
    except Exception as e:
        emit(f"  [ОШИБКА] parse_user_input упал: {e}")
        return

    pretty_print_single(single_result, emit)


def main() -> None:
    os.makedirs("test_runs", exist_ok=True)
    outfile = os.path.join("test_runs", f"tests_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")

    def emit(msg: str = "") -> None:
        with open(outfile, "a", encoding="utf-8") as f:
            f.write(msg + "\n")

    emit(f"Результаты будут сохранены в {outfile}")
    emit()

    print_task_snapshot(emit)
    total = len(SCENARIOS)
    emit(f"Запускаю {total} сценариев...\n")

    for idx, scenario in enumerate(SCENARIOS, start=1):
        emit(f"[{idx}/{total}]")
        run_scenario(scenario, emit)

    emit("Готово. Посмотри глазами, где парсер ведёт себя странно ✨")


if __name__ == "__main__":
    main()

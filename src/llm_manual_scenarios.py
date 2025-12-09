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
    # === CREATE: простые задачи ===
    Scenario(
        name="create_1_simple_no_deadline",
        text="купить хлеб",
    ),
    Scenario(
        name="create_2_simple_with_date",
        text="надо сдать отчёт по истории в пятницу",
    ),
    Scenario(
        name="create_3_time_only_today",
        text="позвонить маме в 20:00",
    ),
    Scenario(
        name="create_4_relative_tomorrow_morning",
        text="завтра утром нужно дописать конспект по калкулусу",
    ),
    Scenario(
        name="create_5_plan_word_nado",
        text="надо выучить 20 слов по английскому",
    ),
    Scenario(
        name="create_6_plan_word_hochu",
        text="хочу подготовиться к мидтерму по истории до понедельника",
    ),
    Scenario(
        name="create_7_typo_style",
        text="надо дсделать отчот по матану до завтра",
    ),

    # === MULTI-CREATE: несколько задач в одном сообщении ===
    Scenario(
        name="multi_1_two_tasks_simple",
        text="купить молоко и яйца, а ещё написать отчёт по калкулусу до завтра",
        use_multi=True,
    ),
    Scenario(
        name="multi_2_many_sentences",
        text=(
            "Сделать презентацию про организацию до пятницы. "
            "Добавить капитанов в чаты до пятницы. "
            "Написать полную повестку собрания до пятницы."
        ),
        use_multi=True,
    ),
    Scenario(
        name="multi_3_voice_like",
        text="провести контроль стажеров до пятницы. провести собрание в субботу в 14:00.",
        use_multi=True,
    ),
    Scenario(
        name="multi_4_mixed_style",
        text=(
            "завтра к вечеру дочитать книгу по истории. "
            "потом как-нибудь созвониться с тимлидом. "
            "и ещё надо написать план игры."
        ),
        use_multi=True,
    ),

    # === RESCHEDULE: перенос / добавление дедлайна к существующим задачам ===
    Scenario(
        name="reschedule_1_add_deadline_to_existing",
        text="добавь дедлайн для задачи купить молоко завтра в шесть вечера",
        use_multi=False,
    ),
    Scenario(
        name="reschedule_2_shift_existing",
        text="перенеси тимбилдинг правления на субботу в 15:00",
        use_multi=False,
    ),
    Scenario(
        name="reschedule_3_shift_existing_no_time",
        text="сдвинь дедлайн по калкулус 2 онлайн на воскресенье",
        use_multi=False,
    ),
    Scenario(
        name="reschedule_4_add_deadline_no_deadline_word",
        text="поставь срок на проверку распределения на игру завтра",
        use_multi=False,
    ),

    # === COMPLETE: завершение задач ===
    Scenario(
        name="complete_1_simple_done",
        text="я сделал калкулус 2 онлайн",
        use_multi=False,
    ),
    Scenario(
        name="complete_2_report_style",
        text="отчёт по истории сдал, можешь отметить выполненным",
        use_multi=False,
    ),
    Scenario(
        name="complete_3_short_ready",
        text="готово, тимбилдинг провели",
        use_multi=False,
    ),
    Scenario(
        name="complete_4_verb_past",
        text="дочитал конспект по калкулусу",
        use_multi=False,
    ),

    # === DELETE: удаление ===
    Scenario(
        name="delete_1_simple",
        text="удали задачу купить молоко",
        use_multi=False,
    ),
    Scenario(
        name="delete_2_soft",
        text="можно убрать задачу про распечатки?",
        use_multi=False,
    ),

    # === SHOW: показать задачи ===
    Scenario(
        name="show_1_today",
        text="что у меня на сегодня по задачам?",
        use_multi=False,
    ),
    Scenario(
        name="show_2_tomorrow",
        text="что у меня завтра?",
        use_multi=False,
    ),
    Scenario(
        name="show_3_specific_date",
        text="что у меня 12 декабря?",
        use_multi=False,
    ),

    # === MASS CLEAR / защитные кейсы ===
    Scenario(
        name="mass_clear_1",
        text="очисти список задач",
        use_multi=False,
    ),
    Scenario(
        name="mass_clear_2",
        text="удали все задачи",
        use_multi=False,
    ),

    # === Переименования (для проверки, что парсер их не путает) ===
    Scenario(
        name="rename_1_explicit",
        text="переименуй задачу калкулус 2 онлайн в подготовка к экзамену по калкулусу",
        use_multi=False,
    ),
    Scenario(
        name="rename_2_instead_of",
        text="вместо тимбилдинг правления задача должна называться собрание правления",
        use_multi=False,
    ),

    # === Приветствия / не-задачи ===
    Scenario(
        name="greeting_1_simple",
        text="привет",
        use_multi=False,
    ),
    Scenario(
        name="greeting_2_mixed",
        text="привет, что ты умеешь?",
        use_multi=False,
    ),
    Scenario(
        name="chitchat_1",
        text="как дела?",
        use_multi=False,
    ),

    # === Сложные/размазанные формулировки ===
    Scenario(
        name="fuzzy_1_long",
        text=(
            "слушай, надо будет как-нибудь разобраться с распределением на игру, "
            "я это хочу сделать в пятницу вечером"
        ),
        use_multi=False,
    ),
    Scenario(
        name="fuzzy_2_mixed_plan_fact",
        text="вчера провёл игру, а завтра надо сделать презентацию про организацию",
        use_multi=True,
    ),
    Scenario(
        name="fuzzy_3_ambiguous",
        text="ну там надо с игрой разобраться и вообще закрыть все хвосты по универу",
        use_multi=True,
    ),

    # === Дополнительные случаи для расширенного покрытия ===
    Scenario(
        name="create_8_mixed_en_ru",
        text="schedule zoom call tomorrow at 10am",
    ),
    Scenario(
        name="create_9_tickets_travel",
        text="купить билеты в алматы на пятницу утром",
    ),
    Scenario(
        name="create_10_presentation_monday",
        text="надо приготовить презентацию к понедельнику 9:00",
    ),
    Scenario(
        name="create_11_time_only_evening",
        text="в 7 вечера пробежка",
    ),
    Scenario(
        name="create_12_voice_filler",
        text="так, запиши уборка квартиры в субботу днём",
    ),
    Scenario(
        name="create_13_typed_typos",
        text="надо сделоть отцет к пятнцие 23:59",
    ),
    Scenario(
        name="create_14_investor_meet",
        text="встретиться с инвестором в 15:30",
    ),
    Scenario(
        name="create_15_evening_run",
        text="вечером пробежать 5к",
    ),

    Scenario(
        name="multi_5_numbered",
        text="1) купить чай 2) отправить отчёт к понедельнику 3) позвонить сестре в 18:30",
        use_multi=True,
    ),
    Scenario(
        name="multi_6_vo_pervoh_vtoroh",
        text="во-первых дописать конспект по истории до завтра, во-вторых купить молоко, в-третьих запланировать встречу в пятницу в 10",
        use_multi=True,
    ),
    Scenario(
        name="multi_7_en_mix_three_tasks",
        text="Do two things: finish math assignment by Friday; book doctor appointment Monday 9am; send team update tonight",
        use_multi=True,
    ),
    Scenario(
        name="multi_8_noisy_voice",
        text="ну короче надо доделать презентацию до субботы, потом ещё позвонить бабушке завтра утром, и оформить заявку до понедельника",
        use_multi=True,
    ),
    Scenario(
        name="multi_9_typos_and_weekdays",
        text="купитб картошку завтра вечером; сделать конспект втроник утром; купить билеты к среде",
        use_multi=True,
    ),
    Scenario(
        name="multi_10_cross_lang_mix",
        text="поставь zoom call на завтра 9:00, потом отправь invoice by Friday, и ещё купить корм коту сегодня",
        use_multi=True,
    ),
    Scenario(
        name="multi_11_three_simple",
        text="собрание завтра в 10; встреча в 14; починить проект вечером",
        use_multi=True,
    ),

    Scenario(
        name="reschedule_5_english",
        text="move meeting with dean to next monday at 3pm",
        use_multi=False,
    ),
    Scenario(
        name="reschedule_6_pronoun_deadline",
        text="добавь ей дедлайн завтра в 12",
        use_multi=False,
    ),
    Scenario(
        name="reschedule_7_time_only",
        text="перенеси созвон с тимлидом на 18:00",
        use_multi=False,
    ),
    Scenario(
        name="reschedule_8_date_only",
        text="перенеси дедлайн по презентации на 12 декабря",
        use_multi=False,
    ),
    Scenario(
        name="reschedule_9_add_deadline_project",
        text="сделай дедлайн на проект через три дня",
        use_multi=False,
    ),

    Scenario(
        name="complete_5_english",
        text="I finished calculus 2 online",
        use_multi=False,
    ),
    Scenario(
        name="complete_6_pronoun",
        text="эту задачу с распечатками закрыл",
        use_multi=False,
    ),
    Scenario(
        name="complete_7_ready_phrase",
        text="готов с презентацией про организацию",
        use_multi=False,
    ),
    Scenario(
        name="complete_8_multi_fact",
        text="презентацию сделал и отчёт закрыл",
        use_multi=True,
    ),

    Scenario(
        name="delete_3_pronoun",
        text="убери её про молоко",
        use_multi=False,
    ),
    Scenario(
        name="delete_4_english",
        text="delete the task about team building",
        use_multi=False,
    ),
    Scenario(
        name="delete_5_quotes",
        text="пожалуйста удалите задачу \"купить билеты\"",
        use_multi=False,
    ),

    Scenario(
        name="show_4_weekend",
        text="что у меня на выходных?",
        use_multi=False,
    ),
    Scenario(
        name="show_5_active",
        text="покажи активные задачи",
        use_multi=False,
    ),

    Scenario(
        name="mass_clear_3_all_tasks",
        text="очистить все дела",
        use_multi=False,
    ),
    Scenario(
        name="mass_clear_4_reset_list",
        text="сбрось список дел",
        use_multi=False,
    ),

    Scenario(
        name="unknown_1_weather",
        text="какая завтра погода?",
        use_multi=False,
    ),
    Scenario(
        name="unknown_2_time",
        text="сколько время?",
        use_multi=False,
    ),

    Scenario(
        name="fuzzy_4_someday",
        text="надо бы заняться научкой когда-нибудь",
        use_multi=False,
    ),
    Scenario(
        name="fuzzy_5_maybe_today_or_tomorrow",
        text="может успею сделать отчёт сегодня, если нет то завтра",
        use_multi=True,
    ),
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
        print(msg)
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

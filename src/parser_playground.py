# src/parser_playground.py
from pprint import pprint

from llm_client import parse_user_input


def main():
    print("Smart-Tasker LLM playground")
    print("Пиши фразы про задачи. Ctrl+C — выход.\n")

    while True:
        try:
            text = input(">>> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nПока 👋")
            break

        if not text:
            continue

        try:
            result = parse_user_input(text)
        except Exception as e:
            print(f"\n[ОШИБКА] {e}\n")
            continue

        print("\nСтруктура от модели:")
        pprint(result.model_dump(), width=120, sort_dicts=False)
        print()


if __name__ == "__main__":
    main()

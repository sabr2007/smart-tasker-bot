# smart-tasker (Telegram Bot + Telegram WebApp)

Один проект, **одна база SQLite**, два клиента:
- Telegram Bot
- Telegram Web App (Mini App)

## Требования

- Python 3.11
- Переменные окружения в `.env` (через `python-dotenv`)

Минимально нужно:
- `TELEGRAM_BOT_TOKEN`
- `OPENAI_API_KEY` (нужен боту; WebApp в Фазе 1 LLM не использует, но конфиг общий)

Опционально:
- `WEBAPP_URL` — URL Mini App (кнопка в боте). По умолчанию `http://localhost:8000/`
- `DB_PATH` — путь к SQLite. По умолчанию используется `src/tasks.db`

## Установка

```bash
pip install -r requirements.txt
```

## Запуск WebApp backend (FastAPI)

Важно: чтобы импорты вида `from config import ...` работали корректно, запускаем `uvicorn` с `--app-dir src`.

```bash
python -m uvicorn web.app:app --app-dir src --host 0.0.0.0 --port 8000 --reload
```

Проверка:
- `GET /health` → `{ "ok": true }`
- `GET /` → статический фронтенд `src/webapp/`

## Запуск Telegram Bot

```bash
python src/main.py
```

## Telegram WebApp (Mini App)

В боте появилась кнопка **«Открыть панель задач»**, которая открывает `WEBAPP_URL`.

Для реального теста внутри Telegram URL должен быть доступен извне и по HTTPS
(обычно используют туннель вроде ngrok / cloudflared), и этот URL должен совпадать с `WEBAPP_URL`.



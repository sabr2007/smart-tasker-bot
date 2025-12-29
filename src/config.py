# src/config.py
import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")  
DEFAULT_TIMEZONE = os.getenv("TIMEZONE", "Asia/Almaty")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8000/")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не найден в .env")


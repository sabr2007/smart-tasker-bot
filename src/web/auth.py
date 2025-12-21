import hashlib
import hmac
import json
import time
from typing import Any, Dict
from urllib.parse import parse_qsl

from config import TELEGRAM_BOT_TOKEN


class TelegramInitDataError(ValueError):
    pass


def verify_telegram_init_data(init_data: str, *, max_age_seconds: int = 24 * 60 * 60) -> Dict[str, Any]:
    """
    Проверяет подпись Telegram WebApp initData по официальной схеме.

    Документация: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

    ВАЖНО:
    - Для backend'а init_data приходит как querystring (k=v&k2=v2...).
    - Поле `hash` обязано совпасть с HMAC SHA-256 от data-check-string.
    - Используем TELEGRAM_BOT_TOKEN из `config.py`.
    - Возвращаем распарсенный dict, в т.ч. user (как dict), если он есть.
    """
    if not init_data or not isinstance(init_data, str):
        raise TelegramInitDataError("initData пустой или не строка")

    bot_token = TELEGRAM_BOT_TOKEN
    if not bot_token:
        raise TelegramInitDataError("TELEGRAM_BOT_TOKEN не задан")

    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True, keep_blank_values=True))
    except Exception as e:
        raise TelegramInitDataError(f"initData не удалось распарсить: {e}") from e

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise TelegramInitDataError("initData без поля hash")

    # Проверка "свежести" (best-effort)
    if max_age_seconds and "auth_date" in pairs:
        try:
            auth_date = int(pairs.get("auth_date") or "0")
            if auth_date > 0:
                now = int(time.time())
                if now - auth_date > max_age_seconds:
                    raise TelegramInitDataError("initData устарел (auth_date слишком старый)")
        except TelegramInitDataError:
            raise
        except Exception:
            # не блокируем, если auth_date сломан
            pass

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))

    # secret_key = HMAC_SHA256("WebAppData", bot_token)
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    # expected_hash = HMAC_SHA256(secret_key, data_check_string)
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise TelegramInitDataError("Неверная подпись initData")

    # Парсим user (если есть): Telegram передаёт JSON строкой
    if "user" in pairs and isinstance(pairs["user"], str):
        try:
            pairs["user"] = json.loads(pairs["user"])
        except Exception:
            # оставим как есть (строкой)
            pass

    pairs["hash"] = received_hash
    return pairs



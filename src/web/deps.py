from typing import Any, Dict, Optional

from fastapi import Header, HTTPException, status

from web.auth import TelegramInitDataError, verify_telegram_init_data


def get_current_user(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> Dict[str, Any]:
    """
    Dependency: извлекает пользователя из Telegram Mini App initData.

    Ожидаем заголовок:
      Authorization: tma <initData>
    """
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Нет Authorization")

    if not authorization.lower().startswith("tma "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверная схема Authorization")

    init_data = authorization[4:].strip()
    if not init_data:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Пустой initData")

    try:
        parsed = verify_telegram_init_data(init_data)
    except TelegramInitDataError as e:
        import logging
        logging.getLogger("web.deps").error(f"Auth failed: {e}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e)) from e

    user = parsed.get("user")
    if not isinstance(user, dict) or "id" not in user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="initData без user.id")

    try:
        user_id = int(user["id"])
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user.id не число")

    return {
        "user_id": user_id,
        "user": user,
        "init_data": init_data,
        "init_data_parsed": parsed,
    }



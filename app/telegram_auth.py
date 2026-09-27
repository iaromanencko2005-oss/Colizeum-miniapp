"""
Проверка initData мини-приложения Telegram.

Telegram подписывает данные о пользователе, которые открыли мини-апп,
секретным ключом на основе токена бота. Без этой проверки любой человек
мог бы обратиться к API от имени чужого tg_id и, например, накрутить
себе уровень. Алгоритм — официальный, из документации Telegram
(https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
"""
import hashlib
import hmac
import json
import os
from urllib.parse import parse_qsl

BOT_TOKEN = os.getenv("BOT_TOKEN", "")


class InvalidInitData(Exception):
    pass


def parse_and_verify(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise InvalidInitData("BOT_TOKEN не задан на сервере")

    parsed = dict(parse_qsl(init_data, strict_parsing=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise InvalidInitData("Нет подписи hash в initData")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise InvalidInitData("Подпись initData не совпадает")

    user_raw = parsed.get("user")
    if not user_raw:
        raise InvalidInitData("В initData нет данных пользователя")

    return json.loads(user_raw)

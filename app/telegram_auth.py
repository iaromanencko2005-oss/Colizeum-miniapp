"""
Проверка initData мини-приложения Telegram.

Telegram подписывает данные о пользователе, которые открыли мини-апп,
секретным ключом на основе токена бота. Без этой проверки любой человек
мог бы обратиться к API от имени чужого tg_id и, например, накрутить
себе уровень. Алгоритм — официальный, из документации Telegram
(https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
"""
import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

BOT_TOKEN = os.getenv("BOT_TOKEN", "")


class InvalidInitData(Exception):
    pass


# ---------- запасной вход: подписанный пропуск в кнопке бота ----------
# Бывает, что Telegram открывает кабинет, но не передаёт ему initData (отдельные
# телефоны, сборки Telegram, сети). Поэтому бот вшивает в адрес кнопки
# «Открыть личный кабинет» пропуск этого гостя: его Telegram ID, имя и срок,
# подписанные секретом на основе токена бота. Подделать пропуск без токена
# бота нельзя, а адрес кнопки Telegram передаёт всегда.
LOGIN_TOKEN_DAYS = 30


def _token_key() -> bytes:
    return hmac.new(b"ColizeumLogin", BOT_TOKEN.encode(), hashlib.sha256).digest()


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _unb64(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


def make_login_token(tg_id: int, first_name: str = "") -> str:
    exp = int(time.time()) + LOGIN_TOKEN_DAYS * 86400
    payload = f"{tg_id}.{exp}.{_b64((first_name or '')[:40])}"
    sig = hmac.new(_token_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def verify_login_token(token: str) -> dict:
    try:
        tg_id, exp, name_b64, sig = token.split(".")
        payload = f"{tg_id}.{exp}.{name_b64}"
        good = hmac.new(_token_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(good, sig):
            raise InvalidInitData("Пропуск недействителен")
        if int(exp) < time.time():
            raise InvalidInitData("Пропуск устарел — открой кабинет заново через /start")
        return {"id": int(tg_id), "first_name": _unb64(name_b64) or None}
    except InvalidInitData:
        raise
    except Exception:
        raise InvalidInitData("Пропуск недействителен")


def parse_and_verify(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise InvalidInitData("BOT_TOKEN не задан на сервере")
    if init_data.startswith("tok:"):
        return verify_login_token(init_data[4:])

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

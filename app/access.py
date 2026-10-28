"""
Доступ к бесплатным играм (рулетка, кости, дайс).

Бесплатные попытки открыты только гостям, которые пополняли счёт в клубе:
  • номер найден в выгрузке CRM с пополнениями (/import_topups, /set_topup) — открыто сразу;
  • либо гость пополнил на стойке (от UNLOCK_MIN_TOPUP ₽), нажал в приложении
    «Проверить пополнение», а администратор сверил с CRM и нажал «Пополнение есть».
Всё остальное в приложении (магазин, батлы, игры на колизы, рейтинг, афиша) работает как раньше.
Админ может открыть или закрыть доступ вручную: /unlock +7…, /lock +7….
"""
import os
from datetime import datetime, timedelta

from .models import Client, lookup_topup

UNLOCK_MIN_TOPUP = int(os.getenv("UNLOCK_MIN_TOPUP", "500"))
CHECK_COOLDOWN = timedelta(minutes=int(os.getenv("TOPUP_CHECK_COOLDOWN_MIN", "30")))


class AccessError(Exception):
    pass


def _wait_text(td: timedelta) -> str:
    m = max(1, int(td.total_seconds() // 60) + (1 if td.total_seconds() % 60 else 0))
    return f"{m} мин" if m < 60 else f"{m // 60} ч {m % 60:02d} мин"


def games_lock(db, client: Client) -> dict:
    """{"locked": bool, ...} — что показать в приложении."""
    if client.games_locked:
        return {"locked": True, "by_admin": True, "min_topup": UNLOCK_MIN_TOPUP, "state": "declined", "retry_in": None}
    if client.games_unlocked_at:
        return {"locked": False, "how": "approved"}
    topup, found = lookup_topup(db, client)
    if found and topup > 0:
        return {"locked": False, "how": "crm"}
    retry_in = None
    if client.topup_check_at and client.topup_check_state in ("pending", "declined"):
        left = client.topup_check_at + CHECK_COOLDOWN - datetime.utcnow()
        if left.total_seconds() > 0:
            retry_in = _wait_text(left)
    return {"locked": True, "by_admin": False, "min_topup": UNLOCK_MIN_TOPUP,
            "state": client.topup_check_state if client.topup_check_state in ("pending", "declined") else None,
            "retry_in": retry_in}


def is_locked(db, client: Client) -> bool:
    return games_lock(db, client)["locked"]


def request_check(db, client: Client) -> None:
    """Гость нажал «Проверить пополнение». Повторно — не чаще раза в CHECK_COOLDOWN."""
    lock = games_lock(db, client)
    if not lock["locked"]:
        return
    if lock.get("by_admin"):
        raise AccessError("Доступ к бесплатным играм закрыт администратором — уточни на стойке")
    if lock["retry_in"]:
        if lock["state"] == "pending":
            raise AccessError(f"Заявка уже у администратора. Повторно отправить можно через {lock['retry_in']}")
        raise AccessError(f"Повторная проверка — через {lock['retry_in']}")
    client.topup_check_at = datetime.utcnow()
    client.topup_check_state = "pending"
    db.commit()


def decide(db, client_id: int, ok: bool) -> Client | None:
    """Решение администратора по заявке. None — заявка уже обработана."""
    c = db.get(Client, client_id)
    if c is None or c.topup_check_state != "pending":
        return None
    c.topup_check_state = "approved" if ok else "declined"
    if ok:
        c.games_unlocked_at = datetime.utcnow()
        c.games_locked = False
    db.commit()
    db.refresh(c)
    return c


def set_unlocked(db, client: Client, on: bool) -> None:
    """Ручное управление админом: /unlock и /lock."""
    if on:
        client.games_unlocked_at = client.games_unlocked_at or datetime.utcnow()
        client.games_locked = False
        client.topup_check_state = "approved"
    else:
        client.games_unlocked_at = None
        client.games_locked = True
        client.topup_check_state = None
    db.commit()

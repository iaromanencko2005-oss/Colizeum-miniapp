"""
Отметка «Я в клубе» с фото чека.

Гость в приложении (или прямо в чате с ботом) прикладывает фото чека за
сегодняшнее посещение. Бот пересылает фото администраторам с кнопками
«✅ Засчитать» / «❌ Отклонить». После подтверждения гостю начисляются
CHECKIN_BONUS колизов (по умолчанию 30) и засчитывается визит.

Одна отметка в день по московскому времени. Фото хранится только в Telegram
(в базе — лишь ссылка на него), чтобы не держать на сервере лишние данные.
"""
import os
from datetime import datetime, timedelta

from sqlalchemy import func

from .models import CheckIn, Client

CHECKIN_BONUS = int(os.getenv("CHECKIN_BONUS", "30"))
MSK = timedelta(hours=3)


class CheckInError(Exception):
    pass


def today_key(now: datetime | None = None) -> str:
    return ((now or datetime.utcnow()) + MSK).strftime("%Y-%m-%d")


def month_prefix(now: datetime | None = None) -> str:
    return ((now or datetime.utcnow()) + MSK).strftime("%Y-%m")


def today(db, client: Client) -> CheckIn | None:
    """Сегодняшняя отметка (последняя, если первую отклонили)."""
    return (db.query(CheckIn).filter(CheckIn.client_id == client.id, CheckIn.day == today_key())
            .order_by(CheckIn.id.desc()).first())


def can_submit(db, client: Client):
    if not client.phone_verified:
        raise CheckInError("Сначала подтверди номер телефона в кабинете")
    t = today(db, client)
    if t and t.status == "pending":
        raise CheckInError("Сегодняшний чек уже на проверке у администратора")
    if t and t.status == "approved":
        raise CheckInError("Сегодня ты уже отметился — приходи завтра!")
    rejected_today = db.query(func.count(CheckIn.id)).filter(
        CheckIn.client_id == client.id, CheckIn.day == today_key(), CheckIn.status == "rejected").scalar() or 0
    if rejected_today >= 2:
        raise CheckInError("Сегодня уже было две попытки — приходи завтра")


def create(db, client: Client) -> CheckIn:
    c = CheckIn(client_id=client.id, tg_id=client.tg_id, day=today_key(), status="pending")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def decide(db, checkin_id: int, approve: bool, by: str):
    """Подтвердить/отклонить. Возвращает отметку, если решение принято сейчас (не повторно)."""
    status = "approved" if approve else "rejected"
    bonus = CHECKIN_BONUS if approve else 0
    n = db.query(CheckIn).filter(CheckIn.id == checkin_id, CheckIn.status == "pending").update(
        {CheckIn.status: status, CheckIn.decided_at: datetime.utcnow(), CheckIn.decided_by: by[:60],
         CheckIn.bonus: bonus}, synchronize_session=False)
    if n != 1:
        db.rollback()
        return None
    c = db.get(CheckIn, checkin_id)
    if approve and bonus:
        # Второй подтверждённый чек за тот же день колиз не даёт.
        dup = db.query(func.count(CheckIn.id)).filter(
            CheckIn.client_id == c.client_id, CheckIn.day == c.day, CheckIn.status == "approved",
            CheckIn.id != c.id).scalar() or 0
        if dup:
            c.bonus = 0
        else:
            db.query(Client).filter(Client.id == c.client_id).update(
                {Client.balance: func.coalesce(Client.balance, 0) + bonus}, synchronize_session=False)
    db.commit()
    db.refresh(c)
    return c


def visits_this_month(db, client: Client) -> int:
    return db.query(func.count(func.distinct(CheckIn.day))).filter(
        CheckIn.client_id == client.id, CheckIn.status == "approved",
        CheckIn.day.like(month_prefix() + "%")).scalar() or 0


def view(db, client: Client) -> dict:
    t = today(db, client)
    return {
        "bonus": CHECKIN_BONUS,
        "today": t.status if t else None,          # None | pending | approved | rejected
        "today_bonus": t.bonus if t else 0,
        "visits_month": visits_this_month(db, client),
    }

"""
Колизы действуют сутки.

Как считается: как только на пустом балансе появляются колизы (выигрыш, подарок,
приглашение друга, чек), запускается таймер на BONUS_TTL_HOURS (24 часа). Новые
начисления его НЕ продлевают. Когда время вышло — всё, что осталось на балансе,
сгорает. Потратить можно одним способом в сутки: списать на карту гостя или
купить в магазине.

За BONUS_WARN_HOURS (3 часа) до сгорания бот напоминает гостю — повод зайти в клуб.
"""
import os
from datetime import datetime, timedelta

from .models import Client, BonusBurn

BONUS_TTL_HOURS = float(os.getenv("BONUS_TTL_HOURS", "24"))
BONUS_WARN_HOURS = float(os.getenv("BONUS_WARN_HOURS", "3"))
WARN_MIN_BALANCE = int(os.getenv("BONUS_WARN_MIN", "50"))     # о мелочи не напоминаем


def enabled() -> bool:
    return BONUS_TTL_HOURS > 0


def deadline_for(client: Client, now: datetime | None = None) -> datetime | None:
    """Когда сгорят колизы гостя (для показа). None — сгорать нечему."""
    if not enabled() or (client.balance or 0) <= 0:
        return None
    return client.bonus_expires_at or (now or datetime.utcnow()) + timedelta(hours=BONUS_TTL_HOURS)


def tick(db, now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """Запускает таймеры, сжигает просроченное, собирает напоминания.
    Возвращает (напомнить [{tg_id, amount, expires_at}], сгорело [{tg_id, amount}])."""
    if not enabled():
        return [], []
    now = now or datetime.utcnow()
    ttl = timedelta(hours=BONUS_TTL_HOURS)
    # 1) появились колизы — запускаем таймер
    db.query(Client).filter(Client.balance > 0, Client.bonus_expires_at.is_(None)).update(
        {Client.bonus_expires_at: now + ttl, Client.bonus_warned: False}, synchronize_session=False)
    db.commit()
    # 2) время вышло — сжигаем остаток
    burned = []
    for c in db.query(Client).filter(Client.bonus_expires_at.isnot(None), Client.bonus_expires_at <= now).all():
        amount = c.balance or 0
        n = db.query(Client).filter(Client.id == c.id, Client.bonus_expires_at == c.bonus_expires_at).update(
            {Client.balance: 0, Client.bonus_expires_at: None, Client.bonus_warned: False}, synchronize_session=False)
        if n == 1 and amount > 0:
            db.add(BonusBurn(client_id=c.id, amount=amount))
            burned.append({"tg_id": c.tg_id, "amount": amount})
        db.commit()
    # 3) скоро сгорят — напоминаем один раз
    warn = []
    soon = now + timedelta(hours=BONUS_WARN_HOURS)
    for c in db.query(Client).filter(Client.bonus_expires_at.isnot(None), Client.bonus_expires_at <= soon,
                                     Client.bonus_expires_at > now, Client.bonus_warned.isnot(True),
                                     Client.balance >= WARN_MIN_BALANCE).all():
        c.bonus_warned = True
        warn.append({"tg_id": c.tg_id, "amount": c.balance, "expires_at": c.bonus_expires_at})
    db.commit()
    return warn, burned


def refund_allowed(deadline: datetime | None, now: datetime | None = None) -> bool:
    """Возврат колизов по отменённому заказу — только если они бы ещё не сгорели.
    Иначе отмена заказа стала бы способом продлить колизы."""
    return deadline is None or (now or datetime.utcnow()) < deadline

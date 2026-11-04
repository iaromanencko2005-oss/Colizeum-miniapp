"""
Колизы и отыгрыш.

Колизы живут «окнами» по BONUS_TTL_HOURS (24 часа). Окно начинается, когда на
балансе появляются колизы: запоминаем баланс в этот момент — это сумма отыгрыша.
Чтобы сохранить колизы, за окно нужно сделать ставок в играх и батлах на колизы
не меньше этой суммы (лежит 1000 — поставь за сутки на 1000, выиграл или проиграл — неважно).

Когда окно закончилось:
  • отыграл — колизы сохраняются, начинается новое окно с текущим балансом;
  • не отыграл — баланс сгорает. Не сгорают только колизы, начисленные за это окно
    кэшбэком по чеку или администратором (protect()): они переходят в новое окно,
    и их нужно отыграть уже там.

Если гость часть колизов списал или потратил в магазине, отыгрывать нужно не больше
того, что осталось на балансе. Бесплатные игры отыгрышем не считаются — только ставки.
За BONUS_WARN_HOURS (3 часа) до конца окна бот напоминает, сколько осталось отыграть.
Списание на карту и магазин — по-прежнему до WITHDRAW_LIMIT (300) в сутки.
"""
import os
from datetime import datetime, timedelta

from sqlalchemy import func

from .models import Client, BonusBurn, GamePlay

BONUS_TTL_HOURS = float(os.getenv("BONUS_TTL_HOURS", "24"))
BONUS_WARN_HOURS = float(os.getenv("BONUS_WARN_HOURS", "3"))
WARN_MIN_BALANCE = int(os.getenv("BONUS_WARN_MIN", "50"))     # о мелочи не напоминаем


def enabled() -> bool:
    return BONUS_TTL_HOURS > 0


def touch(db, client_id: int, now: datetime | None = None) -> None:
    """Раньше трата продлевала срок. Теперь срок продлевает только отыгрыш — оставлено для совместимости."""
    return None


def protect(db, client_id: int, amount: int) -> None:
    """Начисление не за игры (кэшбэк, админ) — если окно уже идёт, эти колизы не сгорят в его конце.
    Коммит — на вызывающем."""
    if not enabled() or amount <= 0:
        return
    db.query(Client).filter(Client.id == client_id, Client.bonus_expires_at.isnot(None)).update(
        {Client.wager_protected: func.coalesce(Client.wager_protected, 0) + amount}, synchronize_session=False)


def turnover(db, tg_id: int, since: datetime | None) -> int:
    q = db.query(func.coalesce(func.sum(GamePlay.stake), 0)).filter(GamePlay.tg_id == tg_id, GamePlay.mode == "bet")
    if since is not None:
        q = q.filter(GamePlay.created_at >= since)
    return int(q.scalar() or 0)


def progress(db, client: Client) -> dict | None:
    """Сколько нужно отыграть в текущем окне. None — окна нет (баланс пуст)."""
    if not enabled() or (client.balance or 0) <= 0 or client.bonus_expires_at is None:
        return None
    balance = client.balance or 0
    protected = min(balance, client.wager_protected or 0)
    required = min(client.wager_required if client.wager_required is not None else balance, balance)
    done = turnover(db, client.tg_id, client.wager_since)
    left = max(0, required - done)
    return {"required": required, "done": min(done, required) if required else done, "left": left,
            "saved": left == 0, "protected": protected, "at_risk": 0 if left == 0 else balance - protected,
            "expires_at": client.bonus_expires_at.isoformat() + "Z"}


def deadline_for(client: Client, now: datetime | None = None) -> datetime | None:
    """Когда заканчивается окно (для показа). None — сгорать нечему."""
    if not enabled() or (client.balance or 0) <= 0:
        return None
    return client.bonus_expires_at or (now or datetime.utcnow()) + timedelta(hours=BONUS_TTL_HOURS)


def _new_window(client: Client, now: datetime, ttl: timedelta):
    client.bonus_expires_at = now + ttl
    client.bonus_warned = False
    client.wager_since = now
    client.wager_required = client.balance or 0
    client.wager_protected = 0


def tick(db, now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """Открывает окна, закрывает истёкшие (сохраняет или сжигает), собирает напоминания.
    Возвращает (напомнить [{tg_id, amount, left, required, expires_at}], сгорело [{tg_id, amount, kept}])."""
    if not enabled():
        return [], []
    now = now or datetime.utcnow()
    ttl = timedelta(hours=BONUS_TTL_HOURS)
    # 1) появились колизы — открываем окно; баланс опустел — окно закрываем
    for c in db.query(Client).filter(Client.balance > 0, Client.bonus_expires_at.is_(None)).all():
        _new_window(c, now, ttl)
    # переход со старых правил: у тех, у кого шёл старый таймер, — новое окно на полные сутки
    for c in db.query(Client).filter(Client.balance > 0, Client.bonus_expires_at.isnot(None), Client.wager_since.is_(None)).all():
        _new_window(c, now, ttl)
    db.query(Client).filter(func.coalesce(Client.balance, 0) <= 0, Client.bonus_expires_at.isnot(None)).update(
        {Client.bonus_expires_at: None, Client.bonus_warned: False, Client.wager_since: None,
         Client.wager_required: None, Client.wager_protected: 0}, synchronize_session=False)
    db.commit()
    # 2) окно закончилось — отыграл: новое окно; не отыграл: сжигаем всё, кроме защищённого
    burned = []
    for c in db.query(Client).filter(Client.bonus_expires_at.isnot(None), Client.bonus_expires_at <= now).all():
        p = progress(db, c)
        if p is None:
            continue
        old_deadline = c.bonus_expires_at
        if p["saved"]:
            _new_window(c, now, ttl)
            db.commit()
            continue
        burn = max(0, (c.balance or 0) - p["protected"])
        n = db.query(Client).filter(Client.id == c.id, Client.bonus_expires_at == old_deadline).update(
            {Client.balance: func.coalesce(Client.balance, 0) - burn}, synchronize_session=False)
        if n != 1:
            db.rollback()
            continue
        db.refresh(c)
        if burn > 0:
            db.add(BonusBurn(client_id=c.id, amount=burn))
            burned.append({"tg_id": c.tg_id, "amount": burn, "kept": c.balance or 0})
        if (c.balance or 0) > 0:
            _new_window(c, now, ttl)
        else:
            c.bonus_expires_at, c.bonus_warned, c.wager_since, c.wager_required, c.wager_protected = None, False, None, None, 0
        db.commit()
    # 3) до конца окна мало времени, а отыграно не всё — напоминаем один раз
    warn = []
    soon = now + timedelta(hours=BONUS_WARN_HOURS)
    for c in db.query(Client).filter(Client.bonus_expires_at.isnot(None), Client.bonus_expires_at <= soon,
                                     Client.bonus_expires_at > now, Client.bonus_warned.isnot(True),
                                     Client.balance >= WARN_MIN_BALANCE).all():
        p = progress(db, c)
        if p is None or p["saved"] or p["at_risk"] < WARN_MIN_BALANCE:
            continue
        c.bonus_warned = True
        warn.append({"tg_id": c.tg_id, "amount": p["at_risk"], "left": p["left"], "required": p["required"],
                     "expires_at": c.bonus_expires_at})
    db.commit()
    return warn, burned


def refund_allowed(deadline: datetime | None, now: datetime | None = None) -> bool:
    """Возврат колизов по отменённому заказу — только если они бы ещё не сгорели.
    Иначе отмена заказа стала бы способом продлить колизы."""
    return deadline is None or (now or datetime.utcnow()) < deadline

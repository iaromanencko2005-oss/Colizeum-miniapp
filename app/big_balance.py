"""
Списание при большом балансе.

Колизы больше не сгорают, пока гость ими пользуется, поэтому баланс может расти.
Чтобы его не выводили систематически, при балансе от BIG_BALANCE (по умолчанию 1000)
списание открывается одним из двух способов:
  • сыграть на сумму баланса — сумма ставок в играх и батлах с прошлого списания
    должна быть не меньше текущего баланса;
  • прислать фото чека о пополнении счёта в клубе — администратор подтверждает,
    и открывается одно списание (действует RECEIPT_HOURS часов).
Магазин и обычные списания при меньшем балансе работают как раньше.
"""
import os
from datetime import datetime, timedelta

from sqlalchemy import func, or_

from .models import Client, GamePlay, Withdrawal, WithdrawReceipt

BIG_BALANCE = int(os.getenv("WITHDRAW_BIG_BALANCE", "1000"))
RECEIPT_HOURS = int(os.getenv("WITHDRAW_RECEIPT_HOURS", "48"))


class ReceiptError(Exception):
    pass


def last_withdraw_at(db, client: Client):
    conds = [Withdrawal.tg_id == client.tg_id]
    if client.phone_normalized:
        conds.append(Withdrawal.phone_normalized == client.phone_normalized)
    return db.query(func.max(Withdrawal.created_at)).filter(or_(*conds)).scalar()


def turnover(db, client: Client, since) -> int:
    q = db.query(func.coalesce(func.sum(GamePlay.stake), 0)).filter(GamePlay.tg_id == client.tg_id, GamePlay.mode == "bet")
    if since:
        q = q.filter(GamePlay.created_at > since)
    return int(q.scalar() or 0)


def pending_receipt(db, client: Client):
    return (db.query(WithdrawReceipt).filter(WithdrawReceipt.client_id == client.id, WithdrawReceipt.status == "pending",
                                             WithdrawReceipt.created_at >= datetime.utcnow() - timedelta(hours=24))
            .order_by(WithdrawReceipt.id.desc()).first())


def state(db, client: Client) -> dict | None:
    """None — баланс небольшой, условий нет."""
    balance = client.balance or 0
    if BIG_BALANCE <= 0 or balance < BIG_BALANCE:
        return None
    now = datetime.utcnow()
    if client.withdraw_unlocked_until and client.withdraw_unlocked_until > now:
        return {"locked": False, "by": "receipt", "min": BIG_BALANCE}
    done = turnover(db, client, last_withdraw_at(db, client))
    left = max(0, balance - done)
    last = (db.query(WithdrawReceipt).filter(WithdrawReceipt.client_id == client.id)
            .order_by(WithdrawReceipt.id.desc()).first())
    receipt = None
    if last and last.created_at >= now - timedelta(hours=24) and last.status in ("pending", "rejected"):
        receipt = last.status
    return {"locked": left > 0, "by": "turnover" if left <= 0 else None, "min": BIG_BALANCE,
            "need": balance, "done": min(done, balance), "left": left, "receipt": receipt}


def consume(client: Client) -> None:
    """Списание сделано — чек использован (коммит — на вызывающем)."""
    client.withdraw_unlocked_until = None


def can_send_receipt(db, client: Client) -> None:
    st = state(db, client)
    if st is None:
        raise ReceiptError(f"Чек нужен только при балансе от {BIG_BALANCE} колизов")
    if not st["locked"]:
        raise ReceiptError("Списание уже открыто — можно отправлять заявку")
    if pending_receipt(db, client):
        raise ReceiptError("Чек уже у администратора — дождись проверки")


def create(db, client: Client) -> WithdrawReceipt:
    r = WithdrawReceipt(client_id=client.id, tg_id=client.tg_id)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def decide(db, receipt_id: int, ok: bool, who: str) -> WithdrawReceipt | None:
    r = db.get(WithdrawReceipt, receipt_id)
    if r is None or r.status != "pending":
        return None
    r.status, r.decided_by, r.decided_at = ("approved" if ok else "rejected"), who[:80], datetime.utcnow()
    if ok:
        c = db.get(Client, r.client_id)
        if c:
            c.withdraw_unlocked_until = datetime.utcnow() + timedelta(hours=RECEIPT_HOURS)
    db.commit()
    db.refresh(r)
    return r

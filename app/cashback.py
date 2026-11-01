"""
Кэшбэк за пополнение — главный способ получить колизы.

Гость пополнил счёт в клубе → в разделе «Кэшбэк» прикрепляет фото чека (или скрин оплаты),
пишет номер, на который пополнял, и сумму → видит, сколько колизов получит по своему статусу.
Админ в чате персонала сверяет пополнение с CRM и жмёт «Начислить» — колизы приходят на баланс
приложения. Подтверждённый чек заодно открывает бесплатные игры и списание при большом балансе.

Процент — по статусу (models.TIER_CASHBACK: Silver 10, Gold 15, Premium 20); для гостей без
статуса — CASHBACK_NONE_PERCENT (по умолчанию 0).
"""
import os
from datetime import datetime, timedelta

from sqlalchemy import func

from .models import Client, CashbackClaim, TIER_CASHBACK, TIER_LABELS, get_tier, lookup_topup, normalize_phone

MIN_AMOUNT = int(os.getenv("CASHBACK_MIN_TOPUP", "100"))
MAX_AMOUNT = int(os.getenv("CASHBACK_MAX_TOPUP", "100000"))
NONE_PERCENT = int(os.getenv("CASHBACK_NONE_PERCENT", "0"))
RECEIPT_UNLOCK_HOURS = int(os.getenv("WITHDRAW_RECEIPT_HOURS", "48"))
STATUS = {"pending": "на проверке", "approved": "начислено", "rejected": "отклонено"}


class CashbackError(Exception):
    pass


def percent_for(tier: str) -> int:
    return NONE_PERCENT if tier == "none" else int(TIER_CASHBACK.get(tier, 0))


def calc(amount: int, percent: int) -> int:
    return int(amount) * int(percent) // 100


def _view(c: CashbackClaim) -> dict:
    return {"id": c.id, "amount": c.amount, "cashback": c.cashback, "percent": c.percent, "status": c.status,
            "status_label": STATUS.get(c.status, c.status), "date": f"{c.created_at + timedelta(hours=3):%d.%m %H:%M}"}


def info(db, client: Client) -> dict:
    topup, _ = lookup_topup(db, client)
    tier = get_tier(topup)
    rows = (db.query(CashbackClaim).filter(CashbackClaim.client_id == client.id)
            .order_by(CashbackClaim.id.desc()).limit(6).all())
    total = db.query(func.coalesce(func.sum(CashbackClaim.cashback), 0)).filter(
        CashbackClaim.client_id == client.id, CashbackClaim.status == "approved").scalar() or 0
    return {
        "tier": tier, "tier_label": TIER_LABELS.get(tier, tier), "percent": percent_for(tier),
        "table": [{"tier": t, "label": TIER_LABELS[t], "percent": percent_for(t)} for t in ("silver", "gold", "premium")],
        "min": MIN_AMOUNT, "max": MAX_AMOUNT, "phone": client.phone or "",
        "pending": any(r.status == "pending" for r in rows),
        "history": [_view(r) for r in rows], "total": int(total),
    }


def check_submit(db, client: Client, phone: str, amount: int) -> tuple[str, int]:
    if not client.phone_verified:
        raise CashbackError("Сначала подтверди номер телефона на главной")
    digits = normalize_phone(phone or "")
    if len(digits) < 10:
        raise CashbackError("Укажи номер, на который пополнял счёт в клубе")
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        raise CashbackError("Укажи сумму пополнения")
    if amount < MIN_AMOUNT:
        raise CashbackError(f"Кэшбэк начисляется за пополнение от {MIN_AMOUNT} ₽")
    if amount > MAX_AMOUNT:
        raise CashbackError("Проверь сумму пополнения")
    pending = db.query(CashbackClaim).filter(CashbackClaim.client_id == client.id, CashbackClaim.status == "pending").first()
    if pending:
        raise CashbackError("Предыдущий чек ещё на проверке — дождись решения администратора")
    last = db.query(func.max(CashbackClaim.created_at)).filter(CashbackClaim.client_id == client.id).scalar()
    if last and datetime.utcnow() - last < timedelta(minutes=2):
        raise CashbackError("Чек только что отправлен — подожди пару минут")
    return "+7" + digits[-10:], amount


def create(db, client: Client, phone: str, amount: int) -> CashbackClaim:
    topup, _ = lookup_topup(db, client)
    tier = get_tier(topup)
    pct = percent_for(tier)
    c = CashbackClaim(client_id=client.id, tg_id=client.tg_id, phone=phone, amount=amount, tier=tier,
                      percent=pct, cashback=calc(amount, pct))
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def decide(db, claim_id: int, ok: bool, who: str, amount: int | None = None) -> CashbackClaim | None:
    """Решение админа. amount — если сумма пополнения в CRM другая, пересчитываем кэшбэк."""
    c = db.get(CashbackClaim, claim_id)
    if c is None or c.status != "pending":
        return None
    if ok and amount:
        c.amount = int(amount)
        c.cashback = calc(c.amount, c.percent)
    c.status, c.decided_by, c.decided_at = ("approved" if ok else "rejected"), who[:80], datetime.utcnow()
    if ok:
        now = datetime.utcnow()
        cl = db.get(Client, c.client_id)
        if cl is not None:
            if c.cashback > 0:
                db.query(Client).filter(Client.id == cl.id).update(
                    {Client.balance: func.coalesce(Client.balance, 0) + c.cashback}, synchronize_session=False)
                from . import bonus_expiry
                bonus_expiry.touch(db, cl.id, now)        # свежий кэшбэк — полные сутки до сгорания
            cl.games_unlocked_at = cl.games_unlocked_at or now     # пополнение подтверждено — бесплатные игры открыты
            cl.games_locked = False
            cl.topup_check_state = "approved"
            cl.withdraw_unlocked_until = now + timedelta(hours=RECEIPT_UNLOCK_HOURS)
    db.commit()
    db.refresh(c)
    return c

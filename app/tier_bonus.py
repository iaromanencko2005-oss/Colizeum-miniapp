"""
Ежемесячный бонус уровня.

Раз в календарный месяц (по Москве) гость со статусом забирает колизы на карту гостя в CRM:
Premium — 3000, Gold — 1500, Silver — 500 (настройка TIER_BONUS="premium:3000,gold:1500,silver:500").
Кнопка «Забрать» на главной сразу отправляет персоналу заявку «начисли N бонусов в CRM».
На баланс приложения эти колизы не попадают, поэтому не сгорают и не занимают дневной лимит списаний.
Один бонус в месяц на номер телефона (с любого аккаунта). Уровень — по статистике CRM.
"""
import os
from calendar import monthrange
from datetime import datetime, timedelta

from sqlalchemy import or_

from .models import Client, TierBonus, TIER_LABELS, get_tier, lookup_topup

MSK = timedelta(hours=3)
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def _parse(raw: str) -> dict:
    out = {}
    for part in raw.split(","):
        k, _, v = part.strip().partition(":")
        if k and v.strip().isdigit():
            out[k.strip().lower()] = int(v)
    return out


AMOUNTS = _parse(os.getenv("TIER_BONUS", "premium:3000,gold:1500,silver:500"))


class TierBonusError(Exception):
    pass


def month_key(now: datetime | None = None) -> str:
    return f"{(now or datetime.utcnow()) + MSK:%Y-%m}"


def _claimed(db, client: Client, month: str):
    conds = [TierBonus.client_id == client.id]
    if client.phone_normalized:
        conds.append(TierBonus.phone_normalized == client.phone_normalized)
    return (db.query(TierBonus).filter(TierBonus.month == month, or_(*conds))
            .order_by(TierBonus.id.desc()).first())


def info(db, client: Client) -> dict | None:
    """Что показать на главной. None — карточку не показываем."""
    if not client.phone_verified:
        return None
    topup, _ = lookup_topup(db, client)
    tier = get_tier(topup)
    amount = AMOUNTS.get(tier, 0)
    now = datetime.utcnow() + MSK
    month = month_key()
    row = _claimed(db, client, month)
    if not amount and row is None:
        return None
    return {
        "tier": tier, "tier_label": TIER_LABELS.get(tier, tier),
        "amount": row.amount if row else amount,
        "month_label": MONTHS[now.month - 1],
        "until": f"{monthrange(now.year, now.month)[1]:02d}.{now.month:02d}",
        "status": row.status if row else None,          # None — можно забрать
    }


def claim(db, client: Client) -> TierBonus:
    if not client.phone_verified:
        raise TierBonusError("Сначала подтверди номер телефона")
    month = month_key()
    if _claimed(db, client, month):
        raise TierBonusError("Бонус уровня за этот месяц уже забран")
    topup, _ = lookup_topup(db, client)
    tier = get_tier(topup)
    amount = AMOUNTS.get(tier, 0)
    if not amount:
        raise TierBonusError("Бонус уровня есть у гостей со статусом Silver, Gold и Premium")
    row = TierBonus(client_id=client.id, phone_normalized=client.phone_normalized, month=month, tier=tier, amount=amount)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def decide(db, bonus_id: int, ok: bool, who: str) -> TierBonus | None:
    row = db.get(TierBonus, bonus_id)
    if row is None or row.status != "requested":
        return None
    row.status, row.decided_by, row.decided_at = ("done" if ok else "declined"), who[:80], datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row

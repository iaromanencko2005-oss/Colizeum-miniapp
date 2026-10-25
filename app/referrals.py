"""
«Приведи друга в бота»: гость пересылает другу свою ссылку на бота.
Пригласивший получает колизы в два этапа:
  1) +REFERRAL_BONUS (50) — когда друг впервые открыл кабинет, принял правила
     и подтвердил номер через Telegram;
  2) +REFERRAL_ACTIVATION_BONUS (100) — когда друг впервые пополнит счёт в клубе
     после регистрации (видно по следующей выгрузке из CRM, /import_topups).

Защита от накруток:
- друг засчитывается, только если он НОВЫЙ пользователь бота (карточка
  создаётся в момент первого согласия, и только тогда запоминается, кто пригласил);
- колиз платится только после подтверждения номера через Telegram;
- за один номер телефона колиз начисляется один раз — завести на тот же
  номер новый аккаунт и получить колиз повторно нельзя;
- пригласить самого себя нельзя.

Размер колизов меняется в Railway → Variables:
  REFERRAL_BONUS            — пригласившему за регистрацию друга (по умолчанию 50)
  REFERRAL_ACTIVATION_BONUS — пригласившему за первое пополнение друга (по умолчанию 100)
  REFERRAL_FRIEND_BONUS     — самому другу сверх подарка за регистрацию (по умолчанию 0)
"""
import os
from urllib.parse import urlencode

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from datetime import datetime

from .models import Client, ReferralReward, PhoneStat, topup_key

REFERRAL_BONUS = int(os.getenv("REFERRAL_BONUS", "50"))
REFERRAL_ACTIVATION_BONUS = int(os.getenv("REFERRAL_ACTIVATION_BONUS", "100"))
REFERRAL_FRIEND_BONUS = int(os.getenv("REFERRAL_FRIEND_BONUS", "0"))

# Имя бота (без @) — заполняется при запуске из Telegram; можно задать вручную.
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")

SHARE_TEXT = ("Залетай в COLIZEUM на Саввинской: личный кабинет клуба в Telegram — "
              "бесплатные игры на колизы каждый день и кэшбэк до 20% 🎮")


def parse_ref(raw) -> int | None:
    """'ref_12' или '12' → 12."""
    s = str(raw or "").strip()
    if s.startswith("ref_"):
        s = s[4:]
    return int(s) if s.isdigit() else None


def referral_link(client_id: int, username: str | None = None) -> str | None:
    username = (username or BOT_USERNAME or "").lstrip("@")
    if not username:
        return None
    return f"https://t.me/{username}?start=ref_{client_id}"


def share_url(link: str) -> str:
    return "https://t.me/share/url?" + urlencode({"url": link, "text": SHARE_TEXT})


def attach_referrer(db, client: Client, raw_ref) -> bool:
    """Запоминает, кто пригласил нового гостя. Вызывается только при создании
    карточки (первое согласие). Возвращает True, если пригласивший найден."""
    ref_id = parse_ref(raw_ref)
    if ref_id is None or ref_id == client.id or client.referred_by:
        return False
    referrer = db.get(Client, ref_id)
    if referrer is None or referrer.consent_at is None:
        return False
    client.referred_by = referrer.id
    return True


def invited_count(db, client: Client) -> int:
    return db.query(func.count(ReferralReward.phone_normalized)).filter(
        ReferralReward.referrer_id == client.id).scalar() or 0


def reward_referral(db, client: Client):
    """Вызывается сразу после того, как гость подтвердил номер. Если его
    пригласили и за этот номер ещё не платили — начисляет колизы.
    Возвращает (tg_id пригласившего, сумма) или None."""
    if not client.referred_by or not client.phone_verified or not client.phone_normalized:
        return None
    if db.get(ReferralReward, client.phone_normalized) is not None:
        return None   # за этот номер уже начисляли
    if db.query(ReferralReward.phone_normalized).filter(
            ReferralReward.referred_tg_id == client.tg_id).first() is not None:
        return None   # за этот аккаунт уже начисляли (даже если он сменил номер)
    referrer = db.get(Client, client.referred_by)
    if referrer is None or referrer.id == client.id:
        return None
    if referrer.phone_normalized and referrer.phone_normalized == client.phone_normalized:
        return None   # «пригласил» сам себя со второго аккаунта

    try:
        db.add(ReferralReward(phone_normalized=client.phone_normalized, referrer_id=referrer.id,
                              referred_tg_id=client.tg_id, amount=REFERRAL_BONUS,
                              base_total=crm_total(db, client)))
        db.flush()   # уникальный номер — защита от двойного начисления
        if REFERRAL_BONUS > 0:
            db.query(Client).filter(Client.id == referrer.id).update(
                {Client.balance: func.coalesce(Client.balance, 0) + REFERRAL_BONUS},
                synchronize_session=False)
        if REFERRAL_FRIEND_BONUS > 0:
            db.query(Client).filter(Client.id == client.id).update(
                {Client.balance: func.coalesce(Client.balance, 0) + REFERRAL_FRIEND_BONUS},
                synchronize_session=False)
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return referrer.tg_id, REFERRAL_BONUS


# ---------- второй этап: друг пополнил счёт в клубе ----------

def crm_total(db, client: Client) -> float:
    """Сколько гость пополнил по последней выгрузке CRM (0 — если номера там нет)."""
    key = topup_key(client)
    stat = db.get(PhoneStat, key) if key else None
    if stat is None:
        return 0.0
    return float(stat.total if stat.total is not None else (stat.avg_monthly or 0))


def check_activations(db) -> list[dict]:
    """Ищет приглашённых друзей, которые после регистрации пополнили счёт в клубе,
    и платит пригласившим второй колиз. Вызывается после каждой загрузки выгрузки
    из CRM и после подтверждения номера. Возвращает [{tg_id, friend, amount}]."""
    if REFERRAL_ACTIVATION_BONUS <= 0:
        return []
    paid = []
    rows = db.query(ReferralReward).filter(ReferralReward.activated_at.is_(None)).all()
    for r in rows:
        friend = db.query(Client).filter(Client.tg_id == r.referred_tg_id).first()
        if friend is None or not friend.phone_verified:
            continue
        now_total = crm_total(db, friend)
        if r.base_total is None:          # приглашение до этого обновления — запоминаем, что было
            r.base_total = now_total
            continue
        if now_total <= (r.base_total or 0) + 0.5:
            continue
        n = db.query(ReferralReward).filter(
            ReferralReward.phone_normalized == r.phone_normalized,
            ReferralReward.activated_at.is_(None)).update(
            {ReferralReward.activated_at: datetime.utcnow(),
             ReferralReward.activation_amount: REFERRAL_ACTIVATION_BONUS}, synchronize_session=False)
        if n != 1:
            continue
        db.query(Client).filter(Client.id == r.referrer_id).update(
            {Client.balance: func.coalesce(Client.balance, 0) + REFERRAL_ACTIVATION_BONUS},
            synchronize_session=False)
        referrer = db.get(Client, r.referrer_id)
        if referrer:
            paid.append({"tg_id": referrer.tg_id, "friend": friend.nickname or friend.tg_name or "Друг",
                         "amount": REFERRAL_ACTIVATION_BONUS})
    db.commit()
    return paid


def activated_count(db, client: Client) -> int:
    return db.query(func.count(ReferralReward.phone_normalized)).filter(
        ReferralReward.referrer_id == client.id, ReferralReward.activated_at.isnot(None)).scalar() or 0

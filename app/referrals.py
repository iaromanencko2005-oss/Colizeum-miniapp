"""
«Приведи друга в бота»: гость пересылает другу свою ссылку на бота, и когда
друг впервые открывает кабинет, принимает правила и подтверждает номер
через Telegram, пригласившему начисляются бонусы.

Защита от накруток:
- друг засчитывается, только если он НОВЫЙ пользователь бота (карточка
  создаётся в момент первого согласия, и только тогда запоминается, кто пригласил);
- бонус платится только после подтверждения номера через Telegram;
- за один номер телефона бонус начисляется один раз — завести на тот же
  номер новый аккаунт и получить бонус повторно нельзя;
- пригласить самого себя нельзя.

Размер бонусов меняется в Railway → Variables:
  REFERRAL_BONUS         — сколько получает пригласивший (по умолчанию 10)
  REFERRAL_FRIEND_BONUS  — сколько получает сам друг (по умолчанию 0)
"""
import os
from urllib.parse import urlencode

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from .models import Client, ReferralReward

REFERRAL_BONUS = int(os.getenv("REFERRAL_BONUS", "10"))
REFERRAL_FRIEND_BONUS = int(os.getenv("REFERRAL_FRIEND_BONUS", "0"))

# Имя бота (без @) — заполняется при запуске из Telegram; можно задать вручную.
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")

SHARE_TEXT = ("Залетай в COLIZEUM на Саввинской: личный кабинет клуба в Telegram — "
              "бесплатные игры на бонусы каждый день и кэшбэк до 20% 🎮")


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
    пригласили и за этот номер ещё не платили — начисляет бонусы.
    Возвращает (tg_id пригласившего, сумма) или None."""
    if not client.referred_by or not client.phone_verified or not client.phone_normalized:
        return None
    if REFERRAL_BONUS <= 0 and REFERRAL_FRIEND_BONUS <= 0:
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
                              referred_tg_id=client.tg_id, amount=REFERRAL_BONUS))
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

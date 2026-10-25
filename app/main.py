"""
Точка входа: веб-API мини-приложения + Telegram-бот в одном процессе.

Один сервис на Railway запускает:
- FastAPI-приложение, которое отдаёт данные для мини-приложения
  (уровень, баланс, акции, рулетка, кости, «Дайс», бронирование, списание);
- Telegram-бота (long polling) фоновой задачей при старте.

Все результаты игр и все изменения баланса считаются здесь, на сервере —
мини-приложение только показывает то, что вернул API. Если бы это
считалось в браузере, любой гость мог бы подделать результат.
"""
import asyncio
import base64
import binascii
import contextlib
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .database import Base, engine, get_db, SessionLocal, add_missing_columns
from .models import (
    Client, Promotion, Idea, Booking, GamePlay, Withdrawal, PhoneStat, ShopOrder, EsportsMatch, Prediction, BonusBurn,
    get_tier, get_next_tier_info, normalize_phone, lookup_topup, resync_client_topups,
    TIER_LABELS, TIER_THRESHOLDS, TIER_CASHBACK,
)
from . import referrals
from .prizes import spin as spin_roulette
from .games import (
    FREE_WINDOW, free_attempts_state, format_wait,
    free_probability_reward, roll_free_probability,
    bet_payout, roll_bet,
)
from . import bet_games as bg
from .models import CrashRound, HiloRound
from .telegram_auth import parse_and_verify, InvalidInitData
from .legal import POLICY_VERSION, consent_html, policy_html
from .bot import build_bot_and_dispatcher, GUEST_COMMANDS, battle_notify
from . import battles
from . import rating
from . import shop, checkin, group, bonus_expiry, social
from .models import TeamPost, ClubEvent, EventGoing
from . import bot_extra, bot_social

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("colizeum")

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
# Куда слать уведомления (заявки на бронь, списания, идеи): ID группы или
# нескольких чатов через запятую. Если не задано — уведомления идут админам лично.
NOTIFY_CHAT_IDS = [
    int(x) for x in os.getenv("NOTIFY_CHAT_IDS", "").replace(" ", "").split(",") if x
]

# Сколько бесплатных попыток даётся на КАЖДУЮ игру за скользящие 24 часа.
FREE_ATTEMPTS_PER_DAY = int(os.getenv("FREE_ATTEMPTS_PER_DAY", "1"))

# «Кости»: сколько колизов за каждое выпавшее очко (выпало 4 → 4 × 10 = 40).
DICE_BONUS_PER_POINT = int(os.getenv("DICE_BONUS_PER_POINT", "10"))

# Бесплатная попытка «Дайс»: реальный шанс = выбранный × этот коэффициент.
PROBABILITY_HOUSE_FACTOR = float(os.getenv("PROBABILITY_HOUSE_FACTOR", "0.3"))

# Ставки колизами в «Дайс»: комиссия клуба в процентах, заложенная в
# коэффициент выплаты, и потолок выигрыша за одну ставку.
BET_HOUSE_EDGE_PERCENT = int(os.getenv("BET_HOUSE_EDGE_PERCENT", "5"))
BET_MAX_PAYOUT = int(os.getenv("BET_MAX_PAYOUT", "3000"))
BET_MIN_CHANCE = 1
BET_MAX_CHANCE = 90

# Игры только на колизы (Crash, Слот, Больше-меньше). Бесплатных попыток нет.
# Комиссия клуба — та же BET_HOUSE_EDGE_PERCENT. Максимальная ставка считается
# сама так, чтобы выигрыш не превышал BET_MAX_PAYOUT.
CRASH_MIN_STAKE = int(os.getenv("CRASH_MIN_STAKE", "20"))
CRASH_MAX_MULTIPLIER = float(os.getenv("CRASH_MAX_MULTIPLIER", "10"))
SLOTS_MIN_STAKE = int(os.getenv("SLOTS_MIN_STAKE", "10"))
HILO_MIN_STAKE = int(os.getenv("HILO_MIN_STAKE", "10"))


def max_stake_for(max_mult: float) -> int:
    return max(1, int(BET_MAX_PAYOUT // max_mult))


CRASH_MAX_STAKE = max_stake_for(CRASH_MAX_MULTIPLIER)
SLOTS_MAX_STAKE = max_stake_for(bg.SLOT_MAX_MULT)
HILO_MAX_STAKE = max_stake_for(bg.hilo_max_coef(BET_HOUSE_EDGE_PERCENT))

# Правила списания колизов (Railway → Variables):
#   WITHDRAW_MIN          — меньше этого списать нельзя (чтобы не дёргали по 50 колизов);
#   WITHDRAW_LIMIT        — сколько всего можно списать на ОДИН НОМЕР за период —
#                           считается по номеру, поэтому второй аккаунт с тем же
#                           номером лимит не обходит;
#   WITHDRAW_WINDOW_HOURS — длина периода в часах: 24 = сутки, 168 = неделя.
WITHDRAW_MIN = int(os.getenv("WITHDRAW_MIN", "50"))
WITHDRAW_LIMIT = int(os.getenv("WITHDRAW_LIMIT", "300"))
WITHDRAW_WINDOW_HOURS = int(os.getenv("WITHDRAW_WINDOW_HOURS", "24"))

GAMES = ("spin", "dice", "probability")

Base.metadata.create_all(bind=engine)
add_missing_columns()


def _startup_resync():
    db = SessionLocal()
    try:
        changed = resync_client_topups(db)
        if changed:
            logger.info("Уровни гостей сверены со статистикой CRM, обновлено: %s", changed)
    finally:
        db.close()


_startup_resync()


# ---------- постоянные акции клуба ----------

DEFAULT_PROMOTIONS = [
    ("Счастливые часы", "-20% на все тарифы каждый будний день до 14:00", None),
    ("День PS", "Каждый понедельник -50% до 14:00", None),
    ("День рождения",
     "Бесплатный час игры в день рождения — покажи админу паспорт. "
     "Бесплатный час получают все приглашённые", None),
    ("Приведи друга", "По 200 бонусов тебе и другу за первое посещение по твоей рекомендации", None),
    ("Рулетка", "Пополни от 2000 ₽, угадай цвет — и получи 1000 бонусов", None),
    ("Ночной пакет", "-15% на короткую ночь — с 22:00 до 6:00", None),
    ("Ночной четверг", "-50% на ночной пакет по четвергам", None),
    ("Напиток в подарок", "Любой напиток в подарок при покупке ночного пакета", None),
    ("Ночь в подарок", "Каждая шестая ночь подряд — в подарок", None),
    ("Отзыв на картах", "Оставь отзыв о клубе на Яндекс.Картах и получи 100 бонусных рублей",
     "https://yandex.ru/maps/org/colizeum/227193289093/reviews/?ll=37.568227%2C55.738865&z=16.56"),
]


def _seed_default_promotions():
    """Добавляет постоянные акции, которых ещё нет в базе (сверка по названию).
    Акции, добавленные через /add_promo, не трогает. Безопасно при каждом запуске."""
    db = SessionLocal()
    try:
        existing = {t for (t,) in db.query(Promotion.title).all()}
        new = [
            Promotion(title=title, description=desc, link=link)
            for title, desc, link in DEFAULT_PROMOTIONS
            if title not in existing
        ]
        if new:
            db.add_all(new)
            db.commit()
            logger.info("Добавлено постоянных акций: %s", len(new))
    finally:
        db.close()


_seed_default_promotions()


def _seed_shop():
    db = SessionLocal()
    try:
        added = shop.seed_catalog(db)
        if added:
            logger.info("Магазин: добавлено товаров из каталога: %s", added)
    except Exception as e:  # noqa: BLE001 — магазин не должен мешать запуску
        logger.warning("Магазин: не удалось загрузить каталог: %s", e)
    finally:
        db.close()


_seed_shop()

app = FastAPI(title="COLIZEUM Mini App API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # мини-апп открывается внутри Telegram — ограничивать источник не нужно
    allow_methods=["*"],
    allow_headers=["*"],
)

webapp_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "webapp")
if os.path.isdir(webapp_dir):
    app.mount("/webapp", StaticFiles(directory=webapp_dir, html=True), name="webapp")


# ---------- схемы запросов ----------

class InitDataPayload(BaseModel):
    initData: str


class LinkPhonePayload(BaseModel):
    initData: str
    phone: str


class ProbabilityPayload(BaseModel):
    initData: str
    threshold: int  # 1..90 — шанс выигрыша, который выставил гость ползунком


class BetPayload(BaseModel):
    initData: str
    threshold: int  # 1..90
    stake: int      # сколько колизов ставит гость


class ConsentPayload(BaseModel):
    initData: str
    ref: str = ""   # реферальная метка из ссылки друга (ref_<id>)


class WithdrawPayload(BaseModel):
    initData: str
    name: str
    phone: str = ""  # не используется: колизы уходят только на подтверждённый номер


class IdeaPayload(BaseModel):
    initData: str
    text: str


class BookingPayload(BaseModel):
    initData: str
    name: str
    phone: str
    time_text: str
    seats_text: str = ""
    notes: str = ""


class AdminTopupPayload(BaseModel):
    secret: str
    phone: str
    monthly_topup: float


# ---------- вспомогательное ----------

def verify(init_data: str) -> dict:
    try:
        return parse_and_verify(init_data)
    except InvalidInitData as e:
        raise HTTPException(status_code=401, detail=str(e))


def get_or_create_client(db: Session, tg_user: dict) -> Client:
    tg_id = tg_user["id"]
    client = db.query(Client).filter(Client.tg_id == tg_id).first()
    if not client:
        name = tg_user.get("first_name") or tg_user.get("username") or ""
        client = Client(tg_id=tg_id, tg_name=name, monthly_topup=0.0, balance=0,
                        last_seen_at=datetime.utcnow())
        db.add(client)
        db.commit()
        db.refresh(client)
    else:
        # Отмечаем визит при каждом открытии мини-аппа.
        client.last_seen_at = datetime.utcnow()
        db.commit()
    return client


def consented_client(db: Session, tg_user: dict) -> Client:
    """Гость, который дал согласие на обработку ПДн. Без согласия сервер
    не принимает от гостя никаких данных и ничего о нём не записывает."""
    client = db.query(Client).filter(Client.tg_id == tg_user["id"]).first()
    if client is None or client.consent_at is None:
        raise HTTPException(status_code=403, detail="Сначала подтверди согласие на обработку данных")
    client.last_seen_at = datetime.utcnow()
    if tg_user.get("username") and client.tg_username != tg_user["username"]:
        client.tg_username = tg_user["username"]
    db.commit()
    return client


def free_state(db: Session, tg_id: int, game: str):
    now = datetime.utcnow()
    times = [
        t for (t,) in db.query(GamePlay.created_at).filter(
            GamePlay.tg_id == tg_id,
            GamePlay.game == game,
            GamePlay.mode == "free",
            GamePlay.created_at >= now - FREE_WINDOW,
        ).all()
    ]
    left, next_at = free_attempts_state(times, FREE_ATTEMPTS_PER_DAY, now)
    return left, next_at, now


def require_free_attempt(db: Session, tg_id: int, game: str):
    left, next_at, now = free_state(db, tg_id, game)
    if left <= 0:
        wait = f" — следующая через {format_wait(next_at - now)}" if next_at else ""
        raise HTTPException(status_code=429, detail=f"Бесплатные попытки закончились{wait}")


def change_balance(db: Session, client: Client, delta: int, require_at_least: int = 0) -> bool:
    """Атомарно меняет баланс. Если require_at_least > 0 — списание пройдёт,
    только если на балансе не меньше этой суммы (защита от двойного нажатия)."""
    q = db.query(Client).filter(Client.id == client.id)
    if require_at_least > 0:
        q = q.filter(func.coalesce(Client.balance, 0) >= require_at_least)
    updated = q.update(
        {Client.balance: func.coalesce(Client.balance, 0) + delta},
        synchronize_session=False,
    )
    return updated == 1


def require_verified(client: Client):
    """Игры и списание — только с номером, подтверждённым через Telegram.
    Так один человек не может завести несколько аккаунтов и слить колизы на один номер."""
    if not client.phone_verified:
        raise HTTPException(status_code=403, detail="Сначала подтверди номер телефона в кабинете")


def window_label() -> str:
    if WITHDRAW_WINDOW_HOURS == 24:
        return "в сутки"
    if WITHDRAW_WINDOW_HOURS == 168:
        return "в неделю"
    if WITHDRAW_WINDOW_HOURS % 24 == 0:
        return f"за {WITHDRAW_WINDOW_HOURS // 24} дн."
    return f"за {WITHDRAW_WINDOW_HOURS} ч"


def recent_withdrawals(db: Session, client: Client, now: datetime):
    """Траты колизов за текущие сутки — заявки на списание И заказы в магазине.
    Тратить можно ОДИН раз в сутки: либо списание, либо магазин. Считается по номеру
    телефона (с любого аккаунта) и по самому аккаунту. Отменённые заказы не считаются.
    Возвращает [(время, сумма, "withdraw" | "shop")] по возрастанию времени."""
    conds = [Withdrawal.tg_id == client.tg_id]
    if client.phone_normalized:
        conds.append(Withdrawal.phone_normalized == client.phone_normalized)
    if client.phone:
        conds.append(Withdrawal.phone == client.phone)
    since = now - timedelta(hours=WITHDRAW_WINDOW_HOURS)
    rows = [(t, int(a), "withdraw") for t, a in db.query(Withdrawal.created_at, Withdrawal.amount)
            .filter(or_(*conds), Withdrawal.created_at >= since).all()]
    oconds = [ShopOrder.tg_id == client.tg_id]
    if client.phone_normalized:
        oconds.append(ShopOrder.phone_normalized == client.phone_normalized)
    rows += [(t, int(a), "shop") for t, a in db.query(ShopOrder.created_at, ShopOrder.total)
             .filter(or_(*oconds), ShopOrder.created_at >= since,
                     ShopOrder.status.notin_(["cancelled", "expired"])).all()]
    return sorted(rows, key=lambda r: r[0])


def withdraw_info(db: Session, client: Client) -> dict:
    """Можно ли сейчас тратить колизы. Одна трата в сутки — списание (от WITHDRAW_MIN
    до WITHDRAW_LIMIT) ИЛИ заказ в магазине (до WITHDRAW_LIMIT)."""
    now = datetime.utcnow()
    balance = client.balance or 0
    rows = recent_withdrawals(db, client, now) if client.phone_verified else []
    used = sum(a for _, a, _ in rows)
    spent_kind = rows[-1][2] if rows else None
    next_in = format_wait(rows[-1][0] + timedelta(hours=WITHDRAW_WINDOW_HOURS) - now) if rows else None
    remaining = 0 if rows else WITHDRAW_LIMIT

    reason = None
    if not client.phone_verified:
        reason = "Подтверди номер телефона, чтобы списывать колизы"
    elif rows:
        what = "уже купил(а) в магазине" if spent_kind == "shop" else "уже списал(а) колизы"
        reason = (f"Сегодня ты {what}. Тратить колизы можно раз {window_label()}: либо списание, либо магазин"
                  + (f" — следующий раз через {next_in}" if next_in else ""))
    elif balance < WITHDRAW_MIN:
        reason = f"Списать можно от {WITHDRAW_MIN} колизов"

    return {
        "available": reason is None,
        "reason": reason,
        "amount": min(balance, WITHDRAW_LIMIT),
        "min": WITHDRAW_MIN,
        "limit": WITHDRAW_LIMIT,
        "used": used,
        "remaining": remaining,
        "window_label": window_label(),
        "next_free_in": next_in,
        "spent_kind": spent_kind,
    }


def client_to_dict(db: Session, client: Client) -> dict:
    # Уровень — по свежей статистике CRM и только после подтверждения номера через Telegram.
    topup, crm_found = lookup_topup(db, client)
    tier = get_tier(topup)
    next_tier, remaining = get_next_tier_info(topup)

    games = {}
    for game in GAMES:
        left, next_at, now = free_state(db, client.tg_id, game)
        games[game] = {
            "left": left,
            "total": FREE_ATTEMPTS_PER_DAY,
            "next_free_in": format_wait(next_at - now) if next_at else None,
        }

    return {
        "consent_required": False,
        "name": client.tg_name,
        "nickname": client.nickname,
        "welcome_bonus": rating.WELCOME_BONUS if rating.welcome_pending(db, client) else 0,
        "phone": client.phone if client.phone_verified else None,
        "phone_verified": bool(client.phone_verified),
        "balance": client.balance or 0,
        "bonus_expires_at": (lambda d: d.isoformat() + "Z" if d else None)(bonus_expiry.deadline_for(client)),
        "bonus_ttl_hours": bonus_expiry.BONUS_TTL_HOURS,
        "monthly_topup": topup,
        "crm_found": crm_found,
        "withdraw": withdraw_info(db, client),
        "referral": {
            "link": referrals.referral_link(client.id),
            "bonus": referrals.REFERRAL_BONUS,
            "activation_bonus": referrals.REFERRAL_ACTIVATION_BONUS,
            "invited": referrals.invited_count(db, client),
            "activated": referrals.activated_count(db, client),
        },
        "checkin": checkin.view(db, client),
        "visits": social.visit_progress(db, client),
        "profile": {"about": client.about or "", "games": social.games_of(client), "rank": client.rank or "",
                    "all_games": social.GAMES},
        "shop_active_orders": db.query(func.count(ShopOrder.id)).filter(
            ShopOrder.client_id == client.id, ShopOrder.status == "new").scalar() or 0,
        "tier": tier,
        "tier_label": TIER_LABELS[tier],
        "cashback_percent": TIER_CASHBACK[tier],
        "next_tier": TIER_LABELS.get(next_tier) if next_tier else None,
        "remaining_to_next_tier": remaining,
        "games": games,
        "dice_bonus_per_point": DICE_BONUS_PER_POINT,
        "bet_config": {
            "house_edge_percent": BET_HOUSE_EDGE_PERCENT,
            "max_payout": BET_MAX_PAYOUT,
            "min_chance": BET_MIN_CHANCE,
            "max_chance": BET_MAX_CHANCE,
        },
        "bet_games": {
            "crash": {"min": CRASH_MIN_STAKE, "max": CRASH_MAX_STAKE,
                      "max_mult": CRASH_MAX_MULTIPLIER, "k": bg.CRASH_K},
            "slots": {"min": SLOTS_MIN_STAKE, "max": SLOTS_MAX_STAKE, "paytable": bg.slot_paytable()},
            "hilo": {"min": HILO_MIN_STAKE, "max": HILO_MAX_STAKE},
        },
        "crash_active": crash_active_dict(db, client.tg_id),
        "hilo_active": hilo_active_dict(db, client.tg_id),
    }


async def notify_admins(text: str):
    """Шлёт уведомление в NOTIFY_CHAT_IDS (например, в группу клуба), а если
    эта настройка пустая — лично каждому из ADMIN_IDS. Если бот не запущен
    или чат недоступен — просто пишет в лог, запрос гостя не падает."""
    if _bot_instance is None:
        return
    targets = NOTIFY_CHAT_IDS or list(ADMIN_IDS)
    for chat_id in targets:
        try:
            await _bot_instance.send_message(chat_id, text)
        except Exception as e:  # noqa: BLE001
            logger.warning("Не удалось отправить уведомление в чат %s: %s", chat_id, e)


# ---------- эндпоинты мини-приложения ----------

@app.get("/api/tiers")
def api_tiers():
    return [
        {
            "tier": name,
            "label": TIER_LABELS[name],
            "threshold": threshold,
            "cashback_percent": TIER_CASHBACK[name],
        }
        for name, threshold in TIER_THRESHOLDS
        if name != "none"   # «Без статуса» — не уровень программы, в списке уровней не показываем
    ]


@app.post("/api/me")
def api_me(payload: InitDataPayload, db: Session = Depends(get_db)):
    tg_user = verify(payload.initData)
    client = db.query(Client).filter(Client.tg_id == tg_user["id"]).first()
    if client is None or client.consent_at is None:
        # Ничего не записываем, пока гость не дал согласие — только просим его.
        return {"consent_required": True}
    client = consented_client(db, tg_user)
    return client_to_dict(db, client)


@app.post("/api/consent")
def api_consent(payload: ConsentPayload, db: Session = Depends(get_db)):
    """Гость поставил галочку «Согласен» — фиксируем дату и редакцию политики.
    Если гость пришёл по ссылке друга и он НОВЫЙ — запоминаем, кто пригласил
    (колиз пригласившему начислится, когда гость подтвердит номер)."""
    tg_user = verify(payload.initData)
    is_new = db.query(Client.id).filter(Client.tg_id == tg_user["id"]).first() is None
    client = get_or_create_client(db, tg_user)
    if client.consent_at is None:
        client.consent_at = datetime.utcnow()
        client.consent_version = POLICY_VERSION
        if is_new and payload.ref:
            referrals.attach_referrer(db, client, payload.ref)
        db.commit()
    return client_to_dict(db, client)


@app.get("/api/policy")
def api_policy():
    return {"version": POLICY_VERSION, "consent_html": consent_html(), "policy_html": policy_html()}


@app.post("/api/link-phone")
def api_link_phone(payload: LinkPhonePayload, db: Session = Depends(get_db)):
    """Ручной ввод номера отключён: номер подтверждается только через Telegram
    (кнопка «Поделиться номером» → бот получает контакт, см. bot.py)."""
    raise HTTPException(status_code=410, detail="Номер подтверждается кнопкой «Поделиться номером»")


@app.get("/api/promotions")
def api_promotions(db: Session = Depends(get_db)):
    """Все активные акции клуба — не зависят от уровня клиента."""
    promos = (
        db.query(Promotion)
        .filter(Promotion.active == True)  # noqa: E712
        .order_by(Promotion.id.asc())
        .all()
    )
    return [
        {"id": p.id, "title": p.title, "description": p.description, "link": p.link}
        for p in promos
    ]


@app.post("/api/spin")
def api_spin(payload: InitDataPayload, db: Session = Depends(get_db)):
    tg_user = verify(payload.initData)
    client = consented_client(db, tg_user)
    require_verified(client)
    require_free_attempt(db, client.tg_id, "spin")

    prize = spin_roulette()
    change_balance(db, client, prize.value)
    db.add(GamePlay(tg_id=client.tg_id, game="spin", mode="free", payout=prize.value))
    db.commit()
    db.refresh(client)

    return {"prize_id": prize.id, "prize_label": prize.label, "value": prize.value,
            "balance": client.balance}


@app.post("/api/dice")
def api_dice(payload: InitDataPayload, db: Session = Depends(get_db)):
    tg_user = verify(payload.initData)
    client = consented_client(db, tg_user)
    require_verified(client)
    require_free_attempt(db, client.tg_id, "dice")

    value = random.randint(1, 6)
    bonus = value * DICE_BONUS_PER_POINT
    change_balance(db, client, bonus)
    db.add(GamePlay(tg_id=client.tg_id, game="dice", mode="free", payout=bonus))
    db.commit()
    db.refresh(client)

    return {"value": value, "bonus": bonus, "balance": client.balance}


@app.post("/api/probability")
def api_probability(payload: ProbabilityPayload, db: Session = Depends(get_db)):
    """«Дайс», бесплатная попытка."""
    tg_user = verify(payload.initData)
    client = consented_client(db, tg_user)
    require_verified(client)
    require_free_attempt(db, client.tg_id, "probability")

    chance = max(BET_MIN_CHANCE, min(BET_MAX_CHANCE, payload.threshold))
    rolled, win = roll_free_probability(chance, PROBABILITY_HOUSE_FACTOR)
    reward = free_probability_reward(chance) if win else 0
    if reward:
        change_balance(db, client, reward)
    db.add(GamePlay(tg_id=client.tg_id, game="probability", mode="free", payout=reward))
    db.commit()
    db.refresh(client)

    return {"win": win, "rolled_number": rolled, "reward": reward, "balance": client.balance}


@app.post("/api/probability/bet")
def api_probability_bet(payload: BetPayload, db: Session = Depends(get_db)):
    """«Дайс», ставка колизами. Без лимита попыток — ограничен только балансом."""
    tg_user = verify(payload.initData)
    client = consented_client(db, tg_user)
    require_verified(client)

    chance = payload.threshold
    stake = payload.stake
    if not (BET_MIN_CHANCE <= chance <= BET_MAX_CHANCE):
        raise HTTPException(status_code=400, detail=f"Шанс — от {BET_MIN_CHANCE} до {BET_MAX_CHANCE}%")
    if stake < 1:
        raise HTTPException(status_code=400, detail="Ставка — минимум 1 колиз")

    payout_if_win = bet_payout(stake, chance, BET_HOUSE_EDGE_PERCENT)
    if payout_if_win > BET_MAX_PAYOUT:
        raise HTTPException(
            status_code=400,
            detail=f"Максимальный выигрыш за одну ставку — {BET_MAX_PAYOUT} колизов. "
                   f"Уменьши ставку или повысь шанс.",
        )

    # Сначала списываем ставку (атомарно — если колизов не хватает, ничего не спишется).
    if not change_balance(db, client, -stake, require_at_least=stake):
        db.rollback()
        raise HTTPException(status_code=400, detail="Недостаточно колизов на балансе")

    rolled, win = roll_bet(chance)
    payout = payout_if_win if win else 0
    if payout:
        change_balance(db, client, payout)

    db.add(GamePlay(tg_id=client.tg_id, game="probability", mode="bet", stake=stake, payout=payout))
    db.commit()
    db.refresh(client)
    if payout:
        note_big_win(db, client.tg_id, "probability", stake, payout)

    return {
        "win": win,
        "rolled_number": rolled,
        "stake": stake,
        "payout": payout,
        "net": payout - stake,
        "balance": client.balance,
    }


# ---------- игры только на колизы: общее ----------

def credit_by_tg(db: Session, tg_id: int, amount: int):
    if amount:
        db.query(Client).filter(Client.tg_id == tg_id).update(
            {Client.balance: func.coalesce(Client.balance, 0) + amount}, synchronize_session=False)


def take_stake(db: Session, client: Client, stake: int, min_stake: int, max_stake: int):
    """Проверяет ставку и атомарно списывает её с баланса."""
    if stake < min_stake:
        raise HTTPException(status_code=400, detail=f"Минимальная ставка — {min_stake} колизов")
    if stake > max_stake:
        raise HTTPException(status_code=400, detail=f"Максимальная ставка — {max_stake} колизов")
    if not change_balance(db, client, -stake, require_at_least=stake):
        db.rollback()
        raise HTTPException(status_code=400, detail="Недостаточно колизов на балансе")


def note_big_win(db: Session, tg_id: int, game: str, stake: int, payout: int, mult: float | None = None):
    """Крупный выигрыш — в очередь на пост в группу клуба (если включено)."""
    if not group.enabled() or payout < group.BIG_WIN_MIN:
        return
    nick = db.query(Client.nickname).filter(Client.tg_id == tg_id).scalar()
    group.big_win(nick, game, stake, payout, mult)


def current_balance(db: Session, tg_id: int) -> int:
    return db.query(func.coalesce(Client.balance, 0)).filter(Client.tg_id == tg_id).scalar() or 0


# ---------- Crash ----------

class CrashStartPayload(BaseModel):
    initData: str
    stake: int
    auto_cashout: float | None = None


class CrashRoundPayload(BaseModel):
    initData: str
    round_id: int


def settle_crash(db: Session, rnd: CrashRound, cashout: bool = False) -> CrashRound:
    """Закрывает раунд, если он закончился (краш, автовывод) или гость нажал «Забрать».
    Закрытие атомарное: даже два одновременных запроса не заплатят дважды."""
    if rnd.status != "running":
        return rnd
    now = datetime.utcnow()
    elapsed = (now - rnd.started_at).total_seconds()
    status, mult = bg.crash_outcome(rnd.crash_point, rnd.auto_cashout, CRASH_MAX_MULTIPLIER, elapsed, cashout)
    if status == "running":
        return rnd
    payout = bg.payout_for(rnd.stake, mult) if status == "cashed" else 0
    updated = db.query(CrashRound).filter(CrashRound.id == rnd.id, CrashRound.status == "running").update(
        {CrashRound.status: status, CrashRound.cashout_mult: mult if status == "cashed" else None,
         CrashRound.payout: payout, CrashRound.finished_at: now},
        synchronize_session=False)
    if updated == 1:
        credit_by_tg(db, rnd.tg_id, payout)
        db.add(GamePlay(tg_id=rnd.tg_id, game="crash", mode="bet", stake=rnd.stake, payout=payout))
    db.commit()
    db.refresh(rnd)
    if updated == 1 and payout:
        note_big_win(db, rnd.tg_id, "crash", rnd.stake, payout, mult)
    return rnd


def crash_round_dict(db: Session, rnd: CrashRound) -> dict:
    d = {"round_id": rnd.id, "status": rnd.status, "stake": rnd.stake,
         "auto_cashout": rnd.auto_cashout, "k": bg.CRASH_K, "max_mult": CRASH_MAX_MULTIPLIER}
    if rnd.status == "running":
        elapsed = (datetime.utcnow() - rnd.started_at).total_seconds()
        d.update(elapsed=elapsed, mult=bg.crash_mult_at(elapsed))
    else:
        # Точку краха раскрываем только после окончания раунда
        d.update(crash_point=bg.crash_display_point(rnd.crash_point, CRASH_MAX_MULTIPLIER),
                 capped=rnd.crash_point > CRASH_MAX_MULTIPLIER,
                 cashout_mult=rnd.cashout_mult, payout=rnd.payout or 0,
                 balance=current_balance(db, rnd.tg_id))
    return d


def crash_active_dict(db: Session, tg_id: int):
    rnd = None
    for r in db.query(CrashRound).filter(CrashRound.tg_id == tg_id, CrashRound.status == "running").all():
        r = settle_crash(db, r)
        if r.status == "running":
            rnd = r
    return crash_round_dict(db, rnd) if rnd else None


def own_crash_round(db: Session, tg_id: int, round_id: int) -> CrashRound:
    rnd = db.get(CrashRound, round_id)
    if rnd is None or rnd.tg_id != tg_id:
        raise HTTPException(status_code=404, detail="Раунд не найден")
    return rnd


@app.post("/api/crash/start")
def api_crash_start(payload: CrashStartPayload, db: Session = Depends(get_db)):
    tg_user = verify(payload.initData)
    client = consented_client(db, tg_user)
    require_verified(client)
    if crash_active_dict(db, client.tg_id):
        raise HTTPException(status_code=409, detail="Сначала дождись конца текущего раунда")

    auto = payload.auto_cashout
    if auto is not None:
        auto = bg.floor2(auto)
        if not (1.01 <= auto <= CRASH_MAX_MULTIPLIER):
            raise HTTPException(status_code=400,
                                detail=f"Автовывод — от ×1.01 до ×{CRASH_MAX_MULTIPLIER:g}")
    take_stake(db, client, payload.stake, CRASH_MIN_STAKE, CRASH_MAX_STAKE)
    rnd = CrashRound(tg_id=client.tg_id, stake=payload.stake, auto_cashout=auto,
                     crash_point=bg.crash_draw_point(BET_HOUSE_EDGE_PERCENT, CRASH_MAX_MULTIPLIER),
                     status="running", started_at=datetime.utcnow())
    db.add(rnd)
    db.commit()
    db.refresh(rnd)
    d = crash_round_dict(db, rnd)
    d["balance"] = current_balance(db, client.tg_id)
    return d


@app.post("/api/crash/state")
def api_crash_state(payload: CrashRoundPayload, db: Session = Depends(get_db)):
    tg_user = verify(payload.initData)
    rnd = settle_crash(db, own_crash_round(db, tg_user["id"], payload.round_id))
    return crash_round_dict(db, rnd)


@app.post("/api/crash/cashout")
def api_crash_cashout(payload: CrashRoundPayload, db: Session = Depends(get_db)):
    tg_user = verify(payload.initData)
    rnd = settle_crash(db, own_crash_round(db, tg_user["id"], payload.round_id), cashout=True)
    return crash_round_dict(db, rnd)


@app.get("/api/crash/history")
def api_crash_history(db: Session = Depends(get_db)):
    """Последние точки краха по всем гостям — для полоски истории над графиком."""
    rows = (db.query(CrashRound.crash_point).filter(CrashRound.status != "running")
            .order_by(CrashRound.finished_at.desc()).limit(14).all())
    return [bg.crash_display_point(p, CRASH_MAX_MULTIPLIER) for (p,) in rows]


# ---------- Слот ----------

class StakePayload(BaseModel):
    initData: str
    stake: int


@app.post("/api/slots/spin")
def api_slots_spin(payload: StakePayload, db: Session = Depends(get_db)):
    tg_user = verify(payload.initData)
    client = consented_client(db, tg_user)
    require_verified(client)
    take_stake(db, client, payload.stake, SLOTS_MIN_STAKE, SLOTS_MAX_STAKE)

    reels = bg.slot_spin()
    mult, label, positions = bg.slot_evaluate(reels)
    payout = bg.payout_for(payload.stake, mult)
    credit_by_tg(db, client.tg_id, payout)
    db.add(GamePlay(tg_id=client.tg_id, game="slots", mode="bet", stake=payload.stake, payout=payout))
    db.commit()
    note_big_win(db, client.tg_id, "slots", payload.stake, payout, mult)
    return {"reels": reels, "mult": mult, "label": label, "positions": positions,
            "payout": payout, "net": payout - payload.stake, "balance": current_balance(db, client.tg_id)}


# ---------- Больше-меньше ----------

class HiloGuessPayload(BaseModel):
    initData: str
    round_id: int
    choice: str   # higher | lower


def hilo_round_dict(db: Session, rnd: HiloRound) -> dict:
    d = {"round_id": rnd.id, "status": rnd.status, "stake": rnd.stake,
         "dealer": bg.card_info(rnd.dealer_card),
         "coefs": bg.hilo_coefs(rnd.dealer_card, BET_HOUSE_EDGE_PERCENT),
         "balance": current_balance(db, rnd.tg_id)}
    if rnd.status != "open":
        d.update(choice=rnd.choice, player=bg.card_info(rnd.player_card), coef=rnd.coef, payout=rnd.payout or 0)
    return d


def hilo_active_dict(db: Session, tg_id: int):
    rnd = db.query(HiloRound).filter(HiloRound.tg_id == tg_id, HiloRound.status == "open").first()
    return hilo_round_dict(db, rnd) if rnd else None


@app.post("/api/hilo/deal")
def api_hilo_deal(payload: StakePayload, db: Session = Depends(get_db)):
    """Ставка → клуб открывает свою карту. Если открытый раунд уже есть
    (гость закрыл приложение на середине), возвращаем его без новой ставки."""
    tg_user = verify(payload.initData)
    client = consented_client(db, tg_user)
    require_verified(client)
    active = hilo_active_dict(db, client.tg_id)
    if active:
        active["resumed"] = True
        return active
    take_stake(db, client, payload.stake, HILO_MIN_STAKE, HILO_MAX_STAKE)
    rnd = HiloRound(tg_id=client.tg_id, stake=payload.stake, dealer_card=bg.hilo_deal(), status="open")
    db.add(rnd)
    db.commit()
    db.refresh(rnd)
    return hilo_round_dict(db, rnd)


@app.post("/api/hilo/guess")
def api_hilo_guess(payload: HiloGuessPayload, db: Session = Depends(get_db)):
    tg_user = verify(payload.initData)
    rnd = db.get(HiloRound, payload.round_id)
    if rnd is None or rnd.tg_id != tg_user["id"]:
        raise HTTPException(status_code=404, detail="Раунд не найден")
    if rnd.status != "open":
        return hilo_round_dict(db, rnd)
    if payload.choice not in ("higher", "lower"):
        raise HTTPException(status_code=400, detail="Выбери «Больше» или «Меньше»")
    coef = bg.hilo_coefs(rnd.dealer_card, BET_HOUSE_EDGE_PERCENT)[payload.choice]
    if coef is None:
        raise HTTPException(status_code=400, detail="Этот вариант невозможен для такой карты")

    player = bg.hilo_draw(rnd.dealer_card)
    win = bg.hilo_wins(rnd.dealer_card, player, payload.choice)
    payout = bg.payout_for(rnd.stake, coef) if win else 0
    updated = db.query(HiloRound).filter(HiloRound.id == rnd.id, HiloRound.status == "open").update(
        {HiloRound.status: "won" if win else "lost", HiloRound.choice: payload.choice,
         HiloRound.player_card: player, HiloRound.coef: coef, HiloRound.payout: payout,
         HiloRound.finished_at: datetime.utcnow()}, synchronize_session=False)
    if updated == 1:
        credit_by_tg(db, rnd.tg_id, payout)
        db.add(GamePlay(tg_id=rnd.tg_id, game="hilo", mode="bet", stake=rnd.stake, payout=payout))
    db.commit()
    db.refresh(rnd)
    if updated == 1 and payout:
        note_big_win(db, rnd.tg_id, "hilo", rnd.stake, payout, coef)
    return hilo_round_dict(db, rnd)


# ---------- батлы «гость против гостя» ----------

class BattleInvitePayload(BaseModel):
    initData: str
    phone: str


class BattleIdPayload(BaseModel):
    initData: str
    battle_id: int


class BattleRespondPayload(BaseModel):
    initData: str
    battle_id: int
    accept: bool
    game: str | None = None     # для принятия предложения: что именно гость видел на экране
    stake: int | None = None


class BattleOfferPayload(BaseModel):
    initData: str
    battle_id: int
    game: str
    stake: int


class BattleActionPayload(BaseModel):
    initData: str
    battle_id: int
    action: str
    amount: int | None = None


def battle_player(db: Session, init_data: str) -> Client:
    client = consented_client(db, verify(init_data))
    if not client.phone_verified:
        raise HTTPException(status_code=403, detail="Для батлов нужно подтвердить номер телефона в кабинете")
    return client


def run_battle(fn, *args):
    """Выполняет действие батла под общим замком и превращает ошибки правил в понятный ответ."""
    try:
        with battles.LOCK:
            return fn(*args)
    except battles.BattleError as e:
        raise HTTPException(status_code=400, detail=str(e))


async def notify_bot(client_id, text, battle_id=None, invite=False):
    if _bot_instance is not None and client_id:
        await battle_notify(_bot_instance, client_id, text, battle_id, invite)


@app.post("/api/battle/invite")
async def api_battle_invite(payload: BattleInvitePayload, db: Session = Depends(get_db)):
    me = battle_player(db, payload.initData)
    b, target, created = run_battle(battles.invite, db, me, payload.phone)
    if target is not None and created:
        await notify_bot(target.id, f"⚔️ {battles.first_name(me)} вызывает тебя на батл в COLIZEUM!\n"
                                    "Сыграете в 21, Crash-дуэль или покер на колизы.", b.id, invite=True)
    return {"ok": True, "battle_id": b.id if (not created and b.status == "active") else None,
            "message": "Если этот номер подтверждён в боте, человеку придёт вызов. "
                       "Как только он примет — батл появится в списке."}


@app.post("/api/battle/list")
def api_battle_list(payload: InitDataPayload, db: Session = Depends(get_db)):
    me = battle_player(db, payload.initData)
    return run_battle(battles.list_view, db, me)


@app.post("/api/battle/respond")
async def api_battle_respond(payload: BattleRespondPayload, db: Session = Depends(get_db)):
    me = battle_player(db, payload.initData)
    b = run_battle(battles.respond, db, me, payload.battle_id, payload.accept)
    name = battles.first_name(me)
    await notify_bot(b.inviter_id, f"⚔️ {name} принял(а) вызов! Выбирай игру и ставку." if payload.accept
                     else f"{name} отказался(ась) от батла.", b.id if payload.accept else None)
    return run_battle(battles.battle_view, db, b, me) if payload.accept else {"ok": True}


@app.post("/api/battle/state")
def api_battle_state(payload: BattleIdPayload, db: Session = Depends(get_db)):
    me = battle_player(db, payload.initData)

    def _state():
        b, _ = battles.get_battle(db, payload.battle_id, me)
        if b.match_id:
            m = db.get(battles.BattleMatch, b.match_id)
            if m:
                battles.tick(db, m)
        return battles.battle_view(db, b, me)
    return run_battle(_state)


@app.post("/api/battle/offer")
async def api_battle_offer(payload: BattleOfferPayload, db: Session = Depends(get_db)):
    me = battle_player(db, payload.initData)
    b, changed = run_battle(battles.make_offer, db, me, payload.battle_id, payload.game, payload.stake)
    idx = battles.player_index(b, me)
    opp_id = b.invitee_id if idx == 0 else b.inviter_id
    what = (f"покер, стек {payload.stake} колизов" if payload.game == "poker"
            else f"{battles.GAMES[payload.game]} на {payload.stake} колизов")
    if changed:
        await notify_bot(opp_id, f"⚔️ {battles.first_name(me)} предлагает: {what}. Принять?", b.id)
    return run_battle(battles.battle_view, db, b, me)


@app.post("/api/battle/offer_respond")
async def api_battle_offer_respond(payload: BattleRespondPayload, db: Session = Depends(get_db)):
    me = battle_player(db, payload.initData)
    b0, _ = run_battle(battles.get_battle, db, payload.battle_id, me)
    proposer = b0.offer_by
    b = run_battle(battles.respond_offer, db, me, payload.battle_id, payload.accept, payload.game, payload.stake)
    if proposer and proposer != me.id:
        await notify_bot(proposer, f"⚔️ {battles.first_name(me)} принял(а) — игра началась!" if payload.accept
                         else f"{battles.first_name(me)} отклонил(а) предложение — предложи другое.", b.id)
    return run_battle(battles.battle_view, db, b, me)


@app.post("/api/battle/action")
def api_battle_action(payload: BattleActionPayload, db: Session = Depends(get_db)):
    me = battle_player(db, payload.initData)

    def _act():
        battles.action(db, me, payload.battle_id, payload.action, payload.amount)
        b, _ = battles.get_battle(db, payload.battle_id, me)
        return battles.battle_view(db, b, me)
    return run_battle(_act)


@app.post("/api/battle/leave")
async def api_battle_leave(payload: BattleIdPayload, db: Session = Depends(get_db)):
    me = battle_player(db, payload.initData)
    b, changed = run_battle(battles.leave, db, me, payload.battle_id)
    idx = battles.player_index(b, me)
    opp_id = b.invitee_id if idx == 0 else b.inviter_id
    if changed and b.invitee_id:
        await notify_bot(opp_id, f"{battles.first_name(me)} вышел(ла) из батла.")
    return {"ok": True}


# ---------- никнейм и рейтинг ----------

class NicknamePayload(BaseModel):
    initData: str
    nickname: str


@app.post("/api/nickname")
def api_nickname(payload: NicknamePayload, db: Session = Depends(get_db)):
    """Гость задаёт никнейм. Если номер уже подтверждён — сразу приветственный колиз."""
    client = consented_client(db, verify(payload.initData))
    try:
        with battles.LOCK:            # тот же замок — чтобы два гостя одновременно не заняли один ник
            rating.set_nickname(db, client, payload.nickname)
    except rating.NicknameError as e:
        raise HTTPException(status_code=400, detail=str(e))
    bonus = rating.try_welcome(db, client)
    d = client_to_dict(db, client)
    d["welcome_awarded"] = bonus
    return d


@app.post("/api/rating")
def api_rating(payload: InitDataPayload, db: Session = Depends(get_db)):
    client = consented_client(db, verify(payload.initData))
    return rating.rating_view(db, client)


# ---------- если у гостя не открылся кабинет ----------

class ClientErrorPayload(BaseModel):
    kind: str = ""
    message: str = ""
    ua: str = ""
    tg_version: str = ""
    platform: str = ""
    has_init: bool = False
    has_hash: bool = False
    extra: str = ""


_client_errors_seen: dict = {}
_client_errors_sent: list = []


@app.post("/api/client_error")
async def api_client_error(p: ClientErrorPayload):
    """Приложение сообщает, что у гостя не загрузился кабинет. Пишем в лог и
    присылаем админам — не чаще раза в час на одинаковую ошибку и не больше 10 в час."""
    msg = p.message[:300]
    logger.warning("Кабинет не загрузился: %s | %s | TG %s %s | init=%s | %s",
                   p.kind, msg, p.tg_version, p.platform, p.has_init, p.ua[:200])
    now = time.time()
    key = (p.kind, msg[:100], p.platform)
    _client_errors_sent[:] = [t for t in _client_errors_sent if now - t < 3600]
    if now - _client_errors_seen.get(key, 0) > 3600 and len(_client_errors_sent) < 10:
        _client_errors_seen[key] = now
        _client_errors_sent.append(now)
        chrome = re.search(r"Chrome/(\d+)", p.ua or "")
        browser = f"встроенный браузер Chrome {chrome.group(1)}" if chrome else (p.ua[:80] or "неизвестно")
        hint = ""
        if chrome and int(chrome.group(1)) < 80:
            hint = "\nПохоже, устарел браузер телефона — гостю нужно обновить «Android System WebView» и Chrome в Google Play."
        elif "не загрузился" in (p.tg_version or ""):
            hint = ("\nУ гостя не открывается сайт telegram.org (оператор или VPN). Кабинет должен работать "
                    "через запасной мост; если не работает — пусть попробует выключить VPN или сменить сеть (Wi-Fi/мобильный).")
        elif not p.has_init and not p.has_hash:
            hint = ("\nTelegram не передал кабинету данные гостя. Пусть гость отправит боту /start и нажмёт "
                    "«Открыть личный кабинет» в НОВОМ сообщении — в эту кнопку вшит его личный пропуск.")
        await notify_admins(f"⚠️ У гостя не открылся кабинет\nЧто случилось: {p.kind} — {msg or '—'}\n"
                            f"Telegram {p.tg_version or '?'} ({p.platform or '?'}), {browser}{hint}\n"
                            f"Тех. данные: {p.extra[:300] or '—'}")
    return {"ok": True}


@app.post("/api/withdraw")
async def api_withdraw(payload: WithdrawPayload, db: Session = Depends(get_db)):
    """Заявка на списание колизов. Колизы уходят только на номер, подтверждённый
    через Telegram; действуют минимум за раз и лимит на один номер за период.
    Сумма сразу снимается с баланса (чтобы нельзя было отправить заявку дважды),
    админ получает сообщение с данными для проверки и начисляет бонусы в CRM."""
    tg_user = verify(payload.initData)
    client = consented_client(db, tg_user)
    require_verified(client)

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Заполни имя")

    # Проверка лимита и запись заявки — под замком, чтобы две одновременные
    # заявки (с двух аккаунтов на один номер) не проскочили лимит вместе.
    async with _withdraw_lock:
        info = withdraw_info(db, client)
        if not info["available"]:
            raise HTTPException(status_code=400, detail=info["reason"])
        amount = info["amount"]

        if not change_balance(db, client, -amount, require_at_least=amount):
            db.rollback()
            raise HTTPException(status_code=409, detail="Баланс изменился — обнови страницу и попробуй ещё раз")

        phone = client.phone
        client.last_withdraw_at = datetime.utcnow()
        username = tg_user.get("username")
        db.add(Withdrawal(
            tg_id=client.tg_id, tg_name=tg_user.get("first_name"), tg_username=username,
            name=name, phone=phone, phone_normalized=client.phone_normalized, amount=amount,
        ))
        db.commit()
        db.refresh(client)

    # Данные, по которым админ быстро поймёт, реальный ли это гость клуба.
    days = (datetime.utcnow() - client.created_at).days if client.created_at else 0
    plays = db.query(func.count(GamePlay.id)).filter(GamePlay.tg_id == client.tg_id).scalar() or 0
    won = db.query(func.coalesce(func.sum(GamePlay.payout - GamePlay.stake), 0)).filter(
        GamePlay.tg_id == client.tg_id).scalar() or 0
    topup, crm_found = lookup_topup(db, client)
    crm_line = (f"есть, в среднем {round(topup):,} ₽/мес → {TIER_LABELS[get_tier(topup)]}".replace(",", " ")
                if crm_found else "НЕТ в выгрузке — гость не пополнял счёт в клубе")

    tg_line = f"@{username}" if username else f"id {client.tg_id}"
    await notify_admins(
        "💸 Заявка на списание колизов\n"
        f"Имя: {name}\n"
        f"Телефон (подтверждён Telegram): {phone}\n"
        f"Сумма: {amount} колизов (одна трата {window_label()} — магазин по этому номеру теперь закрыт до завтра)\n"
        f"Telegram: {tg_line}\n\n"
        "Проверка:\n"
        f"• аккаунту в приложении: {days} дн.\n"
        f"• сыграно игр: {plays}, выиграно всего: {won} колизов\n"
        f"• номер в CRM клуба: {crm_line}\n\n"
        "Начисли бонусы гостю в CRM. Если заявку отклоняешь — "
        f"верни колизы в приложение: /add_bonus {phone} {amount}"
    )

    return {"ok": True, "amount": amount, "balance": client.balance}


@app.post("/api/ideas")
async def api_ideas(payload: IdeaPayload, db: Session = Depends(get_db)):
    tg_user = verify(payload.initData)
    consented_client(db, tg_user)
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Пустая идея")

    idea = Idea(tg_id=tg_user["id"], tg_name=tg_user.get("first_name"), text=payload.text.strip())
    db.add(idea)
    db.commit()

    name = tg_user.get("first_name") or tg_user.get("username") or "Гость"
    await notify_admins(f"💡 Новая идея от {name}:\n\n{idea.text}")
    return {"ok": True}


@app.post("/api/booking")
async def api_booking(payload: BookingPayload, db: Session = Depends(get_db)):
    tg_user = verify(payload.initData)
    consented_client(db, tg_user)
    if not payload.name.strip() or not payload.phone.strip() or not payload.time_text.strip():
        raise HTTPException(status_code=400, detail="Заполни имя, телефон и время")

    booking = Booking(
        tg_id=tg_user["id"],
        tg_name=tg_user.get("first_name"),
        name=payload.name.strip(),
        phone=payload.phone.strip(),
        time_text=payload.time_text.strip(),
        seats_text=payload.seats_text.strip(),
        notes=payload.notes.strip(),
    )
    db.add(booking)
    db.commit()

    lines = [
        "📅 Новая заявка на бронирование",
        f"Имя: {booking.name}",
        f"Телефон: {booking.phone}",
        f"Время: {booking.time_text}",
    ]
    if booking.seats_text:
        lines.append(f"Места: {booking.seats_text}")
    if booking.notes:
        lines.append(f"Пожелания: {booking.notes}")
    await notify_admins("\n".join(lines))
    return {"ok": True}



# ---------- магазин за колизы ----------

class ShopOrderPayload(BaseModel):
    initData: str
    items: list[dict]


class OrderIdPayload(BaseModel):
    initData: str
    order_id: int


@app.get("/api/shop/catalog")
def api_shop_catalog(db: Session = Depends(get_db)):
    return shop.catalog_view(db)


@app.get("/api/shop/img/{product_id}/{variant_id}")
def api_shop_img(product_id: str, variant_id: str, db: Session = Depends(get_db)):
    img = shop.image(db, product_id, variant_id)
    if img is None:
        raise HTTPException(status_code=404, detail="Нет фото")
    return Response(content=img.data, media_type=img.mime or "image/webp",
                    headers={"Cache-Control": "public, max-age=604800"})


def shop_orders_view(db: Session, client: Client) -> list[dict]:
    rows = (db.query(ShopOrder).filter(ShopOrder.client_id == client.id)
            .order_by(ShopOrder.id.desc()).limit(10).all())
    return [shop.order_view(o, client.nickname) for o in rows]


@app.post("/api/shop/order")
async def api_shop_order(payload: ShopOrderPayload, db: Session = Depends(get_db)):
    """Покупка колизами. Общий лимит трат с заявками на списание — под тем же замком."""
    client = consented_client(db, verify(payload.initData))
    require_verified(client)
    try:
        items, total = shop.build_order(db, payload.items)
    except shop.ShopError as e:
        raise HTTPException(status_code=400, detail=str(e))
    async with _withdraw_lock:
        info = withdraw_info(db, client)
        if info["spent_kind"]:
            raise HTTPException(status_code=400, detail=info["reason"])
        if total > WITHDRAW_LIMIT:
            raise HTTPException(status_code=400, detail=(
                f"За один раз — не больше {WITHDRAW_LIMIT} колизов, а в корзине {total}. Убери что-нибудь из корзины."))
        db.refresh(client)
        deadline = bonus_expiry.deadline_for(client)
        if not change_balance(db, client, -total, require_at_least=total):
            db.rollback()
            raise HTTPException(status_code=400, detail="Недостаточно колизов на балансе")
        now = datetime.utcnow()
        order = ShopOrder(client_id=client.id, tg_id=client.tg_id, phone_normalized=client.phone_normalized,
                          items=json.dumps(items, ensure_ascii=False), total=total, status="new",
                          created_at=now, expires_at=now + shop.ORDER_TTL, bonus_deadline=deadline)
        db.add(order)
        db.commit()
        db.refresh(order)
        db.refresh(client)
    if _bot_instance is not None:
        try:
            await bot_extra.shop_notify(_bot_instance, order, client, items)
        except Exception as e:  # noqa: BLE001
            logger.warning("Магазин: не удалось уведомить админов: %s", e)
    return {"order": shop.order_view(order, client.nickname), "balance": client.balance,
            "withdraw": withdraw_info(db, client)}


@app.post("/api/shop/orders")
def api_shop_orders(payload: InitDataPayload, db: Session = Depends(get_db)):
    client = consented_client(db, verify(payload.initData))
    return {"orders": shop_orders_view(db, client)}


@app.post("/api/shop/order_state")
def api_shop_order_state(payload: OrderIdPayload, db: Session = Depends(get_db)):
    client = consented_client(db, verify(payload.initData))
    o = db.get(ShopOrder, payload.order_id)
    if o is None or o.client_id != client.id:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return shop.order_view(o, client.nickname)


@app.post("/api/shop/cancel")
async def api_shop_cancel(payload: OrderIdPayload, db: Session = Depends(get_db)):
    client = consented_client(db, verify(payload.initData))
    r = shop.cancel_order(db, payload.order_id, "отменил гость", client_id=client.id)
    if r is None:
        raise HTTPException(status_code=400, detail="Этот заказ уже выдан или отменён")
    o, refunded = r
    await notify_admins(f"↩️ Гость отменил заказ №{o.id} — не выдавай его.")
    db.refresh(client)
    return {"order": shop.order_view(o, client.nickname), "balance": client.balance, "refunded": refunded,
            "withdraw": withdraw_info(db, client)}


# ---------- отметка «Я в клубе» с чеком ----------

class CheckInPayload(BaseModel):
    initData: str
    photo: str      # data:image/jpeg;base64,…


@app.post("/api/checkin")
async def api_checkin(payload: CheckInPayload, db: Session = Depends(get_db)):
    client = consented_client(db, verify(payload.initData))
    try:
        checkin.can_submit(db, client)
    except checkin.CheckInError as e:
        raise HTTPException(status_code=400, detail=str(e))
    raw = payload.photo.split(",", 1)[-1]
    try:
        photo = base64.b64decode(raw, validate=False)
    except (ValueError, binascii.Error):
        raise HTTPException(status_code=400, detail="Не получилось прочитать фото — попробуй другое")
    if len(photo) < 2000 or len(photo) > 6_000_000:
        raise HTTPException(status_code=400, detail="Фото не подошло — сфотографируй чек ещё раз")
    if _bot_instance is None:
        raise HTTPException(status_code=503, detail="Сейчас не получается отправить чек — пришли фото чека боту в чат")
    c = checkin.create(db, client)
    try:
        file_id = await bot_extra.checkin_notify(_bot_instance, c, client, photo=photo)
    except Exception as e:  # noqa: BLE001
        logger.warning("Чек не отправлен админам: %s", e)
        file_id = None
    if not file_id:
        db.delete(c)
        db.commit()
        raise HTTPException(status_code=503, detail="Не получилось отправить чек администратору — пришли фото чека боту в чат")
    c.photo_file_id = file_id
    db.commit()
    return checkin.view(db, client)


@app.post("/api/rating/friendly")
def api_rating_friendly(payload: InitDataPayload, db: Session = Depends(get_db)):
    client = consented_client(db, verify(payload.initData))
    return rating.friendly_view(db, client)


# ---------- профиль игрока ----------

class ProfilePayload(BaseModel):
    initData: str
    about: str = ""
    games: list[str] = []
    rank: str = ""


class NickPayload(BaseModel):
    initData: str
    nickname: str


@app.post("/api/profile/save")
def api_profile_save(payload: ProfilePayload, db: Session = Depends(get_db)):
    client = consented_client(db, verify(payload.initData))
    try:
        social.save_profile(db, client, payload.about, payload.games, payload.rank)
    except social.SocialError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return social.profile_view(db, client, me=True)


@app.post("/api/profile/view")
def api_profile_view(payload: NickPayload, db: Session = Depends(get_db)):
    consented_client(db, verify(payload.initData))
    c = social.find_by_nickname(db, payload.nickname)
    if c is None:
        raise HTTPException(status_code=404, detail="Игрок не найден")
    return social.profile_view(db, c)


# ---------- тиммейты ----------

class TeamCreatePayload(BaseModel):
    initData: str
    game: str
    day: int = 0          # 0 — сегодня, 1 — завтра…
    hour: int
    minute: int = 0
    need: int = 1
    comment: str = ""


class TeamIdPayload(BaseModel):
    initData: str
    post_id: int


@app.post("/api/team/list")
def api_team_list(payload: InitDataPayload, db: Session = Depends(get_db)):
    client = consented_client(db, verify(payload.initData))
    return {"posts": social.team_list(db, client), "games": social.GAMES}


@app.post("/api/team/create")
async def api_team_create(payload: TeamCreatePayload, db: Session = Depends(get_db)):
    client = consented_client(db, verify(payload.initData))
    require_verified(client)
    if not client.nickname:
        raise HTTPException(status_code=400, detail="Сначала придумай никнейм на главной")
    if not (0 <= payload.day <= 13 and 0 <= payload.hour <= 23 and 0 <= payload.minute <= 59):
        raise HTTPException(status_code=400, detail="Неверное время")
    msk_today = (datetime.utcnow() + timedelta(hours=3)).date()
    d = msk_today + timedelta(days=payload.day)
    play_at = datetime(d.year, d.month, d.day, payload.hour, payload.minute) - timedelta(hours=3)
    try:
        post = social.team_create(db, client, payload.game, play_at, payload.need, payload.comment)
    except social.SocialError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if _bot_instance is not None:
        await bot_social.announce_team(_bot_instance, post, client)
    return {"posts": social.team_list(db, client)}


@app.post("/api/team/join")
async def api_team_join(payload: TeamIdPayload, db: Session = Depends(get_db)):
    client = consented_client(db, verify(payload.initData))
    require_verified(client)
    if not client.nickname:
        raise HTTPException(status_code=400, detail="Сначала придумай никнейм на главной")
    try:
        post = social.team_join(db, client, payload.post_id)
    except social.SocialError as e:
        raise HTTPException(status_code=400, detail=str(e))
    author = db.get(Client, post.client_id)
    if _bot_instance is not None and author is not None:
        await bot_social.team_joined(_bot_instance, post, author, client)
    return {"posts": social.team_list(db, client)}


@app.post("/api/team/close")
def api_team_close(payload: TeamIdPayload, db: Session = Depends(get_db)):
    client = consented_client(db, verify(payload.initData))
    db.query(TeamPost).filter(TeamPost.id == payload.post_id, TeamPost.client_id == client.id).update(
        {TeamPost.status: "closed"}, synchronize_session=False)
    db.commit()
    return {"posts": social.team_list(db, client)}


# ---------- афиша ----------

class EventGoPayload(BaseModel):
    initData: str
    event_id: int
    going: bool = True


@app.post("/api/events")
def api_events(payload: InitDataPayload, db: Session = Depends(get_db)):
    client = consented_client(db, verify(payload.initData))
    return {"events": social.events_list(db, client)}


@app.post("/api/event/going")
def api_event_going(payload: EventGoPayload, db: Session = Depends(get_db)):
    client = consented_client(db, verify(payload.initData))
    ev = db.get(ClubEvent, payload.event_id)
    if ev is None or ev.status != "active":
        raise HTTPException(status_code=404, detail="Событие не найдено")
    row = db.get(EventGoing, (ev.id, client.id))
    if payload.going and row is None:
        db.add(EventGoing(event_id=ev.id, client_id=client.id))
    elif not payload.going and row is not None:
        db.delete(row)
    db.commit()
    return {"events": social.events_list(db, client)}


@app.get("/api/event/img/{event_id}")
def api_event_img(event_id: int, db: Session = Depends(get_db)):
    ev = db.get(ClubEvent, event_id)
    if ev is None or not ev.image:
        raise HTTPException(status_code=404, detail="Нет фото")
    return Response(content=ev.image, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


# ---------- админ ----------

@app.post("/api/admin/topup")
def api_admin_topup(payload: AdminTopupPayload, db: Session = Depends(get_db)):
    if not ADMIN_SECRET or payload.secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Неверный секрет")

    norm = normalize_phone(payload.phone)
    stat = db.get(PhoneStat, norm)
    if stat is None:
        db.add(PhoneStat(phone_normalized=norm, avg_monthly=payload.monthly_topup, updated_at=datetime.utcnow()))
    else:
        stat.avg_monthly = payload.monthly_topup
        stat.updated_at = datetime.utcnow()
    db.commit()
    resync_client_topups(db)
    client = db.query(Client).filter(Client.phone_normalized == norm).first()
    return client_to_dict(db, client) if client else {"ok": True}


# ---------- запуск бота вместе с веб-сервером ----------

_bot_task: asyncio.Task | None = None
_bot_instance = None
_withdraw_lock = asyncio.Lock()


@app.on_event("startup")
async def start_bot():
    global _bot_task, _bot_instance
    if not os.getenv("BOT_TOKEN"):
        logger.warning("BOT_TOKEN не задан — бот не запущен, работает только веб-API")
        return

    bot, dp = build_bot_and_dispatcher()
    _bot_instance = bot

    # Имя бота — для реферальных ссылок; меню команд для гостей.
    try:
        me = await bot.get_me()
        if not referrals.BOT_USERNAME:
            referrals.BOT_USERNAME = me.username or ""
        await bot.set_my_commands(GUEST_COMMANDS)
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось получить имя бота / меню команд: %s", e)

    # Если статистика CRM пустая — статусы ни у кого не появятся. Сразу говорим админам.
    db = SessionLocal()
    try:
        stats_count = db.query(PhoneStat).count()
    finally:
        db.close()
    if stats_count == 0:
        await notify_admins("⚠️ Статистика пополнений из CRM пустая — уровни (Silver/Gold/Premium) "
                            "ни у кого не отображаются. Пришли боту лог финансовых операций "
                            "с подписью /import_topups")

    async def runner():
        try:
            await dp.start_polling(bot)
        except asyncio.CancelledError:
            pass

    _bot_task = asyncio.create_task(runner())
    logger.info("Бот запущен (long polling)")


_sweep_task: asyncio.Task | None = None


async def battle_sweeper():
    """Раз в 10 секунд досчитывает партии батлов, где игроки пропали:
    таймауты ходов, конец Crash-раунда, выплата замороженных ставок."""
    while True:
        await asyncio.sleep(10)
        db = SessionLocal()
        try:
            with battles.LOCK:
                battles.sweep(db)
        except Exception as e:  # noqa: BLE001
            logger.warning("Проверка батлов: %s", e)
        finally:
            db.close()
        await settle_rating()
        try:
            await expiry_tick()
            await group_tick()
        except Exception as e:  # noqa: BLE001
            logger.warning("Фоновые задачи: %s", e)


_rating_checked_month = None


async def settle_rating():
    """В начале нового месяца — призы победителям рейтинга прошлого месяца (один раз)."""
    global _rating_checked_month
    current = rating.month_key(datetime.utcnow())
    if _rating_checked_month == current:
        return
    db = SessionLocal()
    try:
        awarded = rating.settle_due(db)
        friendly = rating.settle_friendly_due(db)
        _rating_checked_month = current
    except Exception as e:  # noqa: BLE001
        logger.warning("Итоги рейтинга: %s", e)
        return
    finally:
        db.close()
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}
    # Призы — на карту гостя в CRM клуба (в приложении бонусы сгорают за сутки,
    # а на карте нет — так приз сохраняет свою ценность). Админу — список к начислению.
    for a in awarded + friendly:
        what = (f"🤝 Ты — самый дружелюбный гость COLIZEUM за {a['month'].lower()}! Привёл(а) друзей: {a['friends']}."
                if "friends" in a else
                f"🏆 Итоги рейтинга COLIZEUM за {a['month'].lower()}: {a['place']} место! Твой оборот — {a['volume']:,} колизов.")
        if _bot_instance is not None:
            try:
                await _bot_instance.send_message(
                    a["tg_id"], (what + f"\nПриз {a['amount']:,} бонусов администратор зачислит на твою карту гостя клуба — "
                                        "там бонусы не сгорают.").replace(",", " "))
            except Exception as e:  # noqa: BLE001
                logger.info("Не удалось поздравить %s: %s", a["tg_id"], e)
    if awarded or friendly:
        lines = ["🏆 Итоги месяца — начисли призы в CRM на карту гостя:"]
        lines += [f"{a['place']}. {a['nickname']} ({a.get('phone') or 'номер не подтверждён'}) — оборот {a['volume']:,} → {a['amount']:,} бонусов на карту"
                  .replace(",", " ") for a in awarded]
        lines += [f"🤝 {a['nickname']} ({a.get('phone') or '—'}) — привёл(а) {a['friends']} друзей → {a['amount']:,} бонусов на карту"
                  .replace(",", " ") for a in friendly]
        await notify_admins("\n".join(lines))
        glines = [f"🏆 Итоги месяца в COLIZEUM — {(awarded or friendly)[0]['month'].lower()}", ""]
        glines += [f"{medal.get(a['place'], a['place'])} {a['nickname']} — оборот {a['volume']:,}, приз {a['amount']:,}"
                   .replace(",", " ") for a in awarded]
        glines += [f"🤝 Самый дружелюбный: {a['nickname']} — привёл(а) {a['friends']} друзей, приз {a['amount']:,}"
                   .replace(",", " ") for a in friendly if a["place"] == 1]
        glines += ["", "Новый месяц — новый рейтинг. Залетай 👇"]
        group.post("\n".join(glines))


# ---------- сгорание колизов и просроченные заказы ----------

def _close_predictions():
    """Прогнозы отключены: открытые ставки (если были) возвращаем один раз при запуске."""
    db = SessionLocal()
    try:
        for p in db.query(Prediction).filter(Prediction.status == "open").all():
            p.status, p.payout = "refunded", p.stake
            db.query(Client).filter(Client.id == p.client_id).update(
                {Client.balance: func.coalesce(Client.balance, 0) + p.stake}, synchronize_session=False)
        db.query(EsportsMatch).filter(EsportsMatch.status == "open").update(
            {EsportsMatch.status: "cancelled", EsportsMatch.settled_at: datetime.utcnow()}, synchronize_session=False)
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("Закрытие прогнозов: %s", e)
        db.rollback()
    finally:
        db.close()


_close_predictions()


async def expiry_tick():
    db = SessionLocal()
    try:
        warn, burned = bonus_expiry.tick(db)
        expired = shop.expire_due(db)
        expired_info = [(o.id, o.tg_id, o.total, refunded) for o, refunded in expired]
    finally:
        db.close()
    if _bot_instance is None:
        return
    db = SessionLocal()
    try:
        reminders = social.event_reminders(db)
    finally:
        db.close()
    for tg_id, title, when in reminders:
        await bot_extra.tell(_bot_instance, tg_id, f"Напоминаем: «{title}» в COLIZEUM — {when}. Ждём тебя!", cabinet=True)
    for w in warn:
        left = w["expires_at"] - datetime.utcnow()
        await bot_extra.tell(_bot_instance, w["tg_id"],
                             f"⏳ Через {format_wait(left)} сгорят {w['amount']:,} колизов на балансе COLIZEUM.\n"
                             f"Колизы действуют сутки с момента начисления. Успей потратить: купить напиток или батончик "
                             f"в магазине приложения или списать до {WITHDRAW_LIMIT} колизов на карту гостя.".replace(",", " "),
                             cabinet=True)
    for oid, tg_id, total, refunded in expired_info:
        await bot_extra.tell(_bot_instance, tg_id, f"Заказ №{oid} не забрали вовремя — он отменён. " + (
            f"{total} колизов вернулись на баланс." if refunded else "Срок действия этих колизов уже истёк — они сгорели."))
        await notify_admins(f"⌛ Заказ №{oid} просрочен и отменён — не выдавай его.")


_weekly_key = None


async def group_tick():
    """Публикует накопившиеся посты в группу клуба и еженедельный рейтинг."""
    global _weekly_key
    if not group.enabled() or _bot_instance is None:
        group.drain()
        return
    key = group.weekly_due(last_key=_weekly_key)
    if key:
        _weekly_key = key
        db = SessionLocal()
        try:
            mk = rating.month_key(datetime.utcnow())
            text = group.weekly_text(rating.month_title(mk), rating.leaderboard(db, mk), rating.friendly_board(db, mk))
        finally:
            db.close()
        if text:
            group.post(text)
    for item in group.drain():
        await bot_extra.post_to_group(_bot_instance, item["text"], item.get("button", True))
        await asyncio.sleep(1)


@app.on_event("startup")
async def start_sweeper():
    global _sweep_task
    _sweep_task = asyncio.create_task(battle_sweeper())


@app.on_event("shutdown")
async def stop_bot():
    if _bot_task:
        _bot_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _bot_task


@app.get("/health")
def health():
    return {"status": "ok"}

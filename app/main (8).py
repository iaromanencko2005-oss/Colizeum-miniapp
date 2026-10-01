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
import contextlib
import logging
import os
import random
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .database import Base, engine, get_db, SessionLocal, add_missing_columns
from .models import (
    Client, Promotion, Idea, Booking, GamePlay, Withdrawal, PhoneStat,
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
from .telegram_auth import parse_and_verify, InvalidInitData
from .legal import POLICY_VERSION, consent_html, policy_html
from .bot import build_bot_and_dispatcher, GUEST_COMMANDS

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

# «Кости»: сколько бонусов за каждое выпавшее очко (выпало 4 → 4 × 10 = 40).
DICE_BONUS_PER_POINT = int(os.getenv("DICE_BONUS_PER_POINT", "10"))

# Бесплатная попытка «Дайс»: реальный шанс = выбранный × этот коэффициент.
PROBABILITY_HOUSE_FACTOR = float(os.getenv("PROBABILITY_HOUSE_FACTOR", "0.3"))

# Ставки бонусами в «Дайс»: комиссия клуба в процентах, заложенная в
# коэффициент выплаты, и потолок выигрыша за одну ставку.
BET_HOUSE_EDGE_PERCENT = int(os.getenv("BET_HOUSE_EDGE_PERCENT", "5"))
BET_MAX_PAYOUT = int(os.getenv("BET_MAX_PAYOUT", "3000"))
BET_MIN_CHANCE = 1
BET_MAX_CHANCE = 90

# Правила списания бонусов (Railway → Variables):
#   WITHDRAW_MIN          — меньше этого списать нельзя (чтобы не дёргали по 50 бонусов);
#   WITHDRAW_LIMIT        — сколько всего можно списать на ОДИН НОМЕР за период —
#                           считается по номеру, поэтому второй аккаунт с тем же
#                           номером лимит не обходит;
#   WITHDRAW_WINDOW_HOURS — длина периода в часах: 24 = сутки, 168 = неделя.
WITHDRAW_MIN = int(os.getenv("WITHDRAW_MIN", "100"))
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
    stake: int      # сколько бонусов ставит гость


class ConsentPayload(BaseModel):
    initData: str
    ref: str = ""   # реферальная метка из ссылки друга (ref_<id>)


class WithdrawPayload(BaseModel):
    initData: str
    name: str
    phone: str = ""  # не используется: бонусы уходят только на подтверждённый номер


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
    Так один человек не может завести несколько аккаунтов и слить бонусы на один номер."""
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
    """Заявки на списание за текущий период — по номеру телефона (с любого
    аккаунта) и по самому аккаунту. Старые заявки без нормализованного номера
    ловим по номеру как он записан."""
    conds = [Withdrawal.tg_id == client.tg_id]
    if client.phone_normalized:
        conds.append(Withdrawal.phone_normalized == client.phone_normalized)
    if client.phone:
        conds.append(Withdrawal.phone == client.phone)
    since = now - timedelta(hours=WITHDRAW_WINDOW_HOURS)
    return (db.query(Withdrawal.created_at, Withdrawal.amount)
            .filter(or_(*conds), Withdrawal.created_at >= since)
            .order_by(Withdrawal.created_at.asc()).all())


def withdraw_info(db: Session, client: Client) -> dict:
    now = datetime.utcnow()
    balance = client.balance or 0
    rows = recent_withdrawals(db, client, now) if client.phone_verified else []
    used = sum(a for _, a in rows)
    remaining = max(0, WITHDRAW_LIMIT - used)

    next_in = None
    if remaining < WITHDRAW_MIN and rows:
        # Когда старые заявки «выйдут» из периода и лимит освободится до минимума.
        left = used
        for created_at, amount in rows:
            left -= amount
            if WITHDRAW_LIMIT - left >= WITHDRAW_MIN:
                next_in = format_wait(created_at + timedelta(hours=WITHDRAW_WINDOW_HOURS) - now)
                break

    reason = None
    if not client.phone_verified:
        reason = "Подтверди номер телефона, чтобы списывать бонусы"
    elif remaining < WITHDRAW_MIN:
        reason = (f"Лимит {WITHDRAW_LIMIT} бонусов {window_label()} на номер исчерпан"
                  + (f" — следующее списание через {next_in}" if next_in else ""))
    elif balance < WITHDRAW_MIN:
        reason = f"Списать можно от {WITHDRAW_MIN} бонусов"

    return {
        "available": reason is None,
        "reason": reason,
        "amount": min(balance, remaining),
        "min": WITHDRAW_MIN,
        "limit": WITHDRAW_LIMIT,
        "used": used,
        "remaining": remaining,
        "window_label": window_label(),
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
        "phone": client.phone if client.phone_verified else None,
        "phone_verified": bool(client.phone_verified),
        "balance": client.balance or 0,
        "monthly_topup": topup,
        "crm_found": crm_found,
        "withdraw": withdraw_info(db, client),
        "referral": {
            "link": referrals.referral_link(client.id),
            "bonus": referrals.REFERRAL_BONUS,
            "invited": referrals.invited_count(db, client),
        },
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
    (бонус пригласившему начислится, когда гость подтвердит номер)."""
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
    """«Дайс», ставка бонусами. Без лимита попыток — ограничен только балансом."""
    tg_user = verify(payload.initData)
    client = consented_client(db, tg_user)
    require_verified(client)

    chance = payload.threshold
    stake = payload.stake
    if not (BET_MIN_CHANCE <= chance <= BET_MAX_CHANCE):
        raise HTTPException(status_code=400, detail=f"Шанс — от {BET_MIN_CHANCE} до {BET_MAX_CHANCE}%")
    if stake < 1:
        raise HTTPException(status_code=400, detail="Ставка — минимум 1 бонус")

    payout_if_win = bet_payout(stake, chance, BET_HOUSE_EDGE_PERCENT)
    if payout_if_win > BET_MAX_PAYOUT:
        raise HTTPException(
            status_code=400,
            detail=f"Максимальный выигрыш за одну ставку — {BET_MAX_PAYOUT} бонусов. "
                   f"Уменьши ставку или повысь шанс.",
        )

    # Сначала списываем ставку (атомарно — если бонусов не хватает, ничего не спишется).
    if not change_balance(db, client, -stake, require_at_least=stake):
        db.rollback()
        raise HTTPException(status_code=400, detail="Недостаточно бонусов на балансе")

    rolled, win = roll_bet(chance)
    payout = payout_if_win if win else 0
    if payout:
        change_balance(db, client, payout)

    db.add(GamePlay(tg_id=client.tg_id, game="probability", mode="bet", stake=stake, payout=payout))
    db.commit()
    db.refresh(client)

    return {
        "win": win,
        "rolled_number": rolled,
        "stake": stake,
        "payout": payout,
        "net": payout - stake,
        "balance": client.balance,
    }


@app.post("/api/withdraw")
async def api_withdraw(payload: WithdrawPayload, db: Session = Depends(get_db)):
    """Заявка на списание бонусов. Бонусы уходят только на номер, подтверждённый
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
        "💸 Заявка на списание бонусов\n"
        f"Имя: {name}\n"
        f"Телефон (подтверждён Telegram): {phone}\n"
        f"Сумма: {amount} бонусов (по номеру {window_label()}: {info['used'] + amount} из {WITHDRAW_LIMIT})\n"
        f"Telegram: {tg_line}\n\n"
        "Проверка:\n"
        f"• аккаунту в приложении: {days} дн.\n"
        f"• сыграно игр: {plays}, выиграно всего: {won} бонусов\n"
        f"• номер в CRM клуба: {crm_line}\n\n"
        "Начисли бонусы гостю в CRM. Если заявку отклоняешь — "
        f"верни бонусы в приложение: /add_bonus {phone} {amount}"
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


@app.on_event("shutdown")
async def stop_bot():
    if _bot_task:
        _bot_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _bot_task


@app.get("/health")
def health():
    return {"status": "ok"}

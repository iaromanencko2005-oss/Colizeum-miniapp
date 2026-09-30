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
from datetime import datetime

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .database import Base, engine, get_db, SessionLocal, add_missing_columns
from .models import (
    Client, Promotion, Idea, Booking, GamePlay, Withdrawal,
    get_tier, get_next_tier_info, normalize_phone,
    TIER_LABELS, TIER_THRESHOLDS, TIER_CASHBACK,
)
from .prizes import spin as spin_roulette
from .games import (
    FREE_WINDOW, free_attempts_state, format_wait,
    free_probability_reward, roll_free_probability,
    bet_payout, roll_bet,
)
from .telegram_auth import parse_and_verify, InvalidInitData
from .bot import build_bot_and_dispatcher

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
FREE_ATTEMPTS_PER_DAY = int(os.getenv("FREE_ATTEMPTS_PER_DAY", "3"))

# Бесплатная попытка «Дайс»: реальный шанс = выбранный × этот коэффициент.
PROBABILITY_HOUSE_FACTOR = float(os.getenv("PROBABILITY_HOUSE_FACTOR", "0.3"))

# Ставки бонусами в «Дайс»: комиссия клуба в процентах, заложенная в
# коэффициент выплаты, и потолок выигрыша за одну ставку.
BET_HOUSE_EDGE_PERCENT = int(os.getenv("BET_HOUSE_EDGE_PERCENT", "5"))
BET_MAX_PAYOUT = int(os.getenv("BET_MAX_PAYOUT", "3000"))
BET_MIN_CHANCE = 1
BET_MAX_CHANCE = 90

GAMES = ("spin", "dice", "probability")

Base.metadata.create_all(bind=engine)
add_missing_columns()


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


class WithdrawPayload(BaseModel):
    initData: str
    name: str
    phone: str


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


def client_to_dict(db: Session, client: Client) -> dict:
    tier = get_tier(client.monthly_topup or 0)
    next_tier, remaining = get_next_tier_info(client.monthly_topup or 0)

    games = {}
    for game in GAMES:
        left, next_at, now = free_state(db, client.tg_id, game)
        games[game] = {
            "left": left,
            "total": FREE_ATTEMPTS_PER_DAY,
            "next_free_in": format_wait(next_at - now) if next_at else None,
        }

    return {
        "name": client.tg_name,
        "phone": client.phone,
        "balance": client.balance or 0,
        "monthly_topup": client.monthly_topup or 0,
        "tier": tier,
        "tier_label": TIER_LABELS[tier],
        "cashback_percent": TIER_CASHBACK[tier],
        "next_tier": TIER_LABELS.get(next_tier) if next_tier else None,
        "remaining_to_next_tier": remaining,
        "games": games,
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
    ]


@app.post("/api/me")
def api_me(payload: InitDataPayload, db: Session = Depends(get_db)):
    tg_user = verify(payload.initData)
    client = get_or_create_client(db, tg_user)
    return client_to_dict(db, client)


@app.post("/api/link-phone")
def api_link_phone(payload: LinkPhonePayload, db: Session = Depends(get_db)):
    tg_user = verify(payload.initData)
    client = get_or_create_client(db, tg_user)
    client.phone = payload.phone
    client.phone_normalized = normalize_phone(payload.phone)
    db.commit()
    return client_to_dict(db, client)


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
    client = get_or_create_client(db, tg_user)
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
    client = get_or_create_client(db, tg_user)
    require_free_attempt(db, client.tg_id, "dice")

    value = random.randint(1, 6)
    bonus = value * 10
    change_balance(db, client, bonus)
    db.add(GamePlay(tg_id=client.tg_id, game="dice", mode="free", payout=bonus))
    db.commit()
    db.refresh(client)

    return {"value": value, "bonus": bonus, "balance": client.balance}


@app.post("/api/probability")
def api_probability(payload: ProbabilityPayload, db: Session = Depends(get_db)):
    """«Дайс», бесплатная попытка."""
    tg_user = verify(payload.initData)
    client = get_or_create_client(db, tg_user)
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
    client = get_or_create_client(db, tg_user)

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
    """Заявка на списание всех бонусов с баланса приложения. Баланс сразу
    обнуляется (чтобы нельзя было отправить заявку дважды), админ получает
    сообщение и начисляет бонусы в CRM клуба."""
    tg_user = verify(payload.initData)
    client = get_or_create_client(db, tg_user)

    name = payload.name.strip()
    phone = payload.phone.strip()
    if not name or not phone:
        raise HTTPException(status_code=400, detail="Заполни имя и телефон")

    amount = client.balance or 0
    if amount <= 0:
        raise HTTPException(status_code=400, detail="На балансе пока нет бонусов")

    if not change_balance(db, client, -amount, require_at_least=amount):
        db.rollback()
        raise HTTPException(status_code=409, detail="Баланс изменился — обнови страницу и попробуй ещё раз")

    username = tg_user.get("username")
    db.add(Withdrawal(
        tg_id=client.tg_id, tg_name=tg_user.get("first_name"), tg_username=username,
        name=name, phone=phone, amount=amount,
    ))
    db.commit()
    db.refresh(client)

    tg_line = f"@{username}" if username else f"id {client.tg_id}"
    await notify_admins(
        "💸 Заявка на списание бонусов\n"
        f"Имя: {name}\n"
        f"Телефон: {phone}\n"
        f"Сумма: {amount} бонусов\n"
        f"Telegram: {tg_line}\n\n"
        "Начисли эти бонусы гостю в CRM. Если заявку нужно отклонить — "
        f"верни бонусы в приложение: /add_bonus {phone} {amount}"
    )

    return {"ok": True, "amount": amount, "balance": client.balance}


@app.post("/api/ideas")
async def api_ideas(payload: IdeaPayload, db: Session = Depends(get_db)):
    tg_user = verify(payload.initData)
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

    client = db.query(Client).filter(Client.phone_normalized == normalize_phone(payload.phone)).first()
    if not client:
        raise HTTPException(status_code=404, detail="Клиент с таким телефоном ещё не открывал мини-приложение")

    client.monthly_topup = payload.monthly_topup
    db.commit()
    return client_to_dict(db, client)


# ---------- запуск бота вместе с веб-сервером ----------

_bot_task: asyncio.Task | None = None
_bot_instance = None


@app.on_event("startup")
async def start_bot():
    global _bot_task, _bot_instance
    if not os.getenv("BOT_TOKEN"):
        logger.warning("BOT_TOKEN не задан — бот не запущен, работает только веб-API")
        return

    bot, dp = build_bot_and_dispatcher()
    _bot_instance = bot

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

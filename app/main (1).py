"""
Точка входа: веб-API мини-приложения + Telegram-бот в одном процессе.

Один сервис на Railway/Render запускает:
- FastAPI-приложение, которое отдаёт данные для мини-приложения
  (уровень, прогресс, акции, рулетка, кости, «Дайс», бронирование);
- Telegram-бота (long polling) фоновой задачей при старте.

Все результаты игр (рулетка, кости, «Дайс») считаются здесь, на
сервере — мини-приложение только показывает то, что вернул API. Это
принципиально: если бы вероятности считались в браузере, любой гость
мог бы подделать результат через консоль разработчика.

Так проще для одного человека без опыта DevOps — не нужно поднимать
два отдельных сервиса и синхронизировать их между собой.
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
from sqlalchemy.orm import Session

from .database import Base, engine, get_db, SessionLocal
from .models import (
    Client, Promotion, Idea, SpinResult, Booking,
    get_tier, get_next_tier_info, TIER_LABELS, TIER_THRESHOLDS, TIER_CASHBACK,
)
from .prizes import spin as spin_roulette
from .telegram_auth import parse_and_verify, InvalidInitData
from .bot import build_bot_and_dispatcher

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("colizeum")

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
SPIN_COOLDOWN_HOURS = int(os.getenv("SPIN_COOLDOWN_HOURS", "24"))
DICE_COOLDOWN_HOURS = int(os.getenv("DICE_COOLDOWN_HOURS", "24"))
PROBABILITY_COOLDOWN_HOURS = int(os.getenv("PROBABILITY_COOLDOWN_HOURS", "24"))

# Насколько занижаем реальный шанс выигрыша в «Дайс» относительно того,
# что гость выставил ползунком (0.3 = реальный шанс втрое ниже заявленного).
PROBABILITY_HOUSE_FACTOR = float(os.getenv("PROBABILITY_HOUSE_FACTOR", "0.3"))

Base.metadata.create_all(bind=engine)


def _seed_default_promotions():
    """Заполняет постоянные акции клуба, если таблица пустая. Нужно, потому
    что база (SQLite без отдельного диска) пересоздаётся при каждом деплое —
    без этого шага акции пропадали бы после каждого обновления, а вручную
    запускать app/seed.py на Railway неоткуда (нет консоли для не-технического
    пользователя). Безопасно вызывать повторно — ничего не дублирует."""
    db = SessionLocal()
    try:
        if db.query(Promotion).count() > 0:
            return
        db.add_all([
            Promotion(
                title="Счастливые часы",
                description="-20% на все тарифы каждый будний день до 14:00",
            ),
            Promotion(
                title="День рождения",
                description=(
                    "Бесплатный час игры в день рождения — покажи админу паспорт. "
                    "Бесплатный час получают все приглашённые"
                ),
            ),
            Promotion(
                title="Приведи друга",
                description="По 200 бонусов тебе и другу за первое посещение по твоей рекомендации",
            ),
            Promotion(
                title="Ночной пакет",
                description="-15% на короткую ночь — с 22:00 до 6:00",
            ),
            Promotion(
                title="Отзыв на картах",
                description="Оставь отзыв о клубе на Яндекс.Картах и получи 100 бонусных рублей",
                link="https://yandex.ru/maps/org/colizeum/227193289093/reviews/?ll=37.568227%2C55.738865&z=16.56",
            ),
        ])
        db.commit()
        logger.info("Добавлены постоянные акции клуба (база была пустой)")
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


class SpinPayload(BaseModel):
    initData: str


class DicePayload(BaseModel):
    initData: str


class ProbabilityPayload(BaseModel):
    initData: str
    threshold: int  # 1..90, шанс выигрыша, который выставил гость ползунком


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

def get_or_create_client(db: Session, tg_user: dict) -> Client:
    tg_id = tg_user["id"]
    client = db.query(Client).filter(Client.tg_id == tg_id).first()
    if not client:
        name = tg_user.get("first_name") or tg_user.get("username") or ""
        client = Client(tg_id=tg_id, tg_name=name, monthly_topup=0.0)
        db.add(client)
        db.commit()
        db.refresh(client)
    return client


def cooldown_ok(last_at: datetime | None, hours: int) -> bool:
    if not last_at:
        return True
    return datetime.utcnow() - last_at >= timedelta(hours=hours)


def client_to_dict(client: Client) -> dict:
    tier = get_tier(client.monthly_topup)
    next_tier, remaining = get_next_tier_info(client.monthly_topup)
    return {
        "name": client.tg_name,
        "phone": client.phone,
        "monthly_topup": client.monthly_topup,
        "tier": tier,
        "tier_label": TIER_LABELS[tier],
        "cashback_percent": TIER_CASHBACK[tier],
        "next_tier": TIER_LABELS.get(next_tier) if next_tier else None,
        "remaining_to_next_tier": remaining,
        "can_spin": cooldown_ok(client.last_spin_at, SPIN_COOLDOWN_HOURS),
        "can_dice": cooldown_ok(client.last_dice_at, DICE_COOLDOWN_HOURS),
        "can_probability": cooldown_ok(client.last_probability_at, PROBABILITY_COOLDOWN_HOURS),
        "spin_cooldown_hours": SPIN_COOLDOWN_HOURS,
        "dice_cooldown_hours": DICE_COOLDOWN_HOURS,
        "probability_cooldown_hours": PROBABILITY_COOLDOWN_HOURS,
    }


async def notify_admins(text: str):
    """Шлёт сообщение всем ADMIN_IDS от имени бота — используется для
    мгновенных уведомлений об идеях и заявках на бронирование. Если бот
    ещё не запущен (нет BOT_TOKEN) — просто пропускает, ничего не падает."""
    if _bot_instance is None:
        return
    for admin_id in ADMIN_IDS:
        try:
            await _bot_instance.send_message(admin_id, text)
        except Exception as e:  # noqa: BLE001 — не роняем запрос гостя из-за проблемы с уведомлением
            logger.warning("Не удалось отправить уведомление админу %s: %s", admin_id, e)


# ---------- эндпоинты мини-приложения ----------

@app.get("/api/tiers")
def api_tiers():
    """Статическая информация об уровнях программы лояльности — пороги
    и кэшбэк, чтобы не дублировать эти цифры в коде мини-приложения."""
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
    try:
        tg_user = parse_and_verify(payload.initData)
    except InvalidInitData as e:
        raise HTTPException(status_code=401, detail=str(e))

    client = get_or_create_client(db, tg_user)
    return client_to_dict(client)


@app.post("/api/link-phone")
def api_link_phone(payload: LinkPhonePayload, db: Session = Depends(get_db)):
    """Клиент подтверждает номер телефона — так его карточка в мини-приложении
    связывается с записью в CRM/POS клуба."""
    try:
        tg_user = parse_and_verify(payload.initData)
    except InvalidInitData as e:
        raise HTTPException(status_code=401, detail=str(e))

    client = get_or_create_client(db, tg_user)
    client.phone = payload.phone
    db.commit()
    return client_to_dict(client)


@app.get("/api/promotions")
def api_promotions(db: Session = Depends(get_db)):
    """Отдаёт все активные акции клуба — они не зависят от уровня клиента."""
    promos = (
        db.query(Promotion)
        .filter(Promotion.active == True)  # noqa: E712
        .order_by(Promotion.created_at.desc())
        .all()
    )
    return [
        {"id": p.id, "title": p.title, "description": p.description, "link": p.link}
        for p in promos
    ]


@app.post("/api/spin")
def api_spin(payload: SpinPayload, db: Session = Depends(get_db)):
    try:
        tg_user = parse_and_verify(payload.initData)
    except InvalidInitData as e:
        raise HTTPException(status_code=401, detail=str(e))

    client = get_or_create_client(db, tg_user)

    if not cooldown_ok(client.last_spin_at, SPIN_COOLDOWN_HOURS):
        next_available = client.last_spin_at + timedelta(hours=SPIN_COOLDOWN_HOURS)
        raise HTTPException(
            status_code=429,
            detail=f"Следующий прокрут доступен после {next_available.isoformat()}",
        )

    prize = spin_roulette()
    client.last_spin_at = datetime.utcnow()

    result = SpinResult(tg_id=client.tg_id, prize_id=prize.id, prize_label=prize.label)
    db.add(result)
    db.commit()

    return {
        "prize_id": prize.id,
        "prize_label": prize.label,
        "value": prize.value,
        "instructions": "Покажи этот экран администратору на кассе, чтобы получить бонусы.",
    }


@app.post("/api/dice")
def api_dice(payload: DicePayload, db: Session = Depends(get_db)):
    try:
        tg_user = parse_and_verify(payload.initData)
    except InvalidInitData as e:
        raise HTTPException(status_code=401, detail=str(e))

    client = get_or_create_client(db, tg_user)

    if not cooldown_ok(client.last_dice_at, DICE_COOLDOWN_HOURS):
        next_available = client.last_dice_at + timedelta(hours=DICE_COOLDOWN_HOURS)
        raise HTTPException(
            status_code=429,
            detail=f"Следующий бросок доступен после {next_available.isoformat()}",
        )

    value = random.randint(1, 6)
    bonus = value * 10
    client.last_dice_at = datetime.utcnow()
    db.commit()

    return {"value": value, "bonus": bonus}


@app.post("/api/probability")
def api_probability(payload: ProbabilityPayload, db: Session = Depends(get_db)):
    try:
        tg_user = parse_and_verify(payload.initData)
    except InvalidInitData as e:
        raise HTTPException(status_code=401, detail=str(e))

    client = get_or_create_client(db, tg_user)

    if not cooldown_ok(client.last_probability_at, PROBABILITY_COOLDOWN_HOURS):
        next_available = client.last_probability_at + timedelta(hours=PROBABILITY_COOLDOWN_HOURS)
        raise HTTPException(
            status_code=429,
            detail=f"Следующая попытка доступна после {next_available.isoformat()}",
        )

    threshold = max(1, min(90, payload.threshold))
    win_chance = (threshold / 100) * PROBABILITY_HOUSE_FACTOR
    win = random.random() < win_chance

    if win:
        rolled_number = random.randint(1, threshold)
    else:
        rolled_number = random.randint(threshold + 1, 100)

    reward = 0
    if win:
        reward = max(10, round(((100 - threshold) * 1.2) / 10) * 10)

    client.last_probability_at = datetime.utcnow()
    db.commit()

    return {"win": win, "rolled_number": rolled_number, "reward": reward}


@app.post("/api/ideas")
async def api_ideas(payload: IdeaPayload, db: Session = Depends(get_db)):
    try:
        tg_user = parse_and_verify(payload.initData)
    except InvalidInitData as e:
        raise HTTPException(status_code=401, detail=str(e))

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
    try:
        tg_user = parse_and_verify(payload.initData)
    except InvalidInitData as e:
        raise HTTPException(status_code=401, detail=str(e))

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


# ---------- админ (временно, до автосинхронизации с CRM/POS) ----------

@app.post("/api/admin/topup")
def api_admin_topup(payload: AdminTopupPayload, db: Session = Depends(get_db)):
    if not ADMIN_SECRET or payload.secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Неверный секрет")

    client = db.query(Client).filter(Client.phone == payload.phone).first()
    if not client:
        raise HTTPException(status_code=404, detail="Клиент с таким телефоном ещё не открывал мини-приложение")

    client.monthly_topup = payload.monthly_topup
    db.commit()
    return client_to_dict(client)


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

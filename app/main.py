"""
Точка входа: веб-API мини-приложения + Telegram-бот в одном процессе.

Один сервис на Railway/Render запускает:
- FastAPI-приложение, которое отдаёт данные для мини-приложения
  (уровень, прогресс, акции, рулетка);
- Telegram-бота (long polling) фоновой задачей при старте.

Так проще для одного человека без опыта DevOps — не нужно поднимать
два отдельных сервиса и синхронизировать их между собой.
"""
import asyncio
import contextlib
import logging
import os
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .database import Base, engine, get_db
from .models import Client, Promotion, Idea, SpinResult, get_tier, get_next_tier_info, TIER_LABELS
from .prizes import spin as spin_roulette
from .telegram_auth import parse_and_verify, InvalidInitData
from .bot import build_bot_and_dispatcher

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("colizeum")

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
SPIN_COOLDOWN_HOURS = int(os.getenv("SPIN_COOLDOWN_HOURS", "24"))

Base.metadata.create_all(bind=engine)

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


class IdeaPayload(BaseModel):
    initData: str
    text: str


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


def client_to_dict(client: Client) -> dict:
    tier = get_tier(client.monthly_topup)
    next_tier, remaining = get_next_tier_info(client.monthly_topup)
    can_spin = True
    if client.last_spin_at:
        can_spin = datetime.utcnow() - client.last_spin_at >= timedelta(hours=SPIN_COOLDOWN_HOURS)
    return {
        "name": client.tg_name,
        "phone": client.phone,
        "monthly_topup": client.monthly_topup,
        "tier": tier,
        "tier_label": TIER_LABELS[tier],
        "next_tier": TIER_LABELS.get(next_tier) if next_tier else None,
        "remaining_to_next_tier": remaining,
        "can_spin": can_spin,
        "spin_cooldown_hours": SPIN_COOLDOWN_HOURS,
    }


# ---------- эндпоинты мини-приложения ----------

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
    """Клиент подтверждает номер телефона кнопкой Telegram request_contact —
    так его карточка в мини-приложении связывается с записью в CRM/POS клуба."""
    try:
        tg_user = parse_and_verify(payload.initData)
    except InvalidInitData as e:
        raise HTTPException(status_code=401, detail=str(e))

    client = get_or_create_client(db, tg_user)
    client.phone = payload.phone
    db.commit()
    return client_to_dict(client)


@app.get("/api/promotions")
def api_promotions(tier: str = "silver", db: Session = Depends(get_db)):
    """Отдаёт акции, доступные для уровня tier и ниже него по иерархии."""
    order = ["silver", "gold", "premium"]
    max_index = order.index(tier) if tier in order else 0
    visible_tiers = order[: max_index + 1]

    promos = (
        db.query(Promotion)
        .filter(Promotion.active == True)  # noqa: E712
        .filter(Promotion.min_tier.in_(visible_tiers))
        .order_by(Promotion.created_at.desc())
        .all()
    )
    return [
        {"id": p.id, "title": p.title, "description": p.description, "min_tier": p.min_tier}
        for p in promos
    ]


@app.post("/api/spin")
def api_spin(payload: SpinPayload, db: Session = Depends(get_db)):
    try:
        tg_user = parse_and_verify(payload.initData)
    except InvalidInitData as e:
        raise HTTPException(status_code=401, detail=str(e))

    client = get_or_create_client(db, tg_user)

    if client.last_spin_at and datetime.utcnow() - client.last_spin_at < timedelta(hours=SPIN_COOLDOWN_HOURS):
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
        "instructions": (
            "Ничего не поделаешь в этот раз — заходи завтра!"
            if prize.id == "empty"
            else "Покажи этот экран администратору на кассе, чтобы получить приз."
        ),
    }


@app.post("/api/ideas")
def api_ideas(payload: IdeaPayload, db: Session = Depends(get_db)):
    try:
        tg_user = parse_and_verify(payload.initData)
    except InvalidInitData as e:
        raise HTTPException(status_code=401, detail=str(e))

    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Пустая идея")

    idea = Idea(tg_id=tg_user["id"], tg_name=tg_user.get("first_name"), text=payload.text.strip())
    db.add(idea)
    db.commit()
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


@app.on_event("startup")
async def start_bot():
    global _bot_task
    if not os.getenv("BOT_TOKEN"):
        logger.warning("BOT_TOKEN не задан — бот не запущен, работает только веб-API")
        return

    bot, dp = build_bot_and_dispatcher()

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

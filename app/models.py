"""
Модели базы данных и логика уровней клиента.

Уровень считается по сумме пополнений за ТЕКУЩИЙ календарный месяц
(monthly_topup). Пока нет автоматической синхронизации с CRM/POS клуба,
эту сумму обновляет администратор вручную (см. эндпоинт /api/admin/topup
в main.py) — либо руками через выгрузку CSV, либо позже через API,
если франчайзер его откроет.
"""
from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean, BigInteger, func,
)

from .database import Base

# Пороги уровней в рублях за месяц — правь здесь, если правила программы
# лояльности изменятся. Клиент ниже порога Silver всё равно попадает в
# Silver — это базовый уровень программы, а не "нет уровня".
TIER_THRESHOLDS = [
    ("silver", 0),
    ("gold", 10000),
    ("premium", 20000),
]

# Порог, начиная с которого статус Silver закрепляется официально
# (используется только в текстах "как устроены уровни" — на сам расчёт
# уровня не влияет, см. TIER_THRESHOLDS выше).
SILVER_QUALIFY_FROM = 5000

TIER_LABELS = {
    "silver": "Silver",
    "gold": "Gold",
    "premium": "Premium",
}

# Кэшбэк по уровням, % — используется и в API (/api/tiers), и в текстах бота.
TIER_CASHBACK = {
    "silver": 10,
    "gold": 15,
    "premium": 20,
}


def get_tier(monthly_topup: float) -> str:
    tier = TIER_THRESHOLDS[0][0]
    for name, threshold in TIER_THRESHOLDS:
        if monthly_topup >= threshold:
            tier = name
    return tier


def get_next_tier_info(monthly_topup: float):
    """Возвращает (следующий_уровень, сколько_осталось) либо (None, 0),
    если клиент уже на максимальном уровне."""
    for name, threshold in TIER_THRESHOLDS:
        if monthly_topup < threshold:
            return name, round(threshold - monthly_topup, 2)
    return None, 0.0


class Client(Base):
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True, index=True, nullable=False)
    tg_name = Column(String, nullable=True)
    phone = Column(String, nullable=True, index=True)
    monthly_topup = Column(Float, default=0.0)
    last_spin_at = Column(DateTime, nullable=True)
    last_dice_at = Column(DateTime, nullable=True)
    last_probability_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Promotion(Base):
    __tablename__ = "promotions"

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=False)
    min_tier = Column(String, default="silver")  # оставлено на будущее, сейчас не фильтрует
    link = Column(String, nullable=True)  # необязательная ссылка (например, на отзывы)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Idea(Base):
    __tablename__ = "ideas"

    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, nullable=False)
    tg_name = Column(String, nullable=True)
    text = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class SpinResult(Base):
    __tablename__ = "spin_results"

    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, nullable=False)
    prize_id = Column(String, nullable=False)
    prize_label = Column(String, nullable=False)
    redeemed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Booking(Base):
    """Заявка из раздела «Быстрое бронирование» — администратор получает
    её мгновенно сообщением от бота и подтверждает вручную."""
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, nullable=False)
    tg_name = Column(String, nullable=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    time_text = Column(String, nullable=False)
    seats_text = Column(String, nullable=True)
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

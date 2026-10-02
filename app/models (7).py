"""
Модели базы данных и логика уровней клиента.

Уровень считается по СРЕДНИМ пополнениям в месяц (поле monthly_topup).
Администратор присылает боту выгрузку из CRM, бот считает среднее по
каждому телефону и хранит его в таблице PhoneStat. Когда гость
привязывает номер в приложении, уровень подтягивается оттуда сразу.
"""
from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean, BigInteger, func,
)

from .database import Base

# Пороги уровней — средние пополнения в месяц, ₽. Правь здесь, если правила
# программы лояльности изменятся; приложение и бот подхватят сами.
# Ниже порога Silver — «Без статуса» (кэшбэка нет).
TIER_THRESHOLDS = [
    ("none", 0),
    ("silver", 2000),
    ("gold", 5000),
    ("premium", 10000),
]

SILVER_QUALIFY_FROM = dict(TIER_THRESHOLDS)["silver"]

from .phones import normalize_phone  # noqa: E402,F401 — используется в main.py и bot.py


TIER_LABELS = {
    "none": "Без статуса",
    "silver": "Silver",
    "gold": "Gold",
    "premium": "Premium",
}

# Кэшбэк по уровням, %
TIER_CASHBACK = {
    "none": 0,
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
    phone_normalized = Column(String, nullable=True, index=True)  # для сверки с выгрузкой из CRM
    monthly_topup = Column(Float, default=0.0)
    balance = Column(Integer, default=0)  # бонусные рубли, накопленные в рулетке/костях/«Дайс»
    last_spin_at = Column(DateTime, nullable=True)
    last_dice_at = Column(DateTime, nullable=True)
    last_probability_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)  # обновляется при каждом заходе в мини-апп
    phone_verified = Column(Boolean, default=False)    # номер получен от Telegram (кнопка «Поделиться номером»)
    last_withdraw_at = Column(DateTime, nullable=True) # последняя заявка на списание бонусов
    consent_at = Column(DateTime, nullable=True)       # когда гость дал согласие на обработку ПДн
    consent_version = Column(String, nullable=True)    # какую редакцию политики он принял
    # Если в CRM клуба гость записан на другой номер, чем в Telegram, — админ
    # связывает их командой /link_crm, и уровень берётся по номеру из CRM.
    crm_phone_normalized = Column(String, nullable=True)
    referred_by = Column(Integer, nullable=True)        # id гостя, который пригласил (реферальная ссылка)
    broadcast_opt_out = Column(Boolean, default=False)  # гость отписался от рассылок (/stop)
    created_at = Column(DateTime, default=datetime.utcnow)


def topup_key(client) -> str | None:
    """По какому номеру искать пополнения гостя в статистике CRM."""
    if not client.phone_verified:
        return None
    return client.crm_phone_normalized or client.phone_normalized


def lookup_topup(db, client) -> tuple[float, bool]:
    """Средние пополнения гостя в месяц — ВСЕГДА берутся из свежей статистики
    CRM (таблица PhoneStat), а не из копии в карточке гостя. Возвращает
    (сумма, нашёлся_ли_номер_в_CRM). Без подтверждённого номера — (0, False)."""
    key = topup_key(client)
    if not key:
        return 0.0, False
    stat = db.get(PhoneStat, key)
    if stat is None:
        return 0.0, False
    return float(stat.avg_monthly or 0), True


def load_topup_map(db) -> dict:
    """Вся статистика CRM одним запросом — для выгрузок и сводок."""
    return {p: float(a or 0) for p, a in db.query(PhoneStat.phone_normalized, PhoneStat.avg_monthly).all()}


def resync_client_topups(db) -> int:
    """Пересчитывает копию monthly_topup у всех гостей по свежей статистике CRM.
    Нужна только для сортировки в выгрузках; на экран уровень идёт из PhoneStat.
    Возвращает, скольким гостям значение поменялось."""
    topups = load_topup_map(db)
    changed = 0
    for c in db.query(Client).all():
        key = topup_key(c)
        value = topups.get(key, 0.0) if key else 0.0
        if abs((c.monthly_topup or 0) - value) > 0.009:
            c.monthly_topup = value
            changed += 1
    db.commit()
    return changed


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


class PhoneStat(Base):
    """Средние пополнения по номеру телефона из выгрузки CRM. Хранится для
    ВСЕХ гостей из выгрузки — даже тех, кто ещё не открывал приложение, —
    чтобы при привязке номера уровень появлялся сразу."""
    __tablename__ = "phone_stats"

    phone_normalized = Column(String, primary_key=True)
    avg_monthly = Column(Float, default=0.0)
    total = Column(Float, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)


class GamePlay(Base):
    """Журнал всех игр. По нему считается, сколько бесплатных попыток
    осталось у гостя за последние 24 часа, и видно, сколько бонусов
    раздано/проиграно в ставках."""
    __tablename__ = "game_plays"

    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, nullable=False, index=True)
    game = Column(String, nullable=False)   # spin | dice | probability
    mode = Column(String, nullable=False)   # free | bet
    stake = Column(Integer, default=0)      # сколько бонусов поставил (для bet)
    payout = Column(Integer, default=0)     # сколько бонусов начислено по итогу
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class Withdrawal(Base):
    """Заявка гостя на списание бонусов с баланса в приложении —
    администратор получает её сообщением и начисляет бонусы в CRM клуба."""
    __tablename__ = "withdrawals"

    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, nullable=False)
    tg_name = Column(String, nullable=True)
    tg_username = Column(String, nullable=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    phone_normalized = Column(String, nullable=True, index=True)  # для лимита «на один номер в сутки»
    amount = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class ReferralReward(Base):
    """Начисление за приглашённого друга. Ключ — номер друга: за один номер
    бонус начисляется только один раз, даже если его заводят на разные аккаунты."""
    __tablename__ = "referral_rewards"

    phone_normalized = Column(String, primary_key=True)
    referrer_id = Column(Integer, nullable=False, index=True)   # Client.id пригласившего
    referred_tg_id = Column(BigInteger, nullable=False)
    amount = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class CrashRound(Base):
    """Раунд Crash. Точка краха определяется в момент ставки и хранится
    только на сервере — гость узнаёт её, когда раунд закончился."""
    __tablename__ = "crash_rounds"

    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, nullable=False, index=True)
    stake = Column(Integer, nullable=False)
    crash_point = Column(Float, nullable=False)
    auto_cashout = Column(Float, nullable=True)
    status = Column(String, nullable=False, default="running")   # running | cashed | crashed
    cashout_mult = Column(Float, nullable=True)
    payout = Column(Integer, default=0)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True, index=True)


class HiloRound(Base):
    """Раунд «Больше-меньше»: ставка сделана, карта клуба открыта, ждём выбор гостя."""
    __tablename__ = "hilo_rounds"

    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, nullable=False, index=True)
    stake = Column(Integer, nullable=False)
    dealer_card = Column(Integer, nullable=False)      # 0…51
    status = Column(String, nullable=False, default="open")   # open | won | lost
    choice = Column(String, nullable=True)             # higher | lower
    player_card = Column(Integer, nullable=True)
    coef = Column(Float, nullable=True)
    payout = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)


class Battle(Base):
    """Батл двух гостей: вызов → принят → предложения игр и сами партии.
    Игрок 0 — кто вызвал, игрок 1 — кого вызвали."""
    __tablename__ = "battles"

    id = Column(Integer, primary_key=True)
    inviter_id = Column(Integer, nullable=False, index=True)       # Client.id
    invitee_id = Column(Integer, nullable=True, index=True)        # Client.id (None — номера нет в боте)
    invitee_phone = Column(String, nullable=True)
    status = Column(String, nullable=False, default="pending")     # pending | active | declined | closed | expired
    offer_game = Column(String, nullable=True)                     # blackjack | crash | poker
    offer_stake = Column(Integer, nullable=True)
    offer_by = Column(Integer, nullable=True)                      # Client.id, кто предложил
    match_id = Column(Integer, nullable=True)                      # текущая или последняя партия
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class BattleMatch(Base):
    """Одна партия батла. Ставки обоих игроков заморожены (списаны с баланса)
    в момент старта и выплачиваются по итогам."""
    __tablename__ = "battle_matches"

    id = Column(Integer, primary_key=True)
    battle_id = Column(Integer, nullable=False, index=True)
    game = Column(String, nullable=False)
    p0_id = Column(Integer, nullable=False)
    p1_id = Column(Integer, nullable=False)
    stakes = Column(String, nullable=False)          # JSON [ставка игрока 0, ставка игрока 1]
    state = Column(String, nullable=False)           # JSON — состояние партии
    status = Column(String, nullable=False, default="running", index=True)   # running | finished
    winner_idx = Column(Integer, nullable=True)
    payouts = Column(String, nullable=True)          # JSON [выплата игроку 0, игроку 1]
    rake = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)

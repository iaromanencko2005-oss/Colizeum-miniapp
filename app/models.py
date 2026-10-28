"""
Модели базы данных и логика уровней клиента.

Уровень считается по СРЕДНИМ пополнениям в месяц (поле monthly_topup).
Администратор присылает боту выгрузку из CRM, бот считает среднее по
каждому телефону и хранит его в таблице PhoneStat. Когда гость
привязывает номер в приложении, уровень подтягивается оттуда сразу.
"""
from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean, BigInteger, LargeBinary, Text, func,
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
    nickname = Column(String, nullable=True)            # никнейм для рейтинга (виден другим гостям)
    nickname_changed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Бонусы действуют сутки: таймер запускается, когда на пустом балансе появились
    # бонусы, и не продлевается новыми начислениями. В срок всё, что осталось, сгорает.
    bonus_expires_at = Column(DateTime, nullable=True, index=True)
    bonus_warned = Column(Boolean, default=False)   # предупредили ли, что скоро сгорят
    # Профиль игрока — его видят другие гости (без имени и телефона)
    about = Column(String, nullable=True)
    games = Column(Text, nullable=True)            # JSON-список любимых игр
    rank = Column(String, nullable=True)           # «Faceit 8», «Легенда 3» — как сам напишет
    tg_username = Column(String, nullable=True)    # @username в Telegram — для связи тиммейтов
    # Бесплатные игры — только тем, кто пополнял счёт в клубе: номер найден в CRM
    # или администратор подтвердил пополнение по заявке «Проверить пополнение».
    games_unlocked_at = Column(DateTime, nullable=True)
    games_locked = Column(Boolean, default=False)      # админ закрыл вручную (/lock) — CRM не открывает
    topup_check_at = Column(DateTime, nullable=True)   # когда гость последний раз просил проверку
    topup_check_state = Column(String, nullable=True)  # pending | declined | approved


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
    # Второй бонус — когда друг впервые пополнит счёт в клубе ПОСЛЕ регистрации.
    # base_total — сколько друг пополнил по выгрузке CRM на момент регистрации:
    # бонус платится, когда по новой выгрузке сумма станет больше.
    base_total = Column(Float, nullable=True)
    activated_at = Column(DateTime, nullable=True)
    activation_amount = Column(Integer, default=0)


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


class WelcomeBonus(Base):
    """Приветственные бонусы. Ключ — номер телефона: один номер — один подарок,
    даже если его переносят на новый аккаунт Telegram."""
    __tablename__ = "welcome_bonuses"

    phone_normalized = Column(String, primary_key=True)
    client_id = Column(Integer, nullable=False)
    amount = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class RatingAward(Base):
    """Призы по итогам месяца в рейтинге. Ключ (месяц, место) — приз за месяц
    выдаётся ровно один раз, даже если сервер перезапустится."""
    __tablename__ = "rating_awards"

    month = Column(String, primary_key=True)        # «2026-10»
    place = Column(Integer, primary_key=True)
    client_id = Column(Integer, nullable=True)
    nickname = Column(String, nullable=True)
    volume = Column(Integer, default=0)
    amount = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class FriendAward(Base):
    """Призы рейтинга «Самый дружелюбный» (кто привёл больше друзей за месяц).
    Ключ (месяц, место) — приз выдаётся ровно один раз."""
    __tablename__ = "friend_awards"

    month = Column(String, primary_key=True)
    place = Column(Integer, primary_key=True)
    client_id = Column(Integer, nullable=True)
    nickname = Column(String, nullable=True)
    friends = Column(Integer, default=0)
    amount = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


# ---------- магазин за бонусы ----------

class ShopProduct(Base):
    """Товар магазина. Вкусы/варианты — JSON-список [{"id","label","active"}].
    Цены и наличие админ меняет командами в боте — при обновлении кода они не сбрасываются."""
    __tablename__ = "shop_products"

    id = Column(String, primary_key=True)          # «redbull», «snickers»…
    name = Column(String, nullable=False)
    category = Column(String, nullable=False)      # energy | soda | tea | water | sweets
    price = Column(Integer, nullable=False)        # цена в бонусах
    bar_price = Column(Integer, nullable=True)     # цена в баре, ₽ — для справки
    adult = Column(Boolean, default=False)         # 18+ (энергетики)
    active = Column(Boolean, default=True)
    sort = Column(Integer, default=0)
    variants = Column(Text, nullable=False, default="[]")
    updated_at = Column(DateTime, default=datetime.utcnow)


class ShopImage(Base):
    """Фото варианта товара (webp/jpeg), ключ «товар/вариант»."""
    __tablename__ = "shop_images"

    key = Column(String, primary_key=True)
    mime = Column(String, default="image/webp")
    data = Column(LargeBinary, nullable=False)
    version = Column(String, nullable=True)        # для кэша в браузере


class ShopOrder(Base):
    """Заказ в магазине. Бонусы списываются сразу; администратор выдаёт товар,
    увидев «живой» экран заказа у гостя, и отмечает выдачу кнопкой в боте."""
    __tablename__ = "shop_orders"

    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, nullable=False, index=True)
    tg_id = Column(BigInteger, nullable=False, index=True)
    phone_normalized = Column(String, nullable=True, index=True)
    items = Column(Text, nullable=False)           # JSON [{"product","variant","name","label","price","qty"}]
    total = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default="new", index=True)   # new | issued | cancelled | expired
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    expires_at = Column(DateTime, nullable=True)       # до какого времени можно забрать товар
    bonus_deadline = Column(DateTime, nullable=True)   # когда сгорели бы бонусы гостя — после этого их не возвращаем
    issued_at = Column(DateTime, nullable=True)
    issued_by = Column(String, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancel_reason = Column(String, nullable=True)


# ---------- отметка «был в клубе» ----------

class CheckIn(Base):
    """Гость отметился в клубе и приложил фото чека. Админ подтверждает в боте —
    гостю начисляются бонусы. Одна отметка в день (по Москве)."""
    __tablename__ = "checkins"

    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, nullable=False, index=True)
    tg_id = Column(BigInteger, nullable=False, index=True)
    day = Column(String, nullable=False, index=True)     # «2026-10-17» по Москве
    status = Column(String, nullable=False, default="pending")   # pending | approved | rejected
    photo_file_id = Column(String, nullable=True)        # фото хранится в Telegram, не у нас
    bonus = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    decided_at = Column(DateTime, nullable=True)
    decided_by = Column(String, nullable=True)


# ---------- прогнозы на киберспорт ----------

class EsportsMatch(Base):
    """Матч для прогнозов. Коэффициенты берутся у букмекера и обновляются,
    пока матч не начался; в прогноз гостя записывается коэффициент на момент ставки."""
    __tablename__ = "esports_matches"

    id = Column(Integer, primary_key=True)
    source = Column(String, nullable=False, default="manual")   # pinnacle | manual
    source_id = Column(String, nullable=True, index=True)
    game = Column(String, nullable=False)                       # CS2, Dota 2…
    league = Column(String, nullable=True)
    team1 = Column(String, nullable=False)
    team2 = Column(String, nullable=False)
    odds1 = Column(Float, nullable=True)
    odds2 = Column(Float, nullable=True)
    start_at = Column(DateTime, nullable=False, index=True)     # UTC
    status = Column(String, nullable=False, default="open", index=True)  # open | settled | cancelled
    winner = Column(Integer, nullable=True)                     # 1 | 2
    odds_updated_at = Column(DateTime, nullable=True)
    reminded = Column(Integer, default=0)                       # сколько раз напомнили админу подвести итог
    announced = Column(Boolean, default=False)                  # опубликован в группе клуба
    created_at = Column(DateTime, default=datetime.utcnow)
    settled_at = Column(DateTime, nullable=True)


class Prediction(Base):
    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, nullable=False, index=True)
    client_id = Column(Integer, nullable=False, index=True)
    tg_id = Column(BigInteger, nullable=False, index=True)
    pick = Column(Integer, nullable=False)          # 1 | 2
    stake = Column(Integer, nullable=False)
    odds = Column(Float, nullable=False)
    status = Column(String, nullable=False, default="open")   # open | won | lost | refunded
    payout = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class BonusBurn(Base):
    """Сгоревшие бонусы (не потрачены за сутки) — для статистики."""
    __tablename__ = "bonus_burns"

    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, nullable=False, index=True)
    amount = Column(Integer, nullable=False)
    reason = Column(String, default="expired")      # expired | order_refund_late
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class VisitReward(Base):
    """Награды серии визитов за месяц (3, 6, 10 подтверждённых чеков) — каждая один раз."""
    __tablename__ = "visit_rewards"

    client_id = Column(Integer, primary_key=True)
    month = Column(String, primary_key=True)           # «2026-10»
    milestone = Column(Integer, primary_key=True)      # 3 | 6 | 10
    reward = Column(String, nullable=False)            # «50» или «hour»
    created_at = Column(DateTime, default=datetime.utcnow)


class TeamPost(Base):
    """Заявка «ищу тиммейтов»."""
    __tablename__ = "team_posts"

    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, nullable=False, index=True)
    game = Column(String, nullable=False)
    play_at = Column(DateTime, nullable=False, index=True)   # UTC
    need = Column(Integer, default=1)
    comment = Column(String, nullable=True)
    status = Column(String, default="open", index=True)      # open | closed
    created_at = Column(DateTime, default=datetime.utcnow)


class TeamJoin(Base):
    __tablename__ = "team_joins"

    post_id = Column(Integer, primary_key=True)
    client_id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ClubEvent(Base):
    """Событие афиши: турнир, марафон, просмотр матча. Заводится админом в боте."""
    __tablename__ = "club_events"

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    starts_at = Column(DateTime, nullable=False, index=True)  # UTC
    image = Column(LargeBinary, nullable=True)
    image_ver = Column(String, nullable=True)
    status = Column(String, default="active", index=True)     # active | deleted
    created_at = Column(DateTime, default=datetime.utcnow)


class EventGoing(Base):
    __tablename__ = "event_going"

    event_id = Column(Integer, primary_key=True)
    client_id = Column(Integer, primary_key=True)
    reminded = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class ClubNews(Base):
    """Новость клуба: изменения и нововведения. Гости ставят реакцию — рады или не интересно."""
    __tablename__ = "club_news"

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    body = Column(Text, nullable=True)
    image = Column(LargeBinary, nullable=True)
    image_ver = Column(String, nullable=True)
    status = Column(String, default="active", index=True)     # active | deleted
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class NewsReaction(Base):
    __tablename__ = "news_reactions"

    news_id = Column(Integer, primary_key=True)
    client_id = Column(Integer, primary_key=True)
    value = Column(Integer, nullable=False)                    # 1 — рад(а), -1 — не интересно
    created_at = Column(DateTime, default=datetime.utcnow)

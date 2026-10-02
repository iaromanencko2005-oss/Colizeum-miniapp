"""
Никнеймы, приветственные бонусы и рейтинг месяца.

Рейтинг — по ОБОРОТУ за календарный месяц (по московскому времени):
    выигрыши в бесплатных играх + все ставки в играх на бонусы и в батлах.
Пример: гость выиграл бесплатно 80 бонусов и прокрутил их ставками на
2 000 — его оборот 2 080, даже если в итоге всё проиграл.

В рейтинге участвуют гости с подтверждённым номером и никнеймом. Другие
видят только никнейм — ни имени, ни телефона.

Итоги подводятся автоматически: в первые минуты нового месяца победители
прошлого месяца получают призы на баланс и сообщение от бота. Приз за
месяц выдаётся ровно один раз (это хранится в базе).

Настройки (Railway → Variables):
  WELCOME_BONUS  — подарок за регистрацию (по умолчанию 100);
  RATING_PRIZES  — призы за места через запятую (по умолчанию «3000» — только 1 место;
                   например «3000,1500,500» — призы за 1, 2 и 3 места).
"""
import os
import re
from datetime import datetime, timedelta

from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError

from .models import Client, GamePlay, RatingAward, WelcomeBonus

WELCOME_BONUS = int(os.getenv("WELCOME_BONUS", "100"))
RATING_PRIZES = [int(x) for x in os.getenv("RATING_PRIZES", "3000").replace(" ", "").split(",") if x]
RATING_FIRST_MONTH = os.getenv("RATING_FIRST_MONTH", "2026-10")   # раньше этого месяца призы не выдаются
MSK = timedelta(hours=3)
NICK_MIN, NICK_MAX = 3, 16

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
          "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]


class NicknameError(Exception):
    pass


# ---------- никнеймы ----------

_ALLOWED = re.compile(r"^[0-9A-Za-zА-Яа-яЁё _.\-]+$")
_RESERVED = ("admin", "админ", "colizeum", "колизеум", "модератор", "moderator", "support", "поддержка")
# Самые грубые корни — остальное админ правит командой /nick.
_BAD = re.compile(r"(х[уy][йеёя]|пизд|[её]бан|[её]бл[аоия]|бляд|блят|пид[оa]р|муд[аи]к|залуп|шлюх|fuck|shit|bitch)",
                  re.IGNORECASE)


def clean_nickname(raw: str) -> str:
    nick = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not (NICK_MIN <= len(nick) <= NICK_MAX):
        raise NicknameError(f"Никнейм — от {NICK_MIN} до {NICK_MAX} символов")
    if not _ALLOWED.match(nick):
        raise NicknameError("Можно буквы, цифры, пробел, точку, дефис и подчёркивание")
    low = nick.lower().replace(" ", "")
    if any(r in low for r in _RESERVED):
        raise NicknameError("Этот никнейм занят — придумай другой")
    if _BAD.search(low.replace(".", "").replace("_", "").replace("-", "")):
        raise NicknameError("Такой никнейм не подойдёт — придумай другой")
    return nick


def nickname_taken(db, nick: str, except_id: int | None = None) -> bool:
    q = db.query(Client.id).filter(func.lower(Client.nickname) == nick.lower())
    if except_id:
        q = q.filter(Client.id != except_id)
    return q.first() is not None


def set_nickname(db, client: Client, raw: str, admin: bool = False) -> str:
    nick = clean_nickname(raw) if not admin else re.sub(r"\s+", " ", raw).strip()[:NICK_MAX]
    if nickname_taken(db, nick, client.id):
        raise NicknameError("Такой никнейм уже занят — придумай другой")
    now = datetime.utcnow()
    if not admin and client.nickname and client.nickname_changed_at and now - client.nickname_changed_at < timedelta(hours=24):
        raise NicknameError("Никнейм можно менять раз в сутки")
    client.nickname = nick
    client.nickname_changed_at = now
    db.commit()
    return nick


# ---------- приветственный бонус ----------

def welcome_pending(db, client: Client) -> bool:
    """Получит ли гость подарок, когда закончит регистрацию."""
    if WELCOME_BONUS <= 0:
        return False
    if client.phone_normalized and db.get(WelcomeBonus, client.phone_normalized) is not None:
        return False
    return db.query(WelcomeBonus.phone_normalized).filter(WelcomeBonus.client_id == client.id).first() is None


def try_welcome(db, client: Client) -> int:
    """Начисляет подарок за регистрацию, когда всё сделано: согласие, номер через
    Telegram и никнейм. Один номер — один подарок. Возвращает сумму или 0."""
    if WELCOME_BONUS <= 0 or not (client.consent_at and client.phone_verified and client.phone_normalized
                                  and client.nickname):
        return 0
    if not welcome_pending(db, client):
        return 0
    try:
        db.add(WelcomeBonus(phone_normalized=client.phone_normalized, client_id=client.id, amount=WELCOME_BONUS))
        db.flush()
        db.query(Client).filter(Client.id == client.id).update(
            {Client.balance: func.coalesce(Client.balance, 0) + WELCOME_BONUS}, synchronize_session=False)
        db.commit()
    except IntegrityError:
        db.rollback()
        return 0
    db.refresh(client)
    return WELCOME_BONUS


# ---------- рейтинг ----------

def month_key(dt_utc: datetime) -> str:
    m = dt_utc + MSK
    return f"{m.year:04d}-{m.month:02d}"


def month_bounds(key: str) -> tuple[datetime, datetime]:
    """Начало и конец месяца по Москве — в UTC, как хранится в базе."""
    y, m = map(int, key.split("-"))
    start = datetime(y, m, 1) - MSK
    end = (datetime(y + (m == 12), m % 12 + 1, 1)) - MSK
    return start, end


def prev_month(key: str) -> str:
    y, m = map(int, key.split("-"))
    return f"{y - (m == 1):04d}-{(m - 2) % 12 + 1:02d}"


def month_title(key: str) -> str:
    y, m = map(int, key.split("-"))
    return f"{MONTHS[m - 1].capitalize()} {y}"


def leaderboard(db, key: str) -> list[dict]:
    """Все участники месяца по убыванию оборота: [{client_id, tg_id, nickname, volume}]."""
    start, end = month_bounds(key)
    vol = func.sum(case((GamePlay.mode == "free", GamePlay.payout), else_=GamePlay.stake))
    rows = (db.query(GamePlay.tg_id, vol.label("vol"), func.min(GamePlay.created_at).label("first"))
            .filter(GamePlay.created_at >= start, GamePlay.created_at < end)
            .group_by(GamePlay.tg_id).all())
    if not rows:
        return []
    clients = {c.tg_id: c for c in db.query(Client).filter(
        Client.tg_id.in_([r[0] for r in rows]), Client.nickname.isnot(None), Client.phone_verified == True).all()}  # noqa: E712
    board = [{"client_id": clients[t].id, "tg_id": t, "nickname": clients[t].nickname, "volume": int(v or 0), "first": f}
             for t, v, f in rows if t in clients and (v or 0) > 0]
    board.sort(key=lambda r: (-r["volume"], r["first"]))     # при равенстве — кто начал раньше
    return board


def rating_view(db, me: Client, limit: int = 50) -> dict:
    now = datetime.utcnow()
    key = month_key(now)
    board = leaderboard(db, key)
    my_place = next((i + 1 for i, r in enumerate(board) if r["client_id"] == me.id), None)
    my_volume = board[my_place - 1]["volume"] if my_place else _my_volume(db, me, key)
    _, end = month_bounds(key)
    last_month = prev_month(key)
    winners = [{"place": a.place, "nickname": a.nickname, "volume": a.volume, "amount": a.amount}
               for a in db.query(RatingAward).filter(RatingAward.month == last_month).order_by(RatingAward.place).all()]
    end_msk = end + MSK - timedelta(minutes=1)
    return {
        "month": month_title(key),
        "ends": f"{end_msk.day} {MONTHS_GEN[end_msk.month - 1]} в 23:59",
        "ends_in_days": max(0, (end - now).days),
        "prizes": RATING_PRIZES,
        "top": [{"place": i + 1, "nickname": r["nickname"], "volume": r["volume"], "me": r["client_id"] == me.id}
                for i, r in enumerate(board[:limit])],
        "participants": len(board),
        "me": {"place": my_place, "volume": my_volume, "nickname": me.nickname,
               "eligible": bool(me.nickname and me.phone_verified)},
        "last_month": month_title(last_month),
        "last_winners": winners,
    }


def _my_volume(db, me: Client, key: str) -> int:
    start, end = month_bounds(key)
    v = db.query(func.sum(case((GamePlay.mode == "free", GamePlay.payout), else_=GamePlay.stake))).filter(
        GamePlay.tg_id == me.tg_id, GamePlay.created_at >= start, GamePlay.created_at < end).scalar()
    return int(v or 0)


def settle_due(db) -> list[dict]:
    """Выдаёт призы за прошлый месяц, если ещё не выдавали. Безопасно вызывать сколько угодно раз.
    Возвращает список награждённых [{tg_id, nickname, place, amount, volume, month}]."""
    key = prev_month(month_key(datetime.utcnow()))
    if key < RATING_FIRST_MONTH or not RATING_PRIZES:
        return []
    if db.query(RatingAward.month).filter(RatingAward.month == key).first() is not None:
        return []
    board = leaderboard(db, key)
    awarded = []
    try:
        for place, prize in enumerate(RATING_PRIZES, start=1):
            r = board[place - 1] if place <= len(board) else None
            db.add(RatingAward(month=key, place=place, client_id=r["client_id"] if r else None,
                               nickname=r["nickname"] if r else None, volume=r["volume"] if r else 0,
                               amount=prize if r else 0))
            if r:
                db.query(Client).filter(Client.id == r["client_id"]).update(
                    {Client.balance: func.coalesce(Client.balance, 0) + prize}, synchronize_session=False)
                awarded.append({"tg_id": r["tg_id"], "nickname": r["nickname"], "place": place,
                                "amount": prize, "volume": r["volume"], "month": month_title(key)})
        db.commit()
    except IntegrityError:          # уже выдали (параллельный запуск) — ничего не делаем
        db.rollback()
        return []
    return awarded

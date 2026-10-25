"""
Сообщество клуба: профиль игрока, серия визитов, поиск тиммейтов, афиша.

Серия визитов — по подтверждённым чекам («Я в клубе») за календарный месяц.
Награды задаются в Railway → Variables:
  VISIT_REWARDS = «3:50,6:100,10:hour» — после 3 визитов +50 колизов, после 6 — +100,
                  после 10 — бесплатный час (его начисляет администратор в CRM, бот напомнит).

Тиммейты — гость публикует заявку (игра, когда, сколько нужно человек), она видна
всем в приложении и уходит постом в Telegram-чат (TEAM_CHAT_ID, иначе CLUB_GROUP_ID).
На отклик бот присылает автору и откликнувшемуся Telegram друг друга.

Афиша — события заводит админ в боте: фото с подписью
  /event 01.11 20:00 | Турнир CS2 5×5 | Описание
Гости жмут «Пойду», бот напоминает им за 2 часа до начала.
"""
import json
import os
import re
from datetime import datetime, timedelta

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from .models import (Client, CheckIn, VisitReward, TeamPost, TeamJoin, ClubEvent, EventGoing,
                     ReferralReward, BattleMatch, ShopOrder, RatingAward, GamePlay)
from .rating import _BAD, month_key

MSK = timedelta(hours=3)
GAMES = ["CS2", "Dota 2", "Valorant", "PUBG", "Fortnite", "Apex Legends", "League of Legends", "Rust",
         "Warzone", "Deadlock", "Overwatch 2", "Minecraft", "GTA V", "EA FC", "Mortal Kombat", "Tekken 8"]
ABOUT_MAX = 200
RANK_MAX = 30
TEAM_MAX_ACTIVE = 2
TEAM_CHAT_ID = [int(x) for x in os.getenv("TEAM_CHAT_ID", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()]


# Короткие грубые слова — только целиком, чтобы не задеть «корабля», «сука» в «сукно» и т.п.
_BAD_WORDS = re.compile(r"(?<![а-яёa-z])(бля|сука|суки|нахуй|нахер|хер|пидр|еблан|уебок|долбоеб|гандон)(?![а-яёa-z])")


class SocialError(Exception):
    pass


def _visit_rewards_cfg() -> list[tuple[int, str]]:
    out = []
    for part in os.getenv("VISIT_REWARDS", "3:50,6:100,10:hour").replace(" ", "").split(","):
        if ":" in part:
            n, r = part.split(":", 1)
            if n.isdigit():
                out.append((int(n), r))
    return sorted(out)


def _clean_text(raw: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(raw or "")).strip()[:limit]
    low = text.lower()
    if text and (_BAD.search(low.replace(" ", "")) or _BAD_WORDS.search(low)):
        raise SocialError("Такие слова писать нельзя — перефразируй")
    return text


# ---------- профиль ----------

def save_profile(db, client: Client, about: str, games: list, rank: str):
    client.about = _clean_text(about, ABOUT_MAX) or None
    client.rank = _clean_text(rank, RANK_MAX) or None
    chosen = [g for g in GAMES if g in (games or [])][:6]
    client.games = json.dumps(chosen, ensure_ascii=False) if chosen else None
    db.commit()


def games_of(client: Client) -> list[str]:
    try:
        return json.loads(client.games or "[]")
    except ValueError:
        return []


def badges(db, client: Client) -> list[dict]:
    visits = db.query(func.count(func.distinct(CheckIn.day))).filter(
        CheckIn.client_id == client.id, CheckIn.status == "approved").scalar() or 0
    friends = db.query(func.count(ReferralReward.phone_normalized)).filter(
        ReferralReward.referrer_id == client.id).scalar() or 0
    wins = db.query(func.count(BattleMatch.id)).filter(
        BattleMatch.status == "finished",
        or_((BattleMatch.p0_id == client.id) & (BattleMatch.winner_idx == 0),
            (BattleMatch.p1_id == client.id) & (BattleMatch.winner_idx == 1))).scalar() or 0
    bought = db.query(func.count(ShopOrder.id)).filter(ShopOrder.client_id == client.id,
                                                       ShopOrder.status == "issued").scalar() or 0
    top3 = db.query(func.count(RatingAward.month)).filter(RatingAward.client_id == client.id,
                                                         RatingAward.place <= 3).scalar() or 0
    plays = db.query(func.count(GamePlay.id)).filter(GamePlay.tg_id == client.tg_id).scalar() or 0
    best = db.query(func.coalesce(func.max(GamePlay.payout), 0)).filter(GamePlay.tg_id == client.tg_id).scalar() or 0
    all_b = [
        ("visit", "Первый визит", visits >= 1),
        ("regular", "10 визитов", visits >= 10),
        ("friends", "Душа компании", friends >= 3),
        ("battle", "Победа в батле", wins >= 1),
        ("shop", "Гурман", bought >= 1),
        ("top3", "Топ-3 месяца", top3 >= 1),
        ("player", "100 игр", plays >= 100),
        ("highroller", "Крупный выигрыш", best >= 1000),
    ]
    return [{"id": i, "title": t, "done": bool(d)} for i, t, d in all_b]


def profile_view(db, client: Client, me: bool = False) -> dict:
    try:
        about = _clean_text(client.about or "", 300)   # старые тексты, сохранённые до фильтра
    except SocialError:
        about = ""
    d = {"nickname": client.nickname, "about": about, "games": games_of(client),
         "rank": client.rank or "", "badges": badges(db, client)}
    if me:
        d["all_games"] = GAMES
    return d


def find_by_nickname(db, nickname: str):
    nick = (nickname or "").strip()
    if not nick:
        return None
    return (db.query(Client).filter(Client.nickname == nick).first()
            or db.query(Client).filter(func.lower(Client.nickname) == nick.lower()).first())


# ---------- серия визитов ----------

def visit_progress(db, client: Client) -> dict:
    mk = month_key(datetime.utcnow())
    count = db.query(func.count(func.distinct(CheckIn.day))).filter(
        CheckIn.client_id == client.id, CheckIn.status == "approved", CheckIn.day.like(mk + "%")).scalar() or 0
    got = {m for (m,) in db.query(VisitReward.milestone).filter(VisitReward.client_id == client.id,
                                                                 VisitReward.month == mk).all()}
    steps = [{"n": n, "reward": r, "done": n in got or count >= n} for n, r in _visit_rewards_cfg()]
    nxt = next((s for s in steps if not s["done"]), None)
    return {"count": count, "steps": steps, "next": nxt}


def grant_visit_rewards(db, client: Client) -> list[dict]:
    """После подтверждённого чека: выдаёт награды за достигнутые ступени месяца (один раз)."""
    mk = month_key(datetime.utcnow())
    prog = visit_progress(db, client)
    granted = []
    for n, reward in _visit_rewards_cfg():
        if prog["count"] < n:
            continue
        try:
            db.add(VisitReward(client_id=client.id, month=mk, milestone=n, reward=reward))
            db.flush()
        except IntegrityError:
            db.rollback()
            continue
        if reward.isdigit():
            db.query(Client).filter(Client.id == client.id).update(
                {Client.balance: func.coalesce(Client.balance, 0) + int(reward)}, synchronize_session=False)
        db.commit()
        granted.append({"n": n, "reward": reward})
    return granted


# ---------- тиммейты ----------

def team_create(db, client: Client, game: str, play_at: datetime, need: int, comment: str) -> TeamPost:
    if game not in GAMES:
        raise SocialError("Выбери игру из списка")
    now = datetime.utcnow()
    if play_at < now - timedelta(minutes=5) or play_at > now + timedelta(days=14):
        raise SocialError("Время — от сейчас и до двух недель вперёд")
    if not (1 <= need <= 9):
        raise SocialError("Нужно от 1 до 9 человек")
    active = db.query(func.count(TeamPost.id)).filter(TeamPost.client_id == client.id, TeamPost.status == "open",
                                                      TeamPost.play_at > now).scalar() or 0
    if active >= TEAM_MAX_ACTIVE:
        raise SocialError(f"У тебя уже {active} активные заявки — закрой одну из них")
    post = TeamPost(client_id=client.id, game=game, play_at=play_at, need=need,
                    comment=_clean_text(comment, 140) or None, status="open")
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


def team_join(db, client: Client, post_id: int) -> TeamPost:
    post = db.get(TeamPost, post_id)
    if post is None or post.status != "open" or post.play_at < datetime.utcnow() - timedelta(hours=1):
        raise SocialError("Заявка уже неактуальна")
    if post.client_id == client.id:
        raise SocialError("Это твоя заявка")
    joined = db.query(func.count(TeamJoin.client_id)).filter(TeamJoin.post_id == post.id).scalar() or 0
    if joined >= post.need:
        raise SocialError("Команда уже собрана")
    try:
        db.add(TeamJoin(post_id=post.id, client_id=client.id))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise SocialError("Ты уже откликнулся на эту заявку")
    return post


def when_text(dt: datetime) -> str:
    d = dt + MSK
    today = (datetime.utcnow() + MSK).date()
    day = "сегодня" if d.date() == today else ("завтра" if d.date() == today + timedelta(days=1) else f"{d:%d.%m}")
    return f"{day} в {d:%H:%M}"


def team_list(db, me: Client) -> list[dict]:
    now = datetime.utcnow()
    posts = (db.query(TeamPost).filter(TeamPost.status == "open", TeamPost.play_at > now - timedelta(hours=1))
             .order_by(TeamPost.play_at.asc()).limit(50).all())
    if not posts:
        return []
    ids = [p.id for p in posts]
    joins = {}
    for pid, cid in db.query(TeamJoin.post_id, TeamJoin.client_id).filter(TeamJoin.post_id.in_(ids)).all():
        joins.setdefault(pid, []).append(cid)
    authors = {c.id: c for c in db.query(Client).filter(Client.id.in_([p.client_id for p in posts])).all()}
    out = []
    for p in posts:
        a = authors.get(p.client_id)
        js = joins.get(p.id, [])
        out.append({"id": p.id, "game": p.game, "when": when_text(p.play_at), "need": p.need, "joined": len(js),
                    "comment": p.comment or "", "author": (a.nickname or "Гость") if a else "Гость",
                    "mine": p.client_id == me.id, "i_joined": me.id in js, "full": len(js) >= p.need})
    return out


# ---------- афиша ----------

# ---------- значок события афиши: подбирается по словам в названии и описании ----------

_EV_SUBJECT = [   # (вид, подпись, корни слов) — первое совпадение
    ("hockey", "Хоккей", r"хокке|\bкхл\b|\bнхл\b"),
    ("football", "Футбол", r"футбол|лиг[аиуе] чемпионов|\bлч\b|\bапл\b|\bрпл\b|ла лиг|серия а|бундеслиг|чемпионат мира|\bевро\b|"
                           r"спартак|цска|зенит|локомотив|динамо|краснодар|реал\b|барселон|манчестер|ливерпул|арсенал|челси|"
                           r"бавари|\bпсж\b|ювентус|милан|интер\b|эль класико"),
    ("basketball", "Баскетбол", r"баскетбол|\bнба\b|\bnba\b"),
    ("fight", "UFC", r"\bufc\b|юфс"),
    ("fight", "Бои", r"\bmma\b|\bмма\b|бокс|\bбой\b|бои\b|поединок|кард"),
    ("fifa", "FIFA", r"fifa|фифа|\bfc ?2[4-9]\b|\bea fc\b"),
    ("shooter", "CS2", r"\bcs ?2?\b|\bкс\b|counter|кэс|faceit|фейсит|мейджор|major"),
    ("dota", "Dota 2", r"dota|дота|доту|\bti\b|international"),
    ("shooter", "Valorant", r"valorant|валорант"),
    ("shooter", "PUBG", r"pubg|пабг"),
    ("game", "PlayStation", r"playstation|\bps ?5?\b|\bпс\b|mortal kombat|\bmk\b|tekken|теккен|gta"),
    ("game", "Fortnite", r"fortnite|фортнайт"),
    ("game", "LoL", r"league of legends|\blol\b"),
    ("night", "Ночь в клубе", r"ночн|марафон|до утра|всю ночь"),
    ("movie", "Кино", r"\bкино|фильм|сериал|мультфильм"),
    ("party", "Вечеринка", r"вечеринк|туса|тусовк|пати|party|диджей|караоке|хэллоуин|хеллоуин|новый год|новогод"),
    ("quiz", "Квиз", r"квиз|quiz|викторин"),
    ("birthday", "День рождения", r"день рождени|\bдр\b|годовщин"),
    ("promo", "Акция", r"акци|скидк|бесплатн|\d+ ?%|розыгрыш|подарк"),
    ("tv", "Трансляция", r"трансляци|смотрим|матч"),
]
_EV_TOURNAMENT = re.compile(r"турнир|чемпионат клуба|кубок|\bлан\b|\blan\b|5[×x]5|1[×x]1|2[×x]2|сетка|призов", re.I)


def event_kind(title: str, description: str = "") -> tuple[str, str]:
    """(вид, подпись) для значка в афише: «Футбол», «Турнир · CS2», «Ночь в клубе»…"""
    text = f"{title} \n {description}".lower().replace("ё", "е")
    tour = bool(_EV_TOURNAMENT.search(text))
    for title_only in (True, False):     # сначала ищем в названии — оно точнее описания
        src = title.lower().replace("ё", "е") if title_only else text
        for kind, label, rx in _EV_SUBJECT:
            if re.search(rx, src):
                if tour and kind in ("shooter", "dota", "fifa", "game", "fight"):
                    return "tournament", f"Турнир · {label}"
                return kind, label
    return ("tournament", "Турнир") if tour else ("event", "Событие")


def parse_event(text: str) -> dict:
    """«01.11 20:00 | Турнир CS2 5×5 | описание» (время московское)."""
    parts = [p.strip() for p in (text or "").split("|")]
    if len(parts) < 2:
        raise SocialError("Формат: /event 01.11 20:00 | Название | Описание (можно с фото)")
    m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s+(\d{1,2})[:.](\d{2})$", parts[0])
    if not m:
        raise SocialError("Дата и время — как «01.11 20:00» (по Москве)")
    now_msk = datetime.utcnow() + MSK
    year = int(m.group(3)) if m.group(3) else now_msk.year
    year += 2000 if year < 100 else 0
    try:
        start = datetime(year, int(m.group(2)), int(m.group(1)), int(m.group(4)), int(m.group(5))) - MSK
    except ValueError:
        raise SocialError("Нет такой даты")
    if not m.group(3) and start < datetime.utcnow() - timedelta(days=1):
        start = start.replace(year=start.year + 1)
    if start < datetime.utcnow():
        raise SocialError("Это время уже прошло")
    return {"starts_at": start, "title": parts[1][:80], "description": (parts[2] if len(parts) > 2 else "")[:600]}


def events_list(db, me: Client | None) -> list[dict]:
    now = datetime.utcnow()
    evs = (db.query(ClubEvent).filter(ClubEvent.status == "active", ClubEvent.starts_at > now - timedelta(hours=3))
           .order_by(ClubEvent.starts_at.asc()).limit(30).all())
    if not evs:
        return []
    ids = [e.id for e in evs]
    counts = dict(db.query(EventGoing.event_id, func.count(EventGoing.client_id)).filter(
        EventGoing.event_id.in_(ids)).group_by(EventGoing.event_id).all())
    mine = set()
    if me is not None:
        mine = {eid for (eid,) in db.query(EventGoing.event_id).filter(EventGoing.client_id == me.id,
                                                                      EventGoing.event_id.in_(ids)).all()}
    return [{"id": e.id, "title": e.title, "description": e.description or "", "when": when_text(e.starts_at),
             "kind": event_kind(e.title, e.description or "")[0], "kind_label": event_kind(e.title, e.description or "")[1],
             "date": f"{(e.starts_at + MSK):%d.%m}", "time": f"{(e.starts_at + MSK):%H:%M}",
             "dow": ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][(e.starts_at + MSK).weekday()],
             "img": f"/api/event/img/{e.id}?v={e.image_ver}" if e.image else None,
             "going": int(counts.get(e.id, 0)), "i_go": e.id in mine} for e in evs]


def event_reminders(db) -> list[tuple]:
    """[(tg_id, событие)] — кому напомнить: событие начнётся в ближайшие 2 часа."""
    now = datetime.utcnow()
    out = []
    rows = (db.query(EventGoing, ClubEvent).join(ClubEvent, ClubEvent.id == EventGoing.event_id)
            .filter(ClubEvent.status == "active", ClubEvent.starts_at > now,
                    ClubEvent.starts_at <= now + timedelta(hours=2), EventGoing.reminded.isnot(True)).all())
    for g, e in rows:
        g.reminded = True
        tg = db.query(Client.tg_id).filter(Client.id == g.client_id).scalar()
        if tg:
            out.append((tg, e.title, when_text(e.starts_at)))
    db.commit()
    return out

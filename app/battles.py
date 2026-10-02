"""
Батлы «гость против гостя»: вызов по номеру телефона, принятие через бота,
предложение игры и ставки, заморозка ставок, ход партии и выплаты.

Деньги:
- 21 и Crash-дуэль — общая ставка, максимум = меньший из двух балансов
  (округлён вниз до 10, чтобы точный баланс соперника не был виден).
  При старте ставка списывается у обоих сразу — потратить её посреди
  игры нельзя. Победитель получает банк минус 5% комиссии клуба.
- Покер — каждый садится со своим стеком: кто предложил — с выбранной
  суммой, соперник — с той же суммой или со всем, что у него есть, если
  меньше. Больше, чем поставил соперник, выиграть нельзя (лишнее при
  олл-ине возвращается). Комиссия 5% — с банков, где дошло до флопа.

Все изменения идут под одним замком: один процесс сервера, гостей немного,
зато два одновременных нажатия не испортят партию.
"""
import json
import os
import random
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import func, or_

from . import battle_games as G
from .bet_games import card_info, CRASH_K
from .models import Battle, BattleMatch, Client, GamePlay, normalize_phone

MIN_STAKE = int(os.getenv("BATTLE_MIN_STAKE", "20"))
INVITES_PER_DAY = int(os.getenv("BATTLE_INVITES_PER_DAY", "10"))
INVITE_TTL = timedelta(hours=24)
GAMES = {"blackjack": "21", "crash": "Crash-дуэль", "poker": "Покер"}

LOCK = threading.Lock()
_rng = random.SystemRandom()
_invite_attempts: dict[int, list[float]] = {}


class BattleError(Exception):
    pass


# ---------- вспомогательное ----------

def floor10(x: int) -> int:
    return max(0, int(x) // 10 * 10)


def balance(db, client_id: int) -> int:
    return db.query(func.coalesce(Client.balance, 0)).filter(Client.id == client_id).scalar() or 0


def first_name(c: Client | None) -> str:
    return (c.tg_name or "Гость") if c else "Гость"


def mask_phone(norm: str) -> str:
    return f"+7 {norm[:3]} ***-**-{norm[-2:]}" if norm and len(norm) == 10 else "номер"


def player_index(battle: Battle, client: Client) -> int:
    if battle.inviter_id == client.id:
        return 0
    if battle.invitee_id == client.id:
        return 1
    raise BattleError("Это не твой батл")


def get_battle(db, battle_id: int, client: Client) -> tuple[Battle, int]:
    b = db.get(Battle, battle_id)
    if b is None:
        raise BattleError("Батл не найден")
    return b, player_index(b, client)


def opponent_of(db, b: Battle, idx: int) -> Client | None:
    oid = b.invitee_id if idx == 0 else b.inviter_id
    return db.get(Client, oid) if oid else None


def max_stake(db, b: Battle) -> int:
    if not b.invitee_id:
        return 0
    return floor10(min(balance(db, b.inviter_id), balance(db, b.invitee_id)))


def _take(db, client_id: int, amount: int) -> bool:
    updated = db.query(Client).filter(Client.id == client_id, func.coalesce(Client.balance, 0) >= amount).update(
        {Client.balance: func.coalesce(Client.balance, 0) - amount}, synchronize_session=False)
    return updated == 1


def _give(db, client_id: int, amount: int):
    if amount:
        db.query(Client).filter(Client.id == client_id).update(
            {Client.balance: func.coalesce(Client.balance, 0) + amount}, synchronize_session=False)


def _load(m: BattleMatch) -> dict:
    return json.loads(m.state)


def _save(m: BattleMatch, s: dict):
    m.state = json.dumps(s)


# ---------- вызов ----------

def invite(db, me: Client, phone: str):
    """Создаёт вызов. Ответ гостю одинаковый, есть номер в боте или нет, —
    чтобы через батлы нельзя было проверять, кто из знакомых ходит в клуб.
    Возвращает (батл, соперник или None, новый ли вызов)."""
    norm = normalize_phone(phone)
    if len(norm) != 10:
        raise BattleError("Введи номер телефона — 10 цифр после +7")
    if me.phone_normalized == norm:
        raise BattleError("Себя вызвать нельзя 🙂")
    now = time.time()
    attempts = [t for t in _invite_attempts.get(me.id, []) if now - t < 86400]
    if len(attempts) >= INVITES_PER_DAY:
        raise BattleError(f"Не больше {INVITES_PER_DAY} вызовов в сутки — попробуй завтра")
    attempts.append(now)
    _invite_attempts[me.id] = attempts

    # Повторный вызов на тот же номер — тот же ответ, есть номер в боте или нет.
    same = db.query(Battle).filter(Battle.inviter_id == me.id, Battle.invitee_phone == norm,
                                   Battle.status == "pending").first()
    if same is not None:
        _expire(same)
        if same.status == "pending":
            return same, None, False
    target = db.query(Client).filter(Client.phone_normalized == norm, Client.phone_verified == True,  # noqa: E712
                                     Client.consent_at.isnot(None)).first()
    if target is not None:
        existing = db.query(Battle).filter(
            Battle.status.in_(("pending", "active")),
            or_((Battle.inviter_id == me.id) & (Battle.invitee_id == target.id),
                (Battle.inviter_id == target.id) & (Battle.invitee_id == me.id))).first()
        if existing:
            return existing, target, False
    b = Battle(inviter_id=me.id, invitee_id=target.id if target else None, invitee_phone=norm,
               status="pending", created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(b)
    db.commit()
    db.refresh(b)
    return b, target, True


def respond(db, me: Client, battle_id: int, accept: bool) -> Battle:
    b = db.get(Battle, battle_id)
    if b is None or b.invitee_id != me.id:
        raise BattleError("Вызов не найден")
    _expire(b)
    if b.status != "pending":
        raise BattleError("Этот вызов уже неактуален")
    b.status = "active" if accept else "declined"
    b.updated_at = datetime.utcnow()
    db.commit()
    return b


def _expire(b: Battle):
    if b.status == "pending" and datetime.utcnow() - b.created_at > INVITE_TTL:
        b.status = "expired"


# ---------- предложение игры ----------

def make_offer(db, me: Client, battle_id: int, game: str, stake: int) -> Battle:
    b, idx = get_battle(db, battle_id, me)
    if b.status != "active":
        raise BattleError("Батл не активен")
    if b.match_id:
        m = db.get(BattleMatch, b.match_id)
        if m and m.status == "running":
            raise BattleError("Сначала доиграйте текущую партию")
    if game not in GAMES:
        raise BattleError("Выбери игру")
    stake = int(stake or 0)
    if stake < MIN_STAKE:
        raise BattleError(f"Минимальная ставка — {MIN_STAKE} бонусов")
    if game == "poker":
        if stake > balance(db, me.id):
            raise BattleError("У тебя столько нет на балансе")
    else:
        limit = max_stake(db, b)
        if stake > limit:
            raise BattleError(f"Максимальная ставка в этом батле — {limit} бонусов"
                              if limit >= MIN_STAKE else "У кого-то из вас меньше минимальной ставки")
    changed = (b.offer_game, b.offer_stake, b.offer_by) != (game, stake, me.id)
    b.offer_game, b.offer_stake, b.offer_by = game, stake, me.id
    b.updated_at = datetime.utcnow()
    db.commit()
    return b, changed


def respond_offer(db, me: Client, battle_id: int, accept: bool, game: str | None = None,
                  stake: int | None = None) -> Battle:
    b, idx = get_battle(db, battle_id, me)
    if not b.offer_game:
        raise BattleError("Предложения нет")
    if accept and b.offer_by != me.id and (b.offer_game != game or b.offer_stake != stake):
        # Защита от подмены: соперник поменял предложение, пока гость нажимал «Играем».
        raise BattleError("Предложение изменилось — посмотри новое и реши ещё раз")
    if b.offer_by == me.id:
        if accept:
            raise BattleError("Ждём ответ соперника")
        _clear_offer(b)                     # отозвать своё предложение
        db.commit()
        return b
    if not accept:
        _clear_offer(b)
        db.commit()
        return b
    _start_match(db, b)
    return b


def _clear_offer(b: Battle):
    b.offer_game = b.offer_stake = b.offer_by = None
    b.updated_at = datetime.utcnow()


def _start_match(db, b: Battle):
    game, stake = b.offer_game, b.offer_stake
    proposer = 0 if b.offer_by == b.inviter_id else 1
    ids = [b.inviter_id, b.invitee_id]
    if game == "poker":
        stakes = [0, 0]
        stakes[proposer] = stake
        opp_balance = balance(db, ids[1 - proposer])
        stakes[1 - proposer] = stake if opp_balance >= stake else floor10(opp_balance)
        if stakes[1 - proposer] < MIN_STAKE:
            raise BattleError(f"Чтобы сесть за стол, нужно минимум {MIN_STAKE} бонусов")
    else:
        stakes = [stake, stake]
    # Замораживаем ставки обоих одним действием: если у кого-то не хватает — не списываем ни у кого.
    if not _take(db, ids[0], stakes[0]) or not _take(db, ids[1], stakes[1]):
        db.rollback()
        raise BattleError("Баланс изменился — у кого-то из вас уже не хватает бонусов. Предложите ставку поменьше")
    now = time.time()
    if game == "blackjack":
        state = G.bj_new(_rng, now)
    elif game == "crash":
        state = G.cd_new(_rng, now)
    else:
        state = G.pk_new(stakes, _rng, now)
    m = BattleMatch(battle_id=b.id, game=game, p0_id=ids[0], p1_id=ids[1], stakes=json.dumps(stakes),
                    state=json.dumps(state), status="running", created_at=datetime.utcnow())
    db.add(m)
    db.flush()
    b.match_id = m.id
    _clear_offer(b)
    db.commit()
    _maybe_finish(db, m, state)


# ---------- ход партии ----------

def _engine_finished(game: str, s: dict) -> bool:
    return s["over"] if game == "poker" else s["finished"]


def tick(db, m: BattleMatch) -> bool:
    """Продвигает партию по времени (таймауты, старт новой раздачи, конец краша)."""
    if m.status != "running":
        return False
    s = _load(m)
    now = time.time()
    changed = False
    if m.game == "blackjack":
        changed = G.bj_tick(s, now)
    elif m.game == "crash":
        changed = G.cd_tick(s, now)
    else:
        for _ in range(10):                    # несколько таймаутов подряд, если долго никто не заходил
            if not G.pk_tick(s, _rng, now):
                break
            changed = True
    if changed:
        _save(m, s)
        db.commit()
    _maybe_finish(db, m, s)
    return changed


def _maybe_finish(db, m: BattleMatch, s: dict):
    if m.status != "running" or not _engine_finished(m.game, s):
        return
    stakes = json.loads(m.stakes)
    if m.game == "poker":
        pay, rake, winner = list(s["stacks"]), s["rake_total"], s["winner"]
    else:
        winner = s.get("winner")
        pay, rake = G.fixed_stake_payout(stakes[0], winner)
    updated = db.query(BattleMatch).filter(BattleMatch.id == m.id, BattleMatch.status == "running").update(
        {BattleMatch.status: "finished", BattleMatch.winner_idx: winner, BattleMatch.payouts: json.dumps(pay),
         BattleMatch.rake: rake, BattleMatch.finished_at: datetime.utcnow(), BattleMatch.state: json.dumps(s)},
        synchronize_session=False)
    if updated == 1:
        ids = [m.p0_id, m.p1_id]
        for i in (0, 1):
            _give(db, ids[i], pay[i])
            tg_id = db.query(Client.tg_id).filter(Client.id == ids[i]).scalar()
            if tg_id:
                db.add(GamePlay(tg_id=tg_id, game="battle_" + m.game, mode="bet", stake=stakes[i], payout=pay[i]))
    db.commit()
    db.refresh(m)


def action(db, me: Client, battle_id: int, act: str, amount=None):
    b, idx = get_battle(db, battle_id, me)
    m = db.get(BattleMatch, b.match_id) if b.match_id else None
    if m is None or m.status != "running":
        raise BattleError("Партия не идёт")
    tick(db, m)
    if m.status != "running":
        return
    s = _load(m)
    now = time.time()
    try:
        if m.game == "blackjack":
            G.bj_action(s, idx, act, now)
        elif m.game == "crash":
            if act != "cashout":
                raise ValueError("Неизвестное действие")
            G.cd_cashout(s, idx, now)
        else:
            G.pk_action(s, idx, act, amount, _rng, now)
    except ValueError as e:
        raise BattleError(str(e))
    _save(m, s)
    db.commit()
    _maybe_finish(db, m, s)


def leave(db, me: Client, battle_id: int) -> Battle:
    """Выйти из батла. Покер посреди раздачи — это сброс карт; 21 — «хватит».
    Crash-раунд длится секунды — его нужно доиграть."""
    b, idx = get_battle(db, battle_id, me)
    m = db.get(BattleMatch, b.match_id) if b.match_id else None
    if m and m.status == "running":
        tick(db, m)
    if m and m.status == "running":
        s = _load(m)
        now = time.time()
        if m.game == "crash":
            raise BattleError("Дождись конца раунда — это пара секунд")
        if m.game == "poker":
            G.pk_leave(s, idx, _rng, now)
        elif not s["done"][idx]:
            G.bj_action(s, idx, "stand", now)
        _save(m, s)
        db.commit()
        _maybe_finish(db, m, s)
    changed = b.status in ("pending", "active")
    if changed:
        b.status = "closed"
        _clear_offer(b)
        db.commit()
    return b, changed


def sweep(db) -> int:
    """Фоновая проверка: досчитывает партии, где игроки пропали (таймауты, конец краша)."""
    n = 0
    for m in db.query(BattleMatch).filter(BattleMatch.status == "running").all():
        try:
            tick(db, m)
            n += 1
        except Exception:  # noqa: BLE001 — одна сломанная партия не должна останавливать остальные
            db.rollback()
            import logging
            logging.getLogger("colizeum").exception("Батл: ошибка в партии %s", m.id)
    return n


# ---------- что показать гостю ----------

def _cards(lst):
    return [card_info(c) for c in lst]


def match_view(m: BattleMatch, idx: int) -> dict:
    s = _load(m)
    now = time.time()
    opp = 1 - idx
    stakes = json.loads(m.stakes)
    v = {"id": m.id, "game": m.game, "game_name": GAMES[m.game], "status": m.status,
         "stake_me": stakes[idx], "stake_opp": stakes[opp]}
    if m.status == "finished":
        pay = json.loads(m.payouts or "[0,0]")
        v["result"] = {"winner": None if m.winner_idx is None else ("me" if m.winner_idx == idx else "opp"),
                       "payout_me": pay[idx], "net_me": pay[idx] - stakes[idx], "rake": m.rake or 0}
    if m.game == "blackjack":
        v.update(my_cards=_cards(s["hands"][idx]), my_total=G.bj_value(s["hands"][idx]), my_done=s["done"][idx],
                 opp_done=s["done"][opp],
                 deadline_in=max(0, round(s["deadline"][idx] - now)))
        if s["finished"]:
            v.update(opp_cards=_cards(s["hands"][opp]), opp_total=G.bj_value(s["hands"][opp]))
    elif m.game == "crash":
        v.update(k=CRASH_K, max_mult=G.CRASH_DUEL_MAX, starts_in=max(0.0, s["start_at"] - now),
                 elapsed=max(0.0, now - s["start_at"]), my_cash=s["cash"][idx])
        if s["finished"]:
            v.update(crash_point=min(s["crash_point"], G.CRASH_DUEL_MAX),
                     capped=s["crash_point"] > G.CRASH_DUEL_MAX, opp_cash=s["cash"][opp])
    else:
        legal = G.pk_legal(s, idx)
        v.update(hand_no=s["hand_no"], sb=s["sb"], bb=s["bb"], button_me=s["button"] == idx,
                 stacks=[s["stacks"][idx], s["stacks"][opp]], bets=[s["bets"][idx], s["bets"][opp]],
                 pot=sum(s["committed"]), street=s["street"], board=_cards(s["board"]),
                 my_hole=_cards(s["hole"][idx]), my_turn=legal is not None, legal=legal,
                 deadline_in=max(0, round(s["deadline"] - now)) if s["street"] != "done" else None,
                 next_hand_in=max(0, round(s["next_hand_at"] - now, 1)) if s["street"] == "done" else None,
                 over=s["over"], left=None if s["left"] is None else ("me" if s["left"] == idx else "opp"),
                 log=[{"who": "me" if w == idx else "opp", "act": a, "amount": x} for w, a, x in s["log"][-6:]])
        last = s.get("last")
        if last:
            lv = {"hand_no": last["hand_no"], "reason": last["reason"], "pot": last["pot"], "rake": last["rake"],
                  "board": _cards(last["board"]),
                  "winner": "split" if len(last["winners"]) == 2 else ("me" if last["winners"][0] == idx else "opp")}
            if last["hands"]:
                lv.update(my_hand={"cards": _cards(last["hands"][idx]["cards"]), "name": last["hands"][idx]["name"]},
                          opp_hand={"cards": _cards(last["hands"][opp]["cards"]), "name": last["hands"][opp]["name"]})
            v["last"] = lv
    return v


def battle_view(db, b: Battle, me: Client) -> dict:
    idx = player_index(b, me)
    _expire(b)
    opp = opponent_of(db, b, idx)
    v = {"id": b.id, "status": b.status, "is_inviter": idx == 0,
         "opponent": first_name(opp) if (opp and (b.status != "pending" or idx == 1)) else mask_phone(b.invitee_phone),
         "min_stake": MIN_STAKE, "max_stake": max_stake(db, b) if b.status == "active" else 0,
         "my_balance": balance(db, me.id), "games": GAMES,
         "offer": None, "match": None}
    if b.offer_game:
        v["offer"] = {"game": b.offer_game, "game_name": GAMES[b.offer_game], "stake": b.offer_stake,
                      "by_me": b.offer_by == me.id}
    if b.match_id:
        m = db.get(BattleMatch, b.match_id)
        if m:
            v["match"] = match_view(m, idx)
    return v


def list_view(db, me: Client) -> dict:
    rows = db.query(Battle).filter(or_(Battle.inviter_id == me.id, Battle.invitee_id == me.id),
                                   Battle.status.in_(("pending", "active"))).order_by(Battle.updated_at.desc()).all()
    incoming, outgoing, active = [], [], []
    changed = False
    for b in rows:
        before = b.status
        _expire(b)
        changed |= before != b.status
        if b.status == "pending":
            if b.invitee_id == me.id:
                incoming.append({"id": b.id, "name": first_name(db.get(Client, b.inviter_id))})
            else:
                outgoing.append({"id": b.id, "name": mask_phone(b.invitee_phone)})
        elif b.status == "active":
            idx = player_index(b, me)
            m = db.get(BattleMatch, b.match_id) if b.match_id else None
            waiting_me = bool(b.offer_game and b.offer_by != me.id)
            if m and m.status == "running":
                tick(db, m)
                if m.status == "running" and m.game == "poker":
                    waiting_me = waiting_me or G.pk_legal(_load(m), idx) is not None
                elif m.status == "running" and m.game == "blackjack":
                    waiting_me = waiting_me or not _load(m)["done"][idx]
            active.append({"id": b.id, "name": first_name(opponent_of(db, b, idx)),
                           "playing": GAMES[m.game] if (m and m.status == "running") else None,
                           "your_move": waiting_me})
    if changed:
        db.commit()
    return {"incoming": incoming, "outgoing": outgoing, "active": active, "min_stake": MIN_STAKE}

"""
Игры для батлов «гость против гостя»: 21, Crash-дуэль, Техасский холдем.

Здесь только правила — без базы и без веб-сервера, чтобы их можно было
проверить симуляцией. Состояние партии — обычный словарь (хранится в базе
как JSON). Время — секунды (time.time()), передаётся снаружи.

Деньги: в 21 и Crash-дуэли ставка общая, банк = 2 × ставка, победитель
получает банк минус комиссия клуба. Ничья — ставки возвращаются без комиссии.
В покере каждый садится со своим стеком; комиссия берётся с каждого банка,
в котором дошло до флопа («нет флопа — нет комиссии»).
"""
import itertools
import random

from .bet_games import crash_draw_point, crash_outcome, crash_time_of

RAKE_PERCENT = 5
ACTION_SECONDS = 45          # на ход в покере и в 21
CRASH_COUNTDOWN = 3          # обратный отсчёт перед стартом Crash-дуэли
CRASH_DUEL_MAX = 10.0        # потолок коэффициента в дуэли
NEXT_HAND_PAUSE = 4          # пауза между раздачами покера, чтобы увидеть итог


def rake_of(pot: int) -> int:
    return pot * RAKE_PERCENT // 100


def card_rank(card: int) -> int:
    return card // 4 + 2


def new_deck(rng) -> list[int]:
    deck = list(range(52))
    rng.shuffle(deck)
    return deck


def fixed_stake_payout(stake: int, winner):
    """Выплаты [игроку 0, игроку 1] и комиссия для игр с общей ставкой."""
    if winner is None:
        return [stake, stake], 0
    pot = stake * 2
    rake = rake_of(pot)
    pay = [0, 0]
    pay[winner] = pot - rake
    return pay, rake


# =====================================================================
# 21
# =====================================================================

def bj_value(cards: list[int]) -> int:
    total, aces = 0, 0
    for c in cards:
        r = card_rank(c)
        if r == 14:
            total += 11
            aces += 1
        else:
            total += min(r, 10)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def bj_new(rng, now: float) -> dict:
    deck = new_deck(rng)
    hands = [[deck.pop(), deck.pop()], [deck.pop(), deck.pop()]]
    s = {"deck": deck, "hands": hands, "done": [False, False],
         "deadline": [now + ACTION_SECONDS] * 2, "finished": False}
    for p in (0, 1):
        if bj_value(hands[p]) >= 21:
            s["done"][p] = True
    _bj_check_finish(s)
    return s


def bj_action(s: dict, p: int, action: str, now: float):
    if s["finished"] or s["done"][p]:
        raise ValueError("Ты уже закончил — ждём соперника")
    if action == "hit":
        s["hands"][p].append(s["deck"].pop())
        if bj_value(s["hands"][p]) >= 21:
            s["done"][p] = True
    elif action == "stand":
        s["done"][p] = True
    else:
        raise ValueError("Неизвестное действие")
    s["deadline"][p] = now + ACTION_SECONDS
    _bj_check_finish(s)


def bj_tick(s: dict, now: float) -> bool:
    """Автоматически «хватит» тем, кто не ходит дольше ACTION_SECONDS."""
    changed = False
    for p in (0, 1):
        if not s["done"][p] and now >= s["deadline"][p]:
            s["done"][p] = True
            changed = True
    if changed:
        _bj_check_finish(s)
    return changed


def _bj_check_finish(s: dict):
    if all(s["done"]):
        s["finished"] = True
        v = [bj_value(h) for h in s["hands"]]
        bust = [x > 21 for x in v]
        if bust[0] and bust[1]:
            s["winner"] = None
        elif bust[0] or bust[1]:
            s["winner"] = 1 if bust[0] else 0
        elif v[0] == v[1]:
            s["winner"] = None
        else:
            s["winner"] = 0 if v[0] > v[1] else 1


# =====================================================================
# Crash-дуэль
# =====================================================================
# Одна кривая на двоих. Каждый жмёт «Забрать» — когда соперник забрал,
# не видно до конца раунда (иначе выгодно всегда ждать второго).
# Побеждает тот, кто забрал на большем коэффициенте до краха.

def cd_new(rng, now: float) -> dict:
    return {"crash_point": crash_draw_point(0, CRASH_DUEL_MAX, rng),
            "start_at": now + CRASH_COUNTDOWN, "cash": [None, None], "finished": False}


def cd_end_time(s: dict) -> float:
    return s["start_at"] + crash_time_of(min(s["crash_point"], CRASH_DUEL_MAX))


def cd_cashout(s: dict, p: int, now: float):
    if s["finished"]:
        raise ValueError("Раунд уже закончился")
    if now < s["start_at"]:
        raise ValueError("Раунд ещё не начался")
    if s["cash"][p] is not None:
        return
    status, mult = crash_outcome(s["crash_point"], None, CRASH_DUEL_MAX, now - s["start_at"], True)
    if status == "cashed":
        s["cash"][p] = mult


def cd_tick(s: dict, now: float) -> bool:
    if s["finished"] or now < cd_end_time(s):
        return False
    capped = s["crash_point"] > CRASH_DUEL_MAX
    vals = []
    for p in (0, 1):
        v = s["cash"][p]
        if v is None and capped:
            v = CRASH_DUEL_MAX                  # дожил до потолка — забрал автоматически
            s["cash"][p] = v
        vals.append(v or 0)
    s["finished"] = True
    s["winner"] = None if vals[0] == vals[1] else (0 if vals[0] > vals[1] else 1)
    return True


# =====================================================================
# Техасский холдем один на один
# =====================================================================

HAND_NAMES = ["Старшая карта", "Пара", "Две пары", "Сет", "Стрит", "Флеш",
              "Фулл-хаус", "Каре", "Стрит-флеш"]


def _rank5(cards) -> tuple:
    ranks = sorted((card_rank(c) for c in cards), reverse=True)
    suits = [c % 4 for c in cards]
    flush = len(set(suits)) == 1
    uniq = sorted(set(ranks), reverse=True)
    straight_high = 0
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            straight_high = uniq[0]
        elif uniq == [14, 5, 4, 3, 2]:
            straight_high = 5                      # «колесо» A-2-3-4-5
    counts = sorted(((ranks.count(r), r) for r in uniq), reverse=True)
    if straight_high and flush:
        return (8, straight_high)
    if counts[0][0] == 4:
        return (7, counts[0][1], counts[1][1])
    if counts[0][0] == 3 and counts[1][0] == 2:
        return (6, counts[0][1], counts[1][1])
    if flush:
        return (5, *ranks)
    if straight_high:
        return (4, straight_high)
    if counts[0][0] == 3:
        return (3, counts[0][1], *[r for c, r in counts[1:]])
    if counts[0][0] == 2 and counts[1][0] == 2:
        return (2, counts[0][1], counts[1][1], counts[2][1])
    if counts[0][0] == 2:
        return (1, counts[0][1], *[r for c, r in counts[1:]])
    return (0, *ranks)


def best_hand(cards7) -> tuple:
    """Лучшая пятёрка из 7 карт: (оценка, пять карт)."""
    best = None
    for combo in itertools.combinations(cards7, 5):
        r = _rank5(combo)
        if best is None or r > best[0]:
            best = (r, list(combo))
    return best


def hand_name(rank: tuple) -> str:
    if rank[0] == 8 and rank[1] == 14:
        return "Роял-флеш"
    return HAND_NAMES[rank[0]]


def pk_new(stacks: list[int], rng, now: float) -> dict:
    eff = min(stacks)
    bb = max(2, (eff // 20) // 2 * 2)          # ~20 больших блайндов на меньший стек
    s = {"stacks": list(stacks), "buyins": list(stacks), "bb": bb, "sb": bb // 2,
         "button": rng.randrange(2), "hand_no": 0, "timeouts": [0, 0], "over": False,
         "left": None, "winner": None, "rake_total": 0, "last": None, "log": [],
         "street": "done", "next_hand_at": now}
    pk_start_hand(s, rng, now)
    return s


def _put(s, p, amount):
    amount = max(0, min(amount, s["stacks"][p]))
    s["stacks"][p] -= amount
    s["bets"][p] += amount
    s["committed"][p] += amount
    return amount


def pk_start_hand(s: dict, rng, now: float):
    s["hand_no"] += 1
    s["button"] = 1 - s["button"] if s["hand_no"] > 1 else s["button"]
    deck = new_deck(rng)
    s["hole"] = [[deck.pop(), deck.pop()], [deck.pop(), deck.pop()]]
    s["deck"] = deck
    s["board"] = []
    s["street"] = "preflop"
    s["bets"] = [0, 0]
    s["committed"] = [0, 0]
    s["acted"] = [False, False]
    s["log"] = []
    btn, other = s["button"], 1 - s["button"]
    _put(s, btn, s["sb"])                       # один на один: баттон ставит малый блайнд
    _put(s, other, s["bb"])
    s["current_bet"] = max(s["bets"])
    s["min_raise"] = s["bb"]
    s["to_act"] = btn                           # префлоп первым ходит баттон
    s["deadline"] = now + ACTION_SECONDS
    _pk_settle(s, rng, now)


def pk_legal(s: dict, p: int) -> dict | None:
    if s["over"] or s["street"] not in ("preflop", "flop", "turn", "river") or s["to_act"] != p:
        return None
    opp = 1 - p
    to_call = s["current_bet"] - s["bets"][p]
    stack = s["stacks"][p]
    legal = {"fold": to_call > 0, "check": to_call == 0, "call": min(to_call, stack) if to_call > 0 else 0,
             "raise": False}
    # Ставить больше, чем соперник может уравнять, бессмысленно — потолок по меньшему стеку.
    max_to = min(s["bets"][p] + stack, s["bets"][opp] + s["stacks"][opp])
    if stack > to_call and s["stacks"][opp] > 0 and max_to > s["current_bet"]:
        base = s["current_bet"] if s["current_bet"] > 0 else 0
        min_to = base + max(s["min_raise"], s["bb"]) if s["current_bet"] > 0 else s["bb"]
        legal.update({"raise": True, "min_to": min(min_to, max_to), "max_to": max_to})
    return legal


def pk_action(s: dict, p: int, action: str, amount: int | None, rng, now: float):
    legal = pk_legal(s, p)
    if legal is None:
        raise ValueError("Сейчас не твой ход")
    opp = 1 - p
    if action == "fold":
        _pk_fold(s, p, rng, now)
        s["timeouts"][p] = 0
        return
    if action == "check":
        if not legal["check"]:
            raise ValueError("Нельзя пропустить — нужно уравнять или сбросить")
        s["log"].append([p, "check", 0])
    elif action == "call":
        if legal["check"]:
            raise ValueError("Уравнивать нечего — можно пропустить")
        put = _put(s, p, legal["call"])
        s["log"].append([p, "allin" if s["stacks"][p] == 0 else "call", put])
    elif action in ("raise", "allin"):
        if not legal["raise"]:
            if action == "allin" and not legal["check"]:
                put = _put(s, p, legal["call"])
                s["log"].append([p, "allin" if s["stacks"][p] == 0 else "call", put])
                s["acted"][p] = True
                s["to_act"] = opp
                s["timeouts"][p] = 0
                _pk_settle(s, rng, now)
                return
            raise ValueError("Повысить сейчас нельзя")
        target = legal["max_to"] if action == "allin" else int(amount or 0)
        if not (legal["min_to"] <= target <= legal["max_to"]):
            raise ValueError(f"Ставка — от {legal['min_to']} до {legal['max_to']}")
        raise_size = target - s["current_bet"]
        _put(s, p, target - s["bets"][p])
        if raise_size >= s["min_raise"]:
            s["min_raise"] = raise_size
        s["current_bet"] = target
        s["acted"] = [False, False]
        s["log"].append([p, "allin" if s["stacks"][p] == 0 else ("bet" if raise_size == target else "raise"), target])
    else:
        raise ValueError("Неизвестное действие")
    s["acted"][p] = True
    s["to_act"] = opp
    s["timeouts"][p] = 0
    _pk_settle(s, rng, now)


def _pk_round_complete(s) -> bool:
    for i in (0, 1):
        if not (s["acted"][i] or s["stacks"][i] == 0):
            return False
    if s["bets"][0] == s["bets"][1]:
        return True
    low = 0 if s["bets"][0] < s["bets"][1] else 1
    return s["stacks"][low] == 0                 # у кого меньше — тот в олл-ине


def _pk_return_uncalled(s):
    hi = 0 if s["bets"][0] > s["bets"][1] else 1
    diff = s["bets"][hi] - s["bets"][1 - hi]
    if diff > 0:
        s["stacks"][hi] += diff
        s["bets"][hi] -= diff
        s["committed"][hi] -= diff


def _pk_settle(s, rng, now):
    """После каждого действия: закончен ли круг торговли, раздать следующую улицу, вскрытие."""
    while True:
        if not _pk_round_complete(s):
            if s["stacks"][s["to_act"]] == 0 or s["acted"][s["to_act"]] and s["bets"][s["to_act"]] == s["current_bet"]:
                s["to_act"] = 1 - s["to_act"]
            s["deadline"] = now + ACTION_SECONDS
            return
        _pk_return_uncalled(s)
        all_in = s["stacks"][0] == 0 or s["stacks"][1] == 0
        if s["street"] == "river" or (all_in and len(s["board"]) == 5):
            _pk_showdown(s, now)
            return
        # следующая улица
        need = 3 if s["street"] == "preflop" else 1
        s["board"] += [s["deck"].pop() for _ in range(need)]
        s["street"] = {"preflop": "flop", "flop": "turn", "turn": "river"}[s["street"]]
        s["bets"] = [0, 0]
        s["current_bet"] = 0
        s["min_raise"] = s["bb"]
        s["acted"] = [False, False]
        s["to_act"] = 1 - s["button"]            # после флопа первым ходит большой блайнд
        if all_in:
            for i in (0, 1):
                s["acted"][i] = True             # торговли больше нет — открываем стол до конца
        # цикл продолжится: если торговать нечем — раздаём дальше


def _pk_end(s, now, winners: list[int], reason: str, hands=None):
    pot = s["committed"][0] + s["committed"][1]
    rake = rake_of(pot) if len(s["board"]) >= 3 else 0
    net = pot - rake
    if len(winners) == 1:
        s["stacks"][winners[0]] += net
    else:
        half = net // 2
        s["stacks"][0] += half
        s["stacks"][1] += half
        s["stacks"][1 - s["button"]] += net - 2 * half     # лишняя фишка — большому блайнду
    s["rake_total"] += rake
    s["last"] = {"hand_no": s["hand_no"], "winners": winners, "reason": reason, "pot": pot, "rake": rake,
                 "board": list(s["board"]), "hands": hands}
    s["street"] = "done"
    s["to_act"] = None
    s["next_hand_at"] = now + NEXT_HAND_PAUSE
    if s["stacks"][0] == 0 or s["stacks"][1] == 0:
        s["over"] = True
        s["winner"] = 0 if s["stacks"][0] > 0 else 1


def _pk_fold(s, p, rng, now):
    s["log"].append([p, "fold", 0])
    _pk_return_uncalled(s)
    _pk_end(s, now, [1 - p], "fold")


def _pk_showdown(s, now):
    ranks = [best_hand(s["hole"][i] + s["board"])[0] for i in (0, 1)]
    winners = [0] if ranks[0] > ranks[1] else [1] if ranks[1] > ranks[0] else [0, 1]
    hands = [{"cards": s["hole"][i], "name": hand_name(ranks[i])} for i in (0, 1)]
    _pk_end(s, now, winners, "showdown", hands)


def pk_tick(s: dict, rng, now: float) -> bool:
    """Ход по времени: авто-чек или авто-фолд, новая раздача после паузы."""
    if s["over"]:
        return False
    if s["street"] == "done":
        if now >= s["next_hand_at"]:
            pk_start_hand(s, rng, now)
            return True
        return False
    if now < s["deadline"]:
        return False
    p = s["to_act"]
    s["timeouts"][p] += 1
    legal = pk_legal(s, p)
    if legal and legal["check"]:
        s["log"].append([p, "check", 0])
        s["acted"][p] = True
        s["to_act"] = 1 - p
        _pk_settle(s, rng, now)
    else:
        _pk_fold(s, p, rng, now)
    if s["timeouts"][p] >= 2 and not s["over"]:
        pk_leave(s, p, rng, now)               # два пропуска подряд — гость ушёл из-за стола
    return True


def pk_leave(s: dict, p: int, rng, now: float):
    """Встать из-за стола. Посреди раздачи — это сброс карт."""
    if s["over"]:
        return
    if s["street"] in ("preflop", "flop", "turn", "river"):
        _pk_fold(s, p, rng, now)
    s["over"] = True
    s["left"] = p
    if s["winner"] is None:
        s["winner"] = 1 - p if s["stacks"][1 - p] > s["buyins"][1 - p] else None

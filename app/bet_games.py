"""
Математика игр «только на бонусы»: Crash, Слот, Больше-меньше.

Всё считается на сервере — приложение только показывает результат.
Во всех трёх играх гость в среднем возвращает себе около 95% поставленного
(5% — преимущество клуба). Это значит: на длинной дистанции бонусы
постепенно «сгорают», а не копятся, и при этом игра честная — шансы
и коэффициенты гость видит заранее.

Crash
-----
Коэффициент растёт от ×1.00 по формуле m(t) = e^(K·t). Заранее, в момент
ставки, сервер тайно определяет точку краха. Шанс, что краш случится
ПОЗЖЕ коэффициента x, равен (1 − комиссия) / x. При комиссии 5%:
  • ~5% раундов крашатся сразу на ×1.00;
  • ~52% — не доходят до ×2;
  • ~81% — не доходят до ×5;
  • до ×10 (потолок) доживает ~9.5% раундов — на ×10 ставка забирается автоматически.
Какую бы стратегию гость ни выбрал (забирать на ×1.2 или ждать ×10),
в среднем он получает назад ~95% ставок.

Слот
----
Три барабана, на каждом пять фигур с разной частотой. Платит лучшая
комбинация из выпавших. Таблица и частоты подобраны точным перебором всех
22³ = 10 648 вариантов: возврат 94.96%, что-то возвращается в ~58%
прокрутов, выигрыш больше ставки — в ~27%.

Больше-меньше
-------------
Колода 52 карты (2…туз). Клуб открывает свою карту (от 3 до короля), гость выбирает
«Больше» или «Меньше» и тянет карту из оставшихся 51. Коэффициент для
каждого варианта = (1 − комиссия) × 51 / число выигрышных карт — чем
рискованнее выбор, тем выше коэффициент. Равная по старшинству карта — проигрыш.
"""
import math
import random

# ---------- общее ----------

def payout_for(stake: int, mult: float) -> int:
    """Выигрыш целыми бонусами (вниз), без ошибок округления float."""
    return int(math.floor(stake * mult + 1e-6))


def floor2(x: float) -> float:
    return math.floor(x * 100 + 1e-9) / 100


# ---------- Crash ----------

CRASH_K = 0.25   # скорость роста: ×2 через ~2.8 с, ×5 через ~6.4 с, ×10 через ~9.2 с


def crash_draw_point(edge_percent: float, max_mult: float, rng=random) -> float:
    """Тайная точка краха раунда. Если выпало выше потолка — возвращаем
    чуть больше потолка: тогда ставка автоматически забирается на ×max."""
    u = rng.random()
    raw = (1 - edge_percent / 100) / (1 - u)
    if raw >= max_mult:
        return round(max_mult + 0.01, 2)
    return max(1.0, floor2(raw))


def crash_mult_at(seconds: float) -> float:
    return floor2(math.exp(CRASH_K * max(0.0, seconds)))


def crash_time_of(mult: float) -> float:
    return math.log(max(1.0, mult)) / CRASH_K


def crash_outcome(crash_point: float, auto_cashout, max_mult: float, elapsed: float, cashout_request: bool):
    """Состояние раунда через elapsed секунд после старта.
    Возвращает (статус, коэффициент): статус — running | cashed | crashed."""
    target = min(auto_cashout or max_mult, max_mult)
    if target < crash_point and elapsed >= crash_time_of(target):
        return "cashed", target                      # сработал автовывод (или потолок)
    if elapsed >= crash_time_of(crash_point):
        return "crashed", crash_point
    if cashout_request:
        m = min(crash_mult_at(elapsed), target)
        if m < crash_point:
            return "cashed", m
        return "crashed", crash_point
    return "running", crash_mult_at(elapsed)


def crash_display_point(crash_point: float, max_mult: float) -> float:
    return min(crash_point, max_mult)


# ---------- Слот ----------

# (символ, вес на барабане, ×за три, ×за две)
SLOT_SYMBOLS = [
    ("star",     2, 20, 4),
    ("diamond",  4, 10, 2),
    ("square",   5,  5, 1.5),
    ("circle",   5,  3, 1),
    ("triangle", 6,  2, 0),
]
SLOT_LABEL3 = {"star": "Три звезды", "diamond": "Три ромба", "square": "Три квадрата",
               "circle": "Три круга", "triangle": "Три треугольника"}
SLOT_LABEL2 = {"star": "Две звезды", "diamond": "Два ромба", "square": "Два квадрата",
               "circle": "Два круга", "triangle": "Два треугольника"}
SLOT_WEIGHTS = {s: w for s, w, _, _ in SLOT_SYMBOLS}
SLOT_THREE = {s: m3 for s, _, m3, _ in SLOT_SYMBOLS}
SLOT_TWO = {s: m2 for s, _, _, m2 in SLOT_SYMBOLS}
SLOT_MAX_MULT = max(SLOT_THREE.values())
SLOT_STAR_SINGLE = 1   # одна звезда — ставка возвращается


def slot_spin(rng=random) -> list[str]:
    symbols = [s for s, *_ in SLOT_SYMBOLS]
    weights = [w for _, w, *_ in SLOT_SYMBOLS]
    return rng.choices(symbols, weights=weights, k=3)


def slot_evaluate(reels: list[str]):
    """Лучшая комбинация: (множитель, подпись, номера выигрышных барабанов)."""
    best = (0.0, "", [])
    counts = {}
    for s in reels:
        counts[s] = counts.get(s, 0) + 1
    for s, c in counts.items():
        positions = [i for i, r in enumerate(reels) if r == s]
        if c == 3 and SLOT_THREE[s] > best[0]:
            best = (float(SLOT_THREE[s]), SLOT_LABEL3[s], positions)
        elif c == 2 and SLOT_TWO[s] > best[0]:
            best = (float(SLOT_TWO[s]), SLOT_LABEL2[s], positions)
    if counts.get("star") == 1 and SLOT_STAR_SINGLE > best[0]:
        best = (float(SLOT_STAR_SINGLE), "Звезда — ставка возвращается", [reels.index("star")])
    return best


def slot_paytable() -> list[dict]:
    rows = []
    for s, _, m3, m2 in SLOT_SYMBOLS:
        rows.append({"symbol": s, "count": 3, "mult": m3})
        if m2:
            rows.append({"symbol": s, "count": 2, "mult": m2})
        if s == "star":
            rows.append({"symbol": s, "count": 1, "mult": SLOT_STAR_SINGLE})
    return rows


def slot_exact_rtp() -> tuple[float, float, float]:
    """Точный возврат, доля прокрутов с выплатой и с выигрышем > ставки."""
    total = sum(SLOT_WEIGHTS.values())
    rtp = hit = win = 0.0
    names = list(SLOT_WEIGHTS)
    for a in names:
        for b in names:
            for c in names:
                p = SLOT_WEIGHTS[a] * SLOT_WEIGHTS[b] * SLOT_WEIGHTS[c] / total ** 3
                m = slot_evaluate([a, b, c])[0]
                rtp += p * m
                hit += p if m > 0 else 0
                win += p if m > 1 else 0
    return rtp, hit, win


# ---------- Больше-меньше ----------

RANK_NAMES = {11: "J", 12: "Q", 13: "K", 14: "A"}
SUITS = ["spades", "hearts", "diamonds", "clubs"]


def card_rank(card: int) -> int:
    return card // 4 + 2          # 2 … 14 (туз)


def card_info(card: int) -> dict:
    r = card_rank(card)
    return {"rank": r, "label": RANK_NAMES.get(r, str(r)), "suit": SUITS[card % 4]}


def hilo_counts(dealer: int) -> tuple[int, int]:
    """Сколько из оставшихся 51 карт старше и младше карты клуба."""
    r = card_rank(dealer)
    return 4 * (14 - r), 4 * (r - 2)


def hilo_coef(winning_cards: int, edge_percent: float):
    if winning_cards <= 0:
        return None
    return floor2((1 - edge_percent / 100) * 51 / winning_cards)


def hilo_coefs(dealer: int, edge_percent: float) -> dict:
    higher, lower = hilo_counts(dealer)
    return {"higher": hilo_coef(higher, edge_percent), "lower": hilo_coef(lower, edge_percent)}


def hilo_max_coef(edge_percent: float) -> float:
    return hilo_coef(4, edge_percent)


def hilo_deal(rng=random) -> int:
    """Карта клуба — от тройки до короля. Двойку и туза клуб не открывает:
    с ними у гостя остался бы только один вариант с коэффициентом ×1.00."""
    return rng.randrange(4, 48)


def hilo_draw(dealer: int, rng=random) -> int:
    card = rng.randrange(51)
    return card if card < dealer else card + 1     # любая карта, кроме карты клуба


def hilo_wins(dealer: int, player: int, choice: str) -> bool:
    if choice == "higher":
        return card_rank(player) > card_rank(dealer)
    return card_rank(player) < card_rank(dealer)

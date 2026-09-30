"""
Математика мини-игр — отдельно от базы и веб-сервера, чтобы её было
легко проверить и поправить.

Ставки в «Дайс» — честные: гость видит реальный шанс, а выпавшее число
определяет исход (число ≤ выбранного шанса — выигрыш). Преимущество
клуба заложено в коэффициент выплаты: за ставку с шансом p гость получает
не 100/p, а (100 − комиссия)/p. При комиссии 5% гость в среднем теряет
5% от каждой ставки при ЛЮБОМ выбранном шансе — поэтому «всегда ставить
на 90%» тоже невыгодно, и клуб в минус не уходит.
"""
import random
from datetime import datetime, timedelta

FREE_WINDOW = timedelta(hours=24)


# ---------- бесплатные попытки ----------

def free_attempts_state(play_times: list[datetime], per_day: int, now: datetime):
    """Сколько бесплатных попыток осталось в скользящем окне 24 часа и когда
    освободится следующая. Возвращает (осталось, момент_следующей | None)."""
    recent = sorted(t for t in play_times if now - t < FREE_WINDOW)
    left = max(0, per_day - len(recent))
    next_at = None
    if left == 0 and per_day > 0 and recent:
        next_at = recent[-per_day] + FREE_WINDOW
    return left, next_at


def format_wait(delta: timedelta) -> str:
    minutes = max(1, int(delta.total_seconds() // 60))
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} ч {mins} мин"
    if hours:
        return f"{hours} ч"
    return f"{mins} мин"


# ---------- «Дайс»: бесплатная попытка ----------

def free_probability_reward(chance: int) -> int:
    """Приз за бесплатную попытку: чем ниже шанс, тем больше бонусов."""
    return max(10, round(((100 - chance) * 1.2) / 10) * 10)


def roll_free_probability(chance: int, house_factor: float):
    """Бесплатная попытка (без ставки) — логика прежняя."""
    win = random.random() < (chance / 100) * house_factor
    rolled = random.randint(1, chance) if win else random.randint(chance + 1, 100)
    return rolled, win


# ---------- «Дайс»: ставка бонусами ----------

def bet_payout(stake: int, chance: int, house_edge_percent: int) -> int:
    """Сколько бонусов гость получит при выигрыше (включая саму ставку).
    Считается целыми числами, чтобы не было ошибок округления."""
    return stake * (100 - house_edge_percent) // chance


def bet_multiplier(chance: int, house_edge_percent: int) -> float:
    return (100 - house_edge_percent) / chance


def roll_bet(chance: int):
    """Честный бросок: число от 1 до 100, выигрыш если оно ≤ шанса."""
    rolled = random.randint(1, 100)
    return rolled, rolled <= chance

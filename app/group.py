"""
Автопосты в группу клуба (сарафанное радио).

Что публикуется (только никнеймы — без имён и телефонов):
  • крупные выигрыши в играх — не чаще раза в BIG_WIN_GAP_HOURS часа;
  • по понедельникам в 12:00 (МСК) — топ-5 рейтинга месяца и самые дружелюбные;
  • итоги месяца — победители рейтингов.

Включается переменной CLUB_GROUP_ID в Railway → Variables: ID группы или
канала клуба (бот должен быть там администратором, право «Отправлять сообщения»).
Узнать ID: добавь бота в группу, напиши там /chatid — бот ответит.
"""
import os
import time
from datetime import datetime, timedelta

CLUB_GROUP_ID = [int(x) for x in os.getenv("CLUB_GROUP_ID", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()]
BIG_WIN_MIN = int(os.getenv("BIG_WIN_MIN", "500"))          # выигрыш от стольки колизов
BIG_WIN_MIN_MULT = float(os.getenv("BIG_WIN_MIN_MULT", "3"))  # и не меньше чем ×3 к ставке
BIG_WIN_GAP_HOURS = float(os.getenv("BIG_WIN_GAP_HOURS", "2"))
MSK = timedelta(hours=3)

GAME_TITLES = {"crash": "Crash", "slots": "Слоте", "hilo": "«Больше-меньше»", "probability": "Дайсе",
               "predict": "прогнозе", "battle_blackjack": "батле в 21", "battle_crash": "Crash-дуэли",
               "battle_poker": "покерном батле"}

_queue: list[dict] = []          # что опубликовать при следующем проходе фоновой задачи
_last_big_win = 0.0


def enabled() -> bool:
    return bool(CLUB_GROUP_ID)


def fmt(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def big_win(nickname: str | None, game: str, stake: int, payout: int, mult: float | None = None):
    """Вызывается из игр после выплаты. Сам решает, достаточно ли выигрыш крупный."""
    global _last_big_win
    if not enabled() or not nickname or payout < BIG_WIN_MIN or stake <= 0:
        return
    if payout / stake < BIG_WIN_MIN_MULT:
        return
    now = time.time()
    if now - _last_big_win < BIG_WIN_GAP_HOURS * 3600:
        return
    _last_big_win = now
    m = mult or payout / stake
    where = GAME_TITLES.get(game, game)
    _queue.append({"text": f"🔥 {nickname} поднял(а) {fmt(payout)} колизов в {where} — ×{m:.2f} к ставке!\n"
                           "Бесплатные игры каждый день — в кабинете COLIZEUM 👇", "button": True})


def post(text: str, button: bool = True):
    if enabled():
        _queue.append({"text": text, "button": button})


def drain() -> list[dict]:
    items = _queue[:]
    _queue.clear()
    return items


def matches_text(matches) -> str:
    lines = ["🎯 Прогнозы на киберспорт в кабинете COLIZEUM", ""]
    for m in matches:
        d = m.start_at + MSK
        lines.append(f"{m.game} · {m.league}\n{m.team1} — {m.team2}\n×{m.odds1:.2f} / ×{m.odds2:.2f} · {d:%d.%m в %H:%M} (МСК)\n")
    lines.append("Ставь колизы на победителя до начала матча — коэффициенты как у букмекеров.")
    return "\n".join(lines)


def result_text(m, results: list[dict]) -> str | None:
    if m.status != "settled":
        return None
    winner = m.team1 if m.winner == 1 else m.team2
    won = [r for r in results if r["status"] == "won"]
    if not results:
        return None
    paid = sum(r["payout"] for r in won)
    tail = (f"Угадали {len(won)} из {len(results)} — выплачено {fmt(paid)} колизов 🎉" if won
            else f"Никто из {len(results)} не угадал 😅")
    return f"✅ {m.game}: {m.team1} — {m.team2}\nПобеда: {winner}. {tail}"


def weekly_due(now: datetime | None = None, last_key: str | None = None) -> str | None:
    """Ключ недели, если пора публиковать еженедельный рейтинг (понедельник после 12:00 МСК)."""
    msk = (now or datetime.utcnow()) + MSK
    if msk.weekday() != 0 or msk.hour != 12:      # окно — с 12:00 до 13:00, чтобы перезапуск не дублировал пост
        return None
    key = msk.strftime("%G-W%V")
    return None if key == last_key else key


def weekly_text(month_title: str, top: list[dict], friendly: list[dict]) -> str | None:
    if not top and not friendly:
        return None
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"🏆 Рейтинг COLIZEUM — {month_title.lower()}, промежуточные итоги", ""]
    if top:
        lines.append("Оборот в играх:")
        lines += [f"{medal.get(i, f'{i}.')} {r['nickname']} — {fmt(r['volume'])}" for i, r in enumerate(top[:5], 1)]
        lines.append("")
    if friendly:
        lines.append("🤝 Самые дружелюбные (привели друзей):")
        lines += [f"{medal.get(i, f'{i}.')} {r['nickname']} — {r['friends']}" for i, r in enumerate(friendly[:3], 1)]
        lines.append("")
    lines.append("Призы победителям — автоматически в конце месяца. Залетай 👇")
    return "\n".join(lines)

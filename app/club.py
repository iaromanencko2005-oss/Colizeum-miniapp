"""
Разделы клуба: «Помощь у ПК» и запрос игры.

Помощь у ПК: гость выбирает место и проблему (не включается ПК, лагает, гарнитура…) —
заявка уходит в чат персонала с кнопками «Иду» и «Решено». Гость видит статус в приложении
и получает сообщение от бота, когда администратор взял заявку.
"""
from datetime import datetime, timedelta

from .models import Client, PcHelp, GameRequest
from . import club_info

KINDS = {
    "power": "не включается ПК",
    "lag": "лагает, низкий FPS",
    "gear": "гарнитура, мышь или клавиатура",
    "account": "нужно войти в аккаунт / Steam",
    "install": "нет игры или нужно обновление",
    "time": "продлить время",
    "other": "другое",
}
ACTIVE_HOURS = 2                    # заявка старше — считаем закрытой
REPEAT_COOLDOWN = timedelta(minutes=2)
REQUEST_COOLDOWN = timedelta(minutes=10)
STATUS = {"new": "Передали администратору", "going": "Администратор идёт к тебе", "done": "Решено"}


class ClubError(Exception):
    pass


def _active(db, client: Client):
    since = datetime.utcnow() - timedelta(hours=ACTIVE_HOURS)
    return (db.query(PcHelp).filter(PcHelp.client_id == client.id, PcHelp.created_at >= since,
                                    PcHelp.status.in_(["new", "going"]))
            .order_by(PcHelp.id.desc()).first())


def help_dict(h: PcHelp | None) -> dict | None:
    if h is None:
        return None
    return {"id": h.id, "seat": h.seat, "kind": h.kind, "kind_label": KINDS.get(h.kind, h.kind),
            "comment": h.comment or "", "status": h.status, "status_label": STATUS.get(h.status, h.status),
            "taken_by": (h.taken_by or "").split(" @")[0], "created_at": h.created_at.isoformat() + "Z"}


def info(db, client: Client) -> dict:
    return {"help": help_dict(_active(db, client)), "kinds": [{"id": k, "name": v} for k, v in KINDS.items()],
            **club_info.games_payload()}


def create_help(db, client: Client, seat: str, kind: str, comment: str) -> PcHelp:
    seat = (seat or "").strip()[:40]
    if not seat:
        raise ClubError("Выбери, где ты сидишь")
    if kind not in KINDS:
        raise ClubError("Выбери, что случилось")
    if _active(db, client) is not None:
        raise ClubError("Заявка уже у администратора — сейчас подойдут")
    last = db.query(PcHelp).filter(PcHelp.client_id == client.id).order_by(PcHelp.id.desc()).first()
    if last and last.created_at >= datetime.utcnow() - REPEAT_COOLDOWN:
        raise ClubError("Подожди пару минут перед новой заявкой")
    h = PcHelp(client_id=client.id, tg_id=client.tg_id, seat=seat, kind=kind, comment=(comment or "").strip()[:200] or None)
    db.add(h)
    db.commit()
    db.refresh(h)
    return h


def close_help(db, client: Client) -> PcHelp | None:
    """Гость сам отмечает «Всё решилось»."""
    h = _active(db, client)
    if h is None:
        return None
    h.status, h.done_at = "done", datetime.utcnow()
    db.commit()
    db.refresh(h)
    return h


def staff_set(db, help_id: int, status: str, who: str) -> PcHelp | None:
    h = db.get(PcHelp, help_id)
    if h is None or h.status == "done" or status not in ("going", "done"):
        return None
    if status == "going" and h.status == "going":
        return None
    if status == "going":
        h.taken_at, h.taken_by = datetime.utcnow(), who
    else:
        h.done_at = datetime.utcnow()
        h.taken_by = h.taken_by or who
    h.status = status
    db.commit()
    db.refresh(h)
    return h


def request_game(db, client: Client, title: str) -> GameRequest:
    title = " ".join((title or "").split())[:60]
    if len(title) < 2:
        raise ClubError("Напиши название игры")
    last = db.query(GameRequest).filter(GameRequest.client_id == client.id).order_by(GameRequest.id.desc()).first()
    if last and last.created_at >= datetime.utcnow() - REQUEST_COOLDOWN:
        raise ClubError("Мы уже получили твою просьбу — следующую можно через 10 минут")
    r = GameRequest(client_id=client.id, title=title)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r

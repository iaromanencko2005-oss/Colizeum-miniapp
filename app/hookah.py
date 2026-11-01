"""
Раздел «Дымок»: заказ кальяна из приложения.

Гость подтверждает 18+, выбирает табаки (описание микса — для 1–3 вкусов), крепость 1–10,
холодок, место и время подачи. Заказ уходит в чат персонала с кнопками «Принял» и «Подан».
После подачи гость может одной кнопкой попросить поменять угли, добавить холодок или забрать кальян.
Оплата — на стойке: колизы, скидки и бонусы к кальянам не применяются (закон запрещает
стимулировать продажу табака).
"""
import json
from datetime import datetime, timedelta

from sqlalchemy import func

from .models import Client, HookahOrder, HookahCall, HookahStock
from . import hookah_data as hd

MSK = timedelta(hours=3)
ACTIVE_HOURS = 6                      # заказ старше — считаем завершённым
MAX_FLAVORS, MAX_DESCRIBE = 5, 3
MAX_ACTIVE = 2
CALL_COOLDOWN = timedelta(minutes=3)
WHEN = {"now": "сейчас", "15": "через 15 минут", "30": "через 30 минут"}
CALLS = {"coals": "поменять угли", "ice": "добавить холодок", "pickup": "забрать кальян"}
STATUS = {"new": "Принят", "accepted": "Готовим", "served": "Подан", "closed": "Завершён", "cancelled": "Отменён"}


class HookahError(Exception):
    pass


def inactive_ids(db) -> set[str]:
    return {fid for (fid,) in db.query(HookahStock.flavor_id).filter(HookahStock.active == False).all()}  # noqa: E712


def _active_q(db, client: Client):
    since = datetime.utcnow() - timedelta(hours=ACTIVE_HOURS)
    return db.query(HookahOrder).filter(HookahOrder.client_id == client.id, HookahOrder.created_at >= since,
                                        HookahOrder.status.in_(["new", "accepted", "served"]))


def order_view(o: HookahOrder) -> dict:
    return {"id": o.id, "status": o.status, "status_label": STATUS.get(o.status, o.status),
            "flavors": json.loads(o.flavors), "mix_name": o.mix_name, "strength": o.strength, "cooling": bool(o.cooling),
            "seat": o.seat, "when": o.serve_when, "when_label": WHEN.get(o.serve_when, ""), "comment": o.comment or "",
            "created": f"{o.created_at + MSK:%H:%M}"}


def menu(db, client: Client) -> dict:
    off = inactive_ids(db)
    flavors = [{"id": f["id"], "name": f["name"], "group": f["group"], "notes": f["notes"], "c1": f["c1"], "c2": f["c2"],
                "active": f["id"] not in off} for f in hd.FLAVORS]
    presets = [{"ids": p, "name": hd.mix_name(p), "notes": ", ".join(hd.BY_ID[i]["name"].lower() for i in p)}
               for p in hd.PRESETS if not (set(p) & off)]
    last = (db.query(HookahOrder).filter(HookahOrder.client_id == client.id)
            .order_by(HookahOrder.id.desc()).first())
    return {"age_confirmed": bool(client.age_confirmed_at), "groups": hd.GROUPS, "flavors": flavors, "presets": presets,
            "active": [order_view(o) for o in _active_q(db, client).order_by(HookahOrder.id.desc()).all()],
            "last": order_view(last) if last else None,
            "limits": {"flavors": MAX_FLAVORS, "describe": MAX_DESCRIBE}}


def confirm_age(db, client: Client) -> None:
    client.age_confirmed_at = client.age_confirmed_at or datetime.utcnow()
    db.commit()


def _check_ids(db, ids: list[str], limit: int) -> list[str]:
    ids = list(dict.fromkeys(i for i in ids if isinstance(i, str)))
    if not ids:
        raise HookahError("Выбери хотя бы один табак")
    if len(ids) > limit:
        raise HookahError(f"Не больше {limit} вкусов")
    unknown = [i for i in ids if i not in hd.BY_ID]
    if unknown:
        raise HookahError("Такого табака нет в списке")
    off = inactive_ids(db) & set(ids)
    if off:
        raise HookahError(f"«{hd.BY_ID[sorted(off)[0]]['name']}» сейчас закончился — выбери другой вкус")
    return ids


def describe(db, ids: list[str]) -> dict:
    return hd.describe(_check_ids(db, ids, MAX_DESCRIBE))


def create_order(db, client: Client, ids: list[str], strength: int, cooling: bool, seat: str, when: str, comment: str) -> HookahOrder:
    if not client.age_confirmed_at:
        raise HookahError("Сначала подтверди, что тебе есть 18")
    if not client.phone_verified:
        raise HookahError("Подтверди номер телефона на главной — так администратор свяжется с тобой")
    ids = _check_ids(db, ids, MAX_FLAVORS)
    if not 1 <= int(strength) <= 10:
        raise HookahError("Крепость — от 1 до 10")
    seat = (seat or "").strip()[:40]
    if not seat:
        raise HookahError("Выбери, куда подать кальян")
    when = when if when in WHEN else "now"
    comment = " ".join((comment or "").split())[:200]
    active = _active_q(db, client).count()
    if active >= MAX_ACTIVE:
        raise HookahError("У тебя уже два активных кальяна — когда закончишь, нажми «Забрать кальян»")
    last = db.query(func.max(HookahOrder.created_at)).filter(HookahOrder.client_id == client.id).scalar()
    if last and datetime.utcnow() - last < timedelta(seconds=60):
        raise HookahError("Заказ только что отправлен — подожди минуту")
    if len(ids) <= MAX_DESCRIBE:
        d = hd.describe(ids)
        fl = [{"id": s["id"], "name": s["name"], "share": s["share"]} for s in d["shares"]]
        name = d["name"]
    else:
        base = 100 // len(ids)
        fl = [{"id": i, "name": hd.BY_ID[i]["name"], "share": base} for i in ids]
        fl[0]["share"] += 100 - base * len(ids)
        name = None
    o = HookahOrder(client_id=client.id, tg_id=client.tg_id, flavors=json.dumps(fl, ensure_ascii=False), mix_name=name,
                    strength=int(strength), cooling=bool(cooling), seat=seat, serve_when=when, comment=comment or None)
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def call(db, client: Client, order_id: int, kind: str) -> tuple[HookahOrder, HookahCall]:
    if kind not in CALLS:
        raise HookahError("Неизвестная просьба")
    o = _active_q(db, client).filter(HookahOrder.id == order_id).first()
    if o is None:
        raise HookahError("Этот кальян уже завершён")
    recent = (db.query(HookahCall).filter(HookahCall.order_id == o.id, HookahCall.kind == kind,
                                          HookahCall.created_at >= datetime.utcnow() - CALL_COOLDOWN).first())
    if recent:
        raise HookahError("Уже передали администратору — сейчас подойдут")
    c = HookahCall(order_id=o.id, kind=kind)
    db.add(c)
    if kind == "pickup":
        o.status, o.closed_at = "closed", datetime.utcnow()
    db.commit()
    db.refresh(o)
    db.refresh(c)
    return o, c


def staff_set(db, order_id: int, status: str) -> HookahOrder | None:
    """Персонал: accepted («Принял») или served («Подан»)."""
    o = db.get(HookahOrder, order_id)
    if o is None or o.status in ("closed", "cancelled"):
        return None
    order = ["new", "accepted", "served"]
    if order.index(status) <= order.index(o.status):
        return None
    o.status = status
    if status == "accepted":
        o.accepted_at = datetime.utcnow()
    else:
        o.accepted_at = o.accepted_at or datetime.utcnow()
        o.served_at = datetime.utcnow()
    db.commit()
    db.refresh(o)
    return o


def set_stock(db, raw: str, active: bool) -> dict | None:
    raw = (raw or "").strip().lower()
    if not raw:
        return None
    f = hd.BY_ID.get(raw) or next((x for x in hd.FLAVORS if x["name"].lower().startswith(raw)), None) \
        or next((x for x in hd.FLAVORS if raw in x["name"].lower()), None)
    if f is None:
        return None
    row = db.get(HookahStock, f["id"])
    if row is None:
        db.add(HookahStock(flavor_id=f["id"], active=active))
    else:
        row.active = active
    db.commit()
    return f


def serve_time(o: HookahOrder) -> str:
    if o.serve_when in ("15", "30"):
        return f"через {o.serve_when} мин (к {o.created_at + MSK + timedelta(minutes=int(o.serve_when)):%H:%M})"
    return "сейчас"

"""
Магазин за колизы: напитки и сладкое из бара клуба.

Как это работает:
1. Гость выбирает товары в приложении и оплачивает колизами — колизы
   списываются сразу, заказ получает номер.
2. Гость подходит к стойке и показывает экран заказа. Экран «живой»: на нём
   идут часы с секундами и бежит полоса — со скриншота или фотошопа такое
   не повторить. Номер заказа совпадает с сообщением, которое пришло
   администратору в Telegram.
3. Администратор отдаёт товар и нажимает в боте «✅ Выдан». У гостя экран
   сразу меняется на «Выдано» — второй раз по этому заказу ничего не получить.

Цены — в колизах и примерно на 20% выше, чем в баре. В магазине только
недорогое: товары, которые в баре стоят не больше SHOP_MAX_BAR_PRICE (250 ₽).
Тратить колизы можно ОДИН раз в сутки: либо заказ в магазине (до 300 колизов),
либо списание на карту гостя — не то и другое вместе.
Заказ нужно забрать за SHOP_ORDER_TTL_MIN минут (по умолчанию 3 часа) — на
экране заказа идёт обратный отсчёт. Не забрал — заказ отменяется; колизы
возвращаются, только если они ещё не сгорели бы по правилу «сутки».

Каталог при первом запуске берётся из shop_catalog.json (фото там же).
Дальше цены и наличие меняются командами в боте: /shop, /shop_price,
/shop_off, /shop_on — правки хранятся в базе и не слетают при обновлениях.
"""
import base64
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import func

from .models import ShopProduct, ShopImage, ShopOrder, Client, BonusBurn
from .bonus_expiry import refund_allowed

logger = logging.getLogger("colizeum")

CATALOG_FILE = os.path.join(os.path.dirname(__file__), "shop_catalog.json")
MAX_QTY = int(os.getenv("SHOP_MAX_QTY", "5"))           # одного товара в заказе — не больше
MAX_BAR_PRICE = int(os.getenv("SHOP_MAX_BAR_PRICE", "250"))   # дороже этого в баре — в магазин не попадает
ORDER_TTL = timedelta(minutes=int(os.getenv("SHOP_ORDER_TTL_MIN", "180")))
SHOP_MARKUP_NOTE = os.getenv(
    "SHOP_NOTE",
    "Цены указаны в колизах (1 колиз = 1 ₽) и примерно на 20% выше, чем в баре клуба.")

CATEGORIES = [("energy", "Энергетики"), ("soda", "Газировка"), ("tea", "Чай, кофе, сок"),
              ("water", "Вода"), ("sweets", "Сладкое")]
CATEGORY_TITLES = dict(CATEGORIES)


class ShopError(Exception):
    pass


def _variants(p: ShopProduct) -> list[dict]:
    try:
        return json.loads(p.variants or "[]")
    except ValueError:
        return []


def seed_catalog(db) -> int:
    """Добавляет из shop_catalog.json товары и фото, которых ещё нет в базе.
    Уже существующие товары не трогает (цены/наличие мог поменять админ)."""
    if not os.path.exists(CATALOG_FILE):
        return 0
    with open(CATALOG_FILE, encoding="utf-8") as f:
        data = json.load(f)
    existing = {pid for (pid,) in db.query(ShopProduct.id).all()}
    have_img = dict(db.query(ShopImage.key, ShopImage.version).all())
    added = 0
    for sort, p in enumerate(data.get("products", [])):
        variants = [{"id": v["id"], "label": v["label"], "active": True} for v in p["variants"]]
        if p["id"] not in existing:
            db.add(ShopProduct(id=p["id"], name=p["name"], category=p["category"], price=p["price"],
                               bar_price=p.get("bar_price"), adult=bool(p.get("adult")),
                               active=bool(p.get("active", True)), sort=sort,
                               variants=json.dumps(variants, ensure_ascii=False)))
            added += 1
        for v in p["variants"]:
            key = f"{p['id']}/{v['id']}"
            if not v.get("img"):
                continue
            raw = base64.b64decode(v["img"])
            ver = hashlib.md5(raw).hexdigest()[:8]
            if key not in have_img:
                db.add(ShopImage(key=key, mime="image/webp", data=raw, version=ver))
            elif have_img[key] != ver:          # в обновлении фото стали лучше — заменяем
                img = db.get(ShopImage, key)
                img.data, img.version = raw, ver
    db.commit()
    return added


def catalog_view(db) -> dict:
    versions = dict(db.query(ShopImage.key, ShopImage.version).all())
    products = []
    for p in db.query(ShopProduct).filter(ShopProduct.active == True).order_by(ShopProduct.sort).all():  # noqa: E712
        if not allowed(p):
            continue
        vs = [v for v in _variants(p) if v.get("active", True)]
        if not vs:
            continue
        products.append({
            "id": p.id, "name": p.name, "category": p.category, "price": p.price,
            "bar_price": p.bar_price, "adult": bool(p.adult),
            "variants": [{"id": v["id"], "label": v["label"],
                          "img": f"/api/shop/img/{p.id}/{v['id']}?v={versions.get(p.id + '/' + v['id'], '0')}"}
                         for v in vs],
        })
    cats = [{"id": c, "title": t} for c, t in CATEGORIES if any(p["category"] == c for p in products)]
    return {"categories": cats, "products": products, "note": SHOP_MARKUP_NOTE, "max_qty": MAX_QTY,
            "order_ttl_min": int(ORDER_TTL.total_seconds() // 60)}


def allowed(p: ShopProduct) -> bool:
    """В магазине — только недорогие позиции бара (по цене в баре)."""
    return MAX_BAR_PRICE <= 0 or (p.bar_price or 0) <= MAX_BAR_PRICE


def image(db, product_id: str, variant_id: str):
    return db.get(ShopImage, f"{product_id}/{variant_id}")


def build_order(db, items: list) -> tuple[list[dict], int]:
    """Проверяет корзину по ценам из базы (цены с телефона не принимаем)."""
    if not items:
        raise ShopError("Корзина пуста")
    if len(items) > 40:
        raise ShopError("Слишком большой заказ")
    merged: dict[tuple, int] = {}
    for it in items:
        if not isinstance(it, dict):
            raise ShopError("Не получилось прочитать корзину — обнови магазин")
        key = (str(it.get("product", ""))[:40], str(it.get("variant", ""))[:40])
        try:
            qty = int(it.get("qty") or 0)
        except (TypeError, ValueError):
            raise ShopError("Не получилось прочитать корзину — обнови магазин")
        if qty <= 0:
            continue
        merged[key] = merged.get(key, 0) + qty
    if not merged:
        raise ShopError("Корзина пуста")
    lines, total = [], 0
    for (pid, vid), qty in merged.items():
        p = db.get(ShopProduct, pid)
        if p is None or not p.active or not allowed(p):
            raise ShopError("Одного из товаров уже нет в продаже — обнови магазин")
        v = next((v for v in _variants(p) if v["id"] == vid and v.get("active", True)), None)
        if v is None:
            raise ShopError(f"«{p.name}»: этого вкуса уже нет — выбери другой")
        if qty > MAX_QTY:
            raise ShopError(f"«{p.name}» — не больше {MAX_QTY} шт. в одном заказе")
        label = v["label"] if len(_variants(p)) > 1 else ""
        lines.append({"product": pid, "variant": vid, "name": p.name, "label": label,
                      "price": p.price, "qty": qty, "adult": bool(p.adult)})
        total += p.price * qty
    return lines, total


def order_view(o: ShopOrder, nickname: str | None = None) -> dict:
    return {
        "id": o.id, "status": o.status, "total": o.total, "items": json.loads(o.items),
        "created_at": o.created_at.isoformat() + "Z" if o.created_at else None,
        "issued_at": o.issued_at.isoformat() + "Z" if o.issued_at else None,
        "expires_at": o.expires_at.isoformat() + "Z" if o.expires_at else None,
        "cancel_reason": o.cancel_reason, "nickname": nickname,
    }


def items_text(items: list[dict]) -> str:
    out = []
    for it in items:
        name = it["name"] + (f" ({it['label']})" if it.get("label") else "")
        out.append(f"• {name} × {it['qty']} — {it['price'] * it['qty']}")
    return "\n".join(out)


def mark_issued(db, order_id: int, by: str):
    """Отметить заказ выданным (только если он не просрочен). Возвращает заказ или None."""
    now = datetime.utcnow()
    n = db.query(ShopOrder).filter(
        ShopOrder.id == order_id, ShopOrder.status == "new",
        (ShopOrder.expires_at.is_(None)) | (ShopOrder.expires_at > now)).update(
        {ShopOrder.status: "issued", ShopOrder.issued_at: now, ShopOrder.issued_by: by[:60]},
        synchronize_session=False)
    db.commit()
    return db.get(ShopOrder, order_id) if n == 1 else None


def cancel_order(db, order_id: int, reason: str, client_id: int | None = None, status: str = "cancelled"):
    """Отменить заказ. Колизы возвращаются, только если по правилу «сутки» они бы
    ещё не сгорели. client_id — если отменяет сам гость (только свой заказ).
    Возвращает (заказ, вернули_ли_колизы) или None, если заказ уже выдан/отменён."""
    q = db.query(ShopOrder).filter(ShopOrder.id == order_id, ShopOrder.status == "new")
    if client_id is not None:
        q = q.filter(ShopOrder.client_id == client_id)
    n = q.update({ShopOrder.status: status, ShopOrder.cancelled_at: datetime.utcnow(),
                  ShopOrder.cancel_reason: reason[:100]}, synchronize_session=False)
    if n != 1:
        db.rollback()
        return None
    o = db.get(ShopOrder, order_id)
    refunded = refund_allowed(o.bonus_deadline)
    if refunded:
        db.query(Client).filter(Client.id == o.client_id).update(
            {Client.balance: func.coalesce(Client.balance, 0) + o.total}, synchronize_session=False)
        # таймер сгорания — прежний, отмена заказа его не продлевает
        c = db.get(Client, o.client_id)
        if o.bonus_deadline and (c.bonus_expires_at is None or c.bonus_expires_at > o.bonus_deadline):
            c.bonus_expires_at = o.bonus_deadline
    else:
        db.add(BonusBurn(client_id=o.client_id, amount=o.total, reason="order_refund_late"))
    db.commit()
    db.refresh(o)
    return o, refunded


def expire_due(db) -> list[tuple]:
    """Заказы, которые не забрали вовремя, — отменяем. Возвращает [(заказ, вернули_ли)]."""
    now = datetime.utcnow()
    out = []
    for (oid,) in db.query(ShopOrder.id).filter(ShopOrder.status == "new", ShopOrder.expires_at.isnot(None),
                                                ShopOrder.expires_at <= now).all():
        r = cancel_order(db, oid, "не забрали вовремя", status="expired")
        if r:
            out.append(r)
    return out


def find_product(db, raw: str):
    """Товар по id или по началу названия (для админ-команд)."""
    raw = (raw or "").strip().lower()
    if not raw:
        return None
    p = db.get(ShopProduct, raw)
    if p:
        return p
    for p in db.query(ShopProduct).order_by(ShopProduct.sort).all():
        if p.name.lower().startswith(raw) or raw in p.name.lower():
            return p
    return None


def set_variant_active(p: ShopProduct, variant_raw: str, active: bool) -> str | None:
    vs = _variants(p)
    raw = variant_raw.strip().lower()
    for v in vs:
        if v["id"] == raw or v["label"].lower().startswith(raw):
            v["active"] = active
            p.variants = json.dumps(vs, ensure_ascii=False)
            return v["label"]
    return None


def admin_list(db) -> str:
    lines = ["🛒 Магазин (id — название — цена в колизах / в баре):", ""]
    for c, title in CATEGORIES:
        rows = db.query(ShopProduct).filter(ShopProduct.category == c).order_by(ShopProduct.sort).all()
        if not rows:
            continue
        lines.append(title)
        for p in rows:
            vs = _variants(p)
            off = [v["label"] for v in vs if not v.get("active", True)]
            lines.append(f"  {p.id} — {p.name} — {p.price}" + (f" / {p.bar_price} ₽" if p.bar_price else "")
                         + ("" if p.active else "  ⛔ скрыт")
                         + (f"  (нет: {', '.join(off)})" if off else ""))
        lines.append("")
    lines += ["Цена: /shop_price redbull 300",
              "Скрыть / вернуть товар: /shop_off monster  ·  /shop_on monster",
              "Скрыть / вернуть вкус: /shop_off burn гуава  ·  /shop_on burn гуава",
              "Заказы: /orders"]
    return "\n".join(lines)

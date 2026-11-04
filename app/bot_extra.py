"""
Бот: магазин, отметки с чеком и автопосты в группу.

Кнопки под уведомлениями («✅ Выдан», «✅ Засчитать», «Кто победил?») могут
нажимать администраторы из ADMIN_IDS и любой участник чата для уведомлений
(NOTIFY_CHAT_IDS) — например, админы смены в рабочей группе.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile, WebAppInfo,
)

from .database import SessionLocal
import json

from .models import Client, ShopOrder, CheckIn, TierBonus, HookahOrder, HookahCall, WithdrawReceipt, CashbackClaim, PcHelp, TIER_LABELS, normalize_phone
from . import shop, checkin, group, referrals, access, tier_bonus, hookah, big_balance, cashback, club
from .bot import is_admin, ADMIN_IDS, WEBAPP_URL, webapp_url

logger = logging.getLogger("colizeum")
router = Router()

NOTIFY_CHAT_IDS = [int(x) for x in os.getenv("NOTIFY_CHAT_IDS", "").replace(" ", "").split(",") if x]
MSK = timedelta(hours=3)


def staff_targets() -> list[int]:
    return NOTIFY_CHAT_IDS or list(ADMIN_IDS)


def can_manage(callback: CallbackQuery) -> bool:
    chat_id = callback.message.chat.id if callback.message else None
    return is_admin(callback.from_user.id) or (chat_id in NOTIFY_CHAT_IDS)


def who(user) -> str:
    return (user.first_name or "") + (f" @{user.username}" if user.username else "")


def msk(dt: datetime) -> str:
    return (dt + MSK).strftime("%H:%M")


def fmt(n) -> str:
    return f"{int(n):,}".replace(",", " ")


async def send_staff(bot: Bot, text: str, markup=None, photo: bytes | None = None) -> str | None:
    """Отправляет в чаты персонала. Возвращает file_id фото (если было фото)."""
    file_id = None
    for chat_id in staff_targets():
        try:
            if photo is not None and file_id is None:
                m = await bot.send_photo(chat_id, BufferedInputFile(photo, "check.jpg"), caption=text, reply_markup=markup)
                file_id = m.photo[-1].file_id if m.photo else None
            elif photo is not None:
                await bot.send_photo(chat_id, file_id, caption=text, reply_markup=markup)
            else:
                await bot.send_message(chat_id, text, reply_markup=markup)
        except Exception as e:  # noqa: BLE001
            logger.warning("Не удалось отправить в чат %s: %s", chat_id, e)
    return file_id


async def tell(bot: Bot, tg_id: int, text: str, cabinet: bool = False):
    try:
        markup = None
        if cabinet and WEBAPP_URL:
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="Открыть кабинет", web_app=WebAppInfo(url=webapp_url(tg_id=tg_id)))]])
        await bot.send_message(tg_id, text, reply_markup=markup)
    except Exception as e:  # noqa: BLE001
        logger.info("Не удалось написать гостю %s: %s", tg_id, e)


async def _edit(callback: CallbackQuery, text: str):
    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=text, reply_markup=None)
        else:
            await callback.message.edit_text(text, reply_markup=None)
    except Exception as e:  # noqa: BLE001
        logger.info("Не удалось обновить сообщение: %s", e)


def guest_line(client: Client) -> str:
    name = client.nickname or client.tg_name or "гость"
    real = f" ({client.tg_name})" if client.nickname and client.tg_name else ""
    return f"{name}{real}, {client.phone or 'номер не подтверждён'}"


# ---------- магазин ----------

async def shop_notify(bot: Bot, order: ShopOrder, client: Client, items: list[dict]):
    adult = any(it.get("adult") for it in items)
    text = (f"🛒 Заказ №{order.id} — {fmt(order.total)} колизов (уже списаны)\n"
            f"Гость: {guest_line(client)}\n\n{shop.items_text(items)}\n\n"
            + ("⚠️ Есть энергетики — выдаём только с 18 лет, при сомнении спроси документ.\n" if adult else "")
            + (f"Забрать до {msk(order.expires_at)} — потом заказ сгорит сам.\n" if order.expires_at else "")
            + f"Гость покажет «живой» экран заказа №{order.id} в приложении (идут часы, бежит полоса). "
              "Отдай товар и нажми «Выдан».")
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выдан", callback_data=f"so:iss:{order.id}"),
        InlineKeyboardButton(text="↩️ Отменить", callback_data=f"so:can:{order.id}"),
    ]])
    await send_staff(bot, text, markup)


@router.callback_query(F.data.startswith("so:"))
async def cb_shop(callback: CallbackQuery):
    if not can_manage(callback):
        await callback.answer("Это кнопка для администратора", show_alert=True)
        return
    _, act, raw = (callback.data.split(":") + ["", ""])[:3]
    if not raw.isdigit():
        await callback.answer()
        return
    db = SessionLocal()
    try:
        if act == "iss":
            o = shop.mark_issued(db, int(raw), who(callback.from_user))
            if o is None:
                cur = db.get(ShopOrder, int(raw))
                st = {"issued": "уже выдан", "cancelled": "отменён", "expired": "просрочен — не выдавай"}.get(
                    cur.status if cur else "", "просрочен — не выдавай")
                await callback.answer(f"Заказ {st}", show_alert=True)
                return
            await callback.answer("Отмечено: выдан")
            await _edit(callback, (callback.message.text or "").split("\n\nГость покажет")[0]
                        + f"\n\n✅ Выдан в {msk(o.issued_at)} — {who(callback.from_user)}")
        elif act == "can":
            r = shop.cancel_order(db, int(raw), f"отменил администратор {who(callback.from_user)}")
            if r is None:
                await callback.answer("Заказ уже выдан или отменён", show_alert=True)
                return
            o, refunded = r
            back = f"{fmt(o.total)} колизов вернулись" if refunded else "срок колизов уже истёк — они сгорели"
            await callback.answer("Отменён")
            await _edit(callback, (callback.message.text or "").split("\n\nГость покажет")[0]
                        + f"\n\n↩️ Отменён — {who(callback.from_user)}. {back}.")
            await tell(callback.bot, o.tg_id, f"Заказ №{o.id} в магазине отменён администратором — {back}.")
    finally:
        db.close()


@router.message(Command("shop"))
async def cmd_shop(message: Message):
    if not is_admin(message.from_user.id):
        return
    db = SessionLocal()
    try:
        await message.answer(shop.admin_list(db))
    finally:
        db.close()


@router.message(Command("shop_price"))
async def cmd_shop_price(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").rsplit(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /shop_price redbull 300  (список товаров: /shop)")
        return
    db = SessionLocal()
    try:
        p = shop.find_product(db, parts[0])
        if p is None:
            await message.answer("Товар не найден — посмотри id в /shop")
            return
        old, p.price = p.price, int(parts[1])
        p.updated_at = datetime.utcnow()
        db.commit()
        await message.answer(f"«{p.name}»: {old} → {p.price} колизов")
    finally:
        db.close()


async def _toggle(message: Message, command: CommandObject, on: bool):
    if not is_admin(message.from_user.id):
        return
    args = (command.args or "").split(maxsplit=1)
    if not args:
        await message.answer(f"Формат: /shop_{'on' if on else 'off'} monster  или  /shop_{'on' if on else 'off'} burn гуава")
        return
    db = SessionLocal()
    try:
        p = shop.find_product(db, args[0])
        if p is None:
            await message.answer("Товар не найден — посмотри id в /shop")
            return
        if len(args) == 2:
            label = shop.set_variant_active(p, args[1], on)
            if label is None:
                await message.answer("Такого вкуса нет у этого товара — посмотри в /shop")
                return
            db.commit()
            await message.answer(f"«{p.name}», вкус «{label}»: {'снова в продаже' if on else 'скрыт'}")
        else:
            p.active = on
            db.commit()
            await message.answer(f"«{p.name}»: {'снова в магазине' if on else 'скрыт из магазина'}")
    finally:
        db.close()


@router.message(Command("shop_off"))
async def cmd_shop_off(message: Message, command: CommandObject):
    await _toggle(message, command, False)


@router.message(Command("shop_on"))
async def cmd_shop_on(message: Message, command: CommandObject):
    await _toggle(message, command, True)


@router.message(Command("orders"))
async def cmd_orders(message: Message):
    if not is_admin(message.from_user.id):
        return
    db = SessionLocal()
    try:
        pending = db.query(ShopOrder).filter(ShopOrder.status == "new").order_by(ShopOrder.id.desc()).limit(20).all()
        week = datetime.utcnow() - timedelta(days=7)
        issued = db.query(ShopOrder).filter(ShopOrder.status == "issued", ShopOrder.issued_at >= week).all()
        lines = [f"🛒 Не выданы: {len(pending)}"]
        for o in pending:
            c = db.get(Client, o.client_id)
            lines.append(f"№{o.id} — {fmt(o.total)} — {c.nickname or c.tg_name if c else '—'} — {(o.created_at + MSK):%d.%m %H:%M}")
        lines += ["", f"Выдано за 7 дней: {len(issued)} заказов на {fmt(sum(o.total for o in issued))} колизов"]
        markup = None
        if pending:
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"✅ №{o.id} выдан", callback_data=f"so:iss:{o.id}")] for o in pending[:8]])
        await message.answer("\n".join(lines), reply_markup=markup)
    finally:
        db.close()


# ---------- отметка с чеком ----------

def _visits_word(n: int) -> str:
    a, b = n % 10, n % 100
    return "визит" if a == 1 and b != 11 else ("визита" if 2 <= a <= 4 and not 12 <= b <= 14 else "визитов")


def checkin_markup(cid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Засчитать (+{checkin.CHECKIN_BONUS})", callback_data=f"ci:ok:{cid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ci:no:{cid}"),
    ]])


async def checkin_notify(bot: Bot, c: CheckIn, client: Client, photo: bytes | None = None, file_id: str | None = None) -> str | None:
    db = SessionLocal()
    try:
        visits = checkin.visits_this_month(db, client)
    finally:
        db.close()
    text = (f"🧾 Отметка «Я в клубе» №{c.id}\nГость: {guest_line(client)}\n"
            f"Визитов в этом месяце (подтверждённых): {visits}\n\n"
            "Проверь, что чек сегодняшний и из нашего клуба.")
    if file_id:
        for chat_id in staff_targets():
            try:
                await bot.send_photo(chat_id, file_id, caption=text, reply_markup=checkin_markup(c.id))
            except Exception as e:  # noqa: BLE001
                logger.warning("Чек не отправлен в %s: %s", chat_id, e)
        return file_id
    return await send_staff(bot, text, checkin_markup(c.id), photo=photo)


@router.callback_query(F.data.startswith("ci:"))
async def cb_checkin(callback: CallbackQuery):
    if not can_manage(callback):
        await callback.answer("Это кнопка для администратора", show_alert=True)
        return
    _, act, raw = (callback.data.split(":") + ["", ""])[:3]
    if not raw.isdigit():
        await callback.answer()
        return
    db = SessionLocal()
    try:
        c = checkin.decide(db, int(raw), act == "ok", who(callback.from_user))
        if c is None:
            await callback.answer("По этой отметке уже приняли решение", show_alert=True)
            return
        visits = checkin.visits_this_month(db, db.get(Client, c.client_id)) if c.status == "approved" else 0
        c_tg, c_status, c_bonus = c.tg_id, c.status, c.bonus     # дальше коммиты — запоминаем значения
        rewards = []
        if c.status == "approved":
            from . import social
            guest = db.get(Client, c.client_id)
            rewards = social.grant_visit_rewards(db, guest)
            guest_line_txt = guest_line(guest)
    finally:
        db.close()
    await callback.answer("Готово")
    base = (callback.message.caption or "").split("\n\nПроверь")[0]
    if c_status == "approved":
        await _edit(callback, base + f"\n\n✅ Засчитано (+{c_bonus}) — {who(callback.from_user)}")
        await tell(callback.bot, c_tg, f"✅ Визит засчитан! " + (f"+{c_bonus} колизов на баланс. " if c_bonus else "")
                   + f"Визитов в этом месяце: {visits}. До встречи в COLIZEUM!")
    else:
        await _edit(callback, base + f"\n\n❌ Отклонено — {who(callback.from_user)}")
        await tell(callback.bot, c_tg, "Отметку по чеку не засчитали: администратор не смог его подтвердить. "
                                          "Пришли фото сегодняшнего чека из COLIZEUM ещё раз — чек должен читаться.")
    for r in rewards:
        if r["reward"].isdigit():
            await tell(callback.bot, c_tg, f"Серия визитов: {r['n']} {_visits_word(r['n'])} в этом месяце — +{r['reward']} колизов!")
        else:
            await tell(callback.bot, c_tg, f"Серия визитов: {r['n']} {_visits_word(r['n'])} в этом месяце — бесплатный час! "
                                              "Администратор начислит его на твою карту гостя.")
            await send_staff(callback.bot, f"Начисли бесплатный час в CRM: {guest_line_txt} — {r['n']} {_visits_word(r['n'])} за месяц.")


@router.message(F.photo, F.chat.type == "private")
async def guest_photo(message: Message):
    """Гость прислал боту фото — считаем, что это чек для отметки «Я в клубе»
    (запасной способ, если в приложении не получилось приложить фото)."""
    if is_admin(message.from_user.id):
        return      # админы присылают боту фото для рассылок — это не чеки
    db = SessionLocal()
    try:
        client = db.query(Client).filter(Client.tg_id == message.from_user.id).first()
        if client is None or client.consent_at is None:
            await message.answer("Сначала открой личный кабинет (/start) и прими правила.")
            return
        try:
            checkin.can_submit(db, client)
        except checkin.CheckInError as e:
            await message.answer(str(e))
            return
        c = checkin.create(db, client)
        file_id = message.photo[-1].file_id
        c.photo_file_id = file_id
        db.commit()
        cid = c.id
        client_snapshot = client
        await checkin_notify(message.bot, c, client_snapshot, file_id=file_id)
    finally:
        db.close()
    await message.answer(f"🧾 Чек получил и отправил администратору (отметка №{cid}). "
                         f"Как только подтвердит — придут +{checkin.CHECKIN_BONUS} колизов.")


# ---------- проверка пополнения (открывает бесплатные игры) ----------

def topup_markup(cid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Пополнение есть", callback_data=f"tp:ok:{cid}"),
        InlineKeyboardButton(text="❌ Не нашёл", callback_data=f"tp:no:{cid}"),
    ]])


async def topup_check_notify(bot: Bot, client: Client, min_topup: int):
    tg = f"@{client.tg_username}" if client.tg_username else f"id {client.tg_id}"
    text = (f"💳 Проверка пополнения\n"
            f"Гость: {guest_line(client)}\n"
            f"Telegram: {tg}\n"
            f"В выгрузке CRM номер не найден.\n\n"
            f"Проверь в CRM, пополнил ли гость счёт от {fmt(min_topup)} ₽. "
            f"Подтвердишь — гостю откроются бесплатные игры в приложении.")
    await send_staff(bot, text, topup_markup(client.id))


@router.callback_query(F.data.startswith("tp:"))
async def cb_topup(callback: CallbackQuery):
    if not can_manage(callback):
        await callback.answer("Это кнопка для администратора", show_alert=True)
        return
    _, act, raw = (callback.data.split(":") + ["", ""])[:3]
    if not raw.isdigit():
        await callback.answer()
        return
    db = SessionLocal()
    try:
        c = access.decide(db, int(raw), act == "ok")
        if c is None:
            await callback.answer("По этой заявке уже приняли решение", show_alert=True)
            return
        c_tg, c_ok = c.tg_id, c.topup_check_state == "approved"
    finally:
        db.close()
    await callback.answer("Готово")
    base = (callback.message.text or "").split("\n\nПроверь в CRM")[0]
    if c_ok:
        await _edit(callback, base + f"\n\n✅ Пополнение подтверждено — {who(callback.from_user)}")
        await tell(callback.bot, c_tg, "✅ Пополнение подтверждено! Бесплатные игры в кабинете COLIZEUM открыты — "
                                        "рулетка, кости и дайс каждый день.", cabinet=True)
    else:
        await _edit(callback, base + f"\n\n❌ Не подтверждено — {who(callback.from_user)}")
        await tell(callback.bot, c_tg, f"Администратор не нашёл пополнение от {fmt(access.UNLOCK_MIN_TOPUP)} ₽ на твоём номере. "
                                        "Пополни счёт на стойке клуба и нажми «Проверить пополнение» в разделе «Игры» ещё раз.")


async def _set_games_access(message: Message, command: CommandObject, on: bool):
    if not is_admin(message.from_user.id):
        return
    norm = normalize_phone(command.args or "")
    if len(norm) < 10:
        await message.answer(f"Формат: /{'unlock' if on else 'lock'} +79991234567")
        return
    db = SessionLocal()
    try:
        c = db.query(Client).filter(Client.phone_normalized == norm, Client.phone_verified == True).first()  # noqa: E712
        if c is None:
            await message.answer("Гость с таким подтверждённым номером не найден")
            return
        access.set_unlocked(db, c, on)
        c_tg = c.tg_id
    finally:
        db.close()
    if on:
        await message.answer("Бесплатные игры для гостя открыты.")
        await tell(message.bot, c_tg, "✅ Бесплатные игры в кабинете COLIZEUM открыты!", cabinet=True)
    else:
        await message.answer("Бесплатные игры для гостя закрыты (CRM их не откроет, пока не сделаешь /unlock).")


@router.message(Command("unlock"))
async def cmd_unlock(message: Message, command: CommandObject):
    await _set_games_access(message, command, True)


@router.message(Command("lock"))
async def cmd_lock(message: Message, command: CommandObject):
    await _set_games_access(message, command, False)


# ---------- ежемесячный бонус уровня ----------

def tier_bonus_markup(bid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Начислил в CRM", callback_data=f"tb:ok:{bid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"tb:no:{bid}"),
    ]])


def _tier_bonus_text(row: TierBonus, client: Client) -> str:
    tg = f"@{client.tg_username}" if client.tg_username else f"id {client.tg_id}"
    month = tier_bonus.MONTHS[int(row.month[5:]) - 1]
    return (f"🎁 Бонус уровня {row.tier.capitalize()} за {month}: {fmt(row.amount)} колизов\n"
            f"Гость: {guest_line(client)}\nTelegram: {tg}\n\n"
            f"Начисли гостю {fmt(row.amount)} бонусов на карту в CRM и нажми «Начислил».")


async def tier_bonus_notify(bot: Bot, row: TierBonus, client: Client):
    await send_staff(bot, _tier_bonus_text(row, client), tier_bonus_markup(row.id))


@router.callback_query(F.data.startswith("tb:"))
async def cb_tier_bonus(callback: CallbackQuery):
    if not can_manage(callback):
        await callback.answer("Это кнопка для администратора", show_alert=True)
        return
    _, act, raw = (callback.data.split(":") + ["", ""])[:3]
    if not raw.isdigit():
        await callback.answer()
        return
    db = SessionLocal()
    try:
        row = tier_bonus.decide(db, int(raw), act == "ok", who(callback.from_user))
        if row is None:
            await callback.answer("По этой заявке уже приняли решение", show_alert=True)
            return
        c = db.get(Client, row.client_id)
        c_tg, amount, ok = c.tg_id if c else None, row.amount, row.status == "done"
    finally:
        db.close()
    await callback.answer("Готово")
    base = (callback.message.text or "").split("\n\nНачисли гостю")[0]
    await _edit(callback, base + (f"\n\n✅ Начислено — {who(callback.from_user)}" if ok else f"\n\n❌ Отклонено — {who(callback.from_user)}"))
    if c_tg:
        await tell(callback.bot, c_tg, f"🎁 Бонус уровня зачислен: {fmt(amount)} бонусов уже на твоей карте гостя COLIZEUM." if ok
                   else "Бонус уровня в этом месяце не начислен — уточни у администратора на стойке.")


@router.message(Command("bonuses"))
async def cmd_bonuses(message: Message):
    """Заявки на бонус уровня, по которым ещё не приняли решение."""
    if not is_admin(message.from_user.id):
        return
    db = SessionLocal()
    try:
        rows = db.query(TierBonus).filter(TierBonus.status == "requested").order_by(TierBonus.id).limit(20).all()
        month = tier_bonus.month_key()
        done = db.query(TierBonus).filter(TierBonus.month == month, TierBonus.status == "done").all()
        await message.answer(f"Бонусы уровня за этот месяц: начислено {len(done)} на {fmt(sum(r.amount for r in done))} колизов. "
                             f"Ждут решения: {len(rows)}.")
        for r in rows:
            c = db.get(Client, r.client_id)
            if c:
                await message.answer(_tier_bonus_text(r, c), reply_markup=tier_bonus_markup(r.id))
    finally:
        db.close()


# ---------- «Дымок»: заказы кальянов ----------

def _hookah_text(o: HookahOrder, client: Client) -> str:
    fl = json.loads(o.flavors)
    tg = f"@{client.tg_username}" if client.tg_username else f"id {client.tg_id}"
    return (f"💨 Кальян №{o.id} — к месту {o.seat}\n"
            f"Гость: {guest_line(client)}, {tg}\n\n"
            + (f"Микс: {o.mix_name}\n" if o.mix_name else "")
            + "Вкусы: " + ", ".join(f"{x['name']} {x['share']}%" for x in fl) + "\n"
            f"Крепость: {o.strength} из 10\n"
            f"Холодок: {'да' if o.cooling else 'нет'}\n"
            f"Подать: {hookah.serve_time(o)}"
            + (f"\nКомментарий: {o.comment}" if o.comment else "")
            + "\n\nПри подаче проверь возраст гостя.")


def hookah_markup(o: HookahOrder) -> InlineKeyboardMarkup | None:
    row = []
    if o.status == "new":
        row.append(InlineKeyboardButton(text="✅ Принял", callback_data=f"hk:acc:{o.id}"))
    if o.status in ("new", "accepted"):
        row.append(InlineKeyboardButton(text="💨 Подан", callback_data=f"hk:srv:{o.id}"))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


async def hookah_notify(bot: Bot, o: HookahOrder, client: Client):
    await send_staff(bot, _hookah_text(o, client), hookah_markup(o))


async def hookah_call_notify(bot: Bot, o: HookahOrder, c: HookahCall, client: Client):
    icon = {"coals": "🔥", "ice": "❄️", "pickup": "🧹"}.get(c.kind, "💨")
    text = f"{icon} {o.seat} (кальян №{o.id}): гость просит {hookah.CALLS[c.kind]}\nГость: {guest_line(client)}"
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Сделано", callback_data=f"hc:done:{c.id}")]])
    await send_staff(bot, text, markup)


@router.callback_query(F.data.startswith("hk:"))
async def cb_hookah(callback: CallbackQuery):
    if not can_manage(callback):
        await callback.answer("Это кнопка для администратора", show_alert=True)
        return
    _, act, raw = (callback.data.split(":") + ["", ""])[:3]
    if not raw.isdigit() or act not in ("acc", "srv"):
        await callback.answer()
        return
    db = SessionLocal()
    try:
        o = hookah.staff_set(db, int(raw), "accepted" if act == "acc" else "served")
        if o is None:
            await callback.answer("Статус уже обновлён", show_alert=True)
            return
        o_id, o_tg, o_status, markup = o.id, o.tg_id, o.status, hookah_markup(o)
    finally:
        db.close()
    await callback.answer("Готово")
    base = (callback.message.text or "").split("\n\n✅")[0].split("\n\n💨 Подан")[0]
    mark = (f"\n\n✅ Принял — {who(callback.from_user)}" if o_status == "accepted"
            else f"\n\n💨 Подан в {msk(datetime.utcnow())} — {who(callback.from_user)}")
    try:
        await callback.message.edit_text(base + mark, reply_markup=markup)
    except Exception as e:  # noqa: BLE001
        logger.info("Не удалось обновить сообщение: %s", e)
    if o_status == "served":
        await tell(callback.bot, o_tg, f"💨 Кальян №{o_id} подан. Нужно поменять угли или добавить холодок — "
                                        "нажми кнопку в разделе «Дымок».", cabinet=True)


@router.callback_query(F.data.startswith("hc:"))
async def cb_hookah_call(callback: CallbackQuery):
    if not can_manage(callback):
        await callback.answer("Это кнопка для администратора", show_alert=True)
        return
    raw = (callback.data.split(":") + ["", "", ""])[2]
    if not raw.isdigit():
        await callback.answer()
        return
    db = SessionLocal()
    try:
        c = db.get(HookahCall, int(raw))
        if c is None or c.done:
            await callback.answer("Уже отмечено", show_alert=True)
            return
        c.done = True
        db.commit()
    finally:
        db.close()
    await callback.answer("Готово")
    await _edit(callback, (callback.message.text or "") + f"\n\n✅ Сделано — {who(callback.from_user)}")


@router.message(Command("hookah"))
async def cmd_hookah(message: Message):
    if not is_admin(message.from_user.id):
        return
    db = SessionLocal()
    try:
        off = hookah.inactive_ids(db)
        since = datetime.utcnow() - timedelta(days=30)
        n = db.query(HookahOrder).filter(HookahOrder.created_at >= since).count()
    finally:
        db.close()
    from . import hookah_data as hd
    lines = [f"Табаки «Дымка» (заказов за 30 дней: {n}):", ""]
    for f in hd.FLAVORS:
        lines.append(("⛔️ " if f["id"] in off else "✅ ") + f["name"])
    lines += ["", "Закончился вкус: /hookah_off чизкейк", "Снова есть: /hookah_on чизкейк"]
    await message.answer("\n".join(lines))


async def _hookah_stock(message: Message, command: CommandObject, active: bool):
    if not is_admin(message.from_user.id):
        return
    db = SessionLocal()
    try:
        f = hookah.set_stock(db, command.args or "", active)
    finally:
        db.close()
    if f is None:
        await message.answer(f"Не нашёл такой табак. Формат: /hookah_{'on' if active else 'off'} чизкейк (список — /hookah)")
        return
    await message.answer(f"«{f['name']}» — {'снова в списке' if active else 'скрыт из списка: закончился'}.")


@router.message(Command("hookah_off"))
async def cmd_hookah_off(message: Message, command: CommandObject):
    await _hookah_stock(message, command, False)


@router.message(Command("hookah_on"))
async def cmd_hookah_on(message: Message, command: CommandObject):
    await _hookah_stock(message, command, True)


# ---------- чек о пополнении для списания при большом балансе ----------

def receipt_markup(rid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Пополнение есть", callback_data=f"wr:ok:{rid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wr:no:{rid}"),
    ]])


async def receipt_notify(bot: Bot, r: WithdrawReceipt, client: Client, photo: bytes) -> str | None:
    text = (f"🧾 Чек о пополнении — для списания колизов\n"
            f"Гость: {guest_line(client)}\nБаланс в приложении: {fmt(client.balance or 0)} колизов\n\n"
            f"Проверь, что чек из COLIZEUM и пополнение свежее. Подтвердишь — гостю откроется одно списание "
            f"(до {fmt(big_balance.RECEIPT_HOURS)} ч).")
    return await send_staff(bot, text, receipt_markup(r.id), photo=photo)


@router.callback_query(F.data.startswith("wr:"))
async def cb_receipt(callback: CallbackQuery):
    if not can_manage(callback):
        await callback.answer("Это кнопка для администратора", show_alert=True)
        return
    _, act, raw = (callback.data.split(":") + ["", ""])[:3]
    if not raw.isdigit():
        await callback.answer()
        return
    db = SessionLocal()
    try:
        r = big_balance.decide(db, int(raw), act == "ok", who(callback.from_user))
        if r is None:
            await callback.answer("По этому чеку уже приняли решение", show_alert=True)
            return
        r_tg, ok = r.tg_id, r.status == "approved"
    finally:
        db.close()
    await callback.answer("Готово")
    base = (callback.message.caption or callback.message.text or "").split("\n\nПроверь, что чек")[0]
    await _edit(callback, base + (f"\n\n✅ Подтверждено — {who(callback.from_user)}" if ok else f"\n\n❌ Отклонено — {who(callback.from_user)}"))
    await tell(callback.bot, r_tg, "✅ Чек подтверждён — списание колизов открыто. Отправь заявку в кабинете." if ok
               else "Чек о пополнении не подтвердили. Пришли фото свежего чека из COLIZEUM — или сыграй на сумму баланса.",
               cabinet=True)


# ---------- кэшбэк за пополнение ----------

def cashback_markup(c: CashbackClaim) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Начислить {fmt(c.cashback)}" if c.cashback else "✅ Пополнение есть", callback_data=f"cs:ok:{c.id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"cs:no:{c.id}"),
    ]])


def _cashback_text(c: CashbackClaim, client: Client) -> str:
    tg = f"@{client.tg_username}" if client.tg_username else f"id {client.tg_id}"
    return (f"💳 Кэшбэк за пополнение №{c.id}\n"
            f"Гость: {guest_line(client)}, {tg}\n"
            f"Статус: {TIER_LABELS.get(c.tier, c.tier)} — кэшбэк {c.percent}%\n\n"
            f"Пополнение: {fmt(c.amount)} ₽ на номер {c.phone}\n"
            f"К начислению: {fmt(c.cashback)} колизов\n\n"
            f"Проверь пополнение в CRM. Если сумма другая — /cashback {c.id} <сумма в CRM>")


async def cashback_notify(bot: Bot, c: CashbackClaim, client: Client, photo: bytes) -> str | None:
    return await send_staff(bot, _cashback_text(c, client), cashback_markup(c), photo=photo)


async def _cashback_done(bot: Bot, c: CashbackClaim):
    if c.status == "approved" and c.cashback > 0:
        await tell(bot, c.tg_id, f"💳 Кэшбэк начислен: +{fmt(c.cashback)} колизов за пополнение на {fmt(c.amount)} ₽ ({c.percent}%). "
                                 "Бесплатные игры и списание открыты.", cabinet=True)
    elif c.status == "approved":
        await tell(bot, c.tg_id, f"✅ Пополнение на {fmt(c.amount)} ₽ подтверждено — бесплатные игры открыты. "
                                 "Кэшбэк колизами начисляется гостям со статусом Silver, Gold и Premium.", cabinet=True)
    else:
        await tell(bot, c.tg_id, "Кэшбэк по чеку не начислен: администратор не нашёл это пополнение в системе клуба. "
                                 "Проверь номер и сумму и отправь чек ещё раз.", cabinet=True)


@router.callback_query(F.data.startswith("cs:"))
async def cb_cashback(callback: CallbackQuery):
    if not can_manage(callback):
        await callback.answer("Это кнопка для администратора", show_alert=True)
        return
    _, act, raw = (callback.data.split(":") + ["", ""])[:3]
    if not raw.isdigit():
        await callback.answer()
        return
    db = SessionLocal()
    try:
        c = cashback.decide(db, int(raw), act == "ok", who(callback.from_user))
        if c is None:
            await callback.answer("По этому чеку уже приняли решение", show_alert=True)
            return
        db.expunge(c)
    finally:
        db.close()
    await callback.answer("Готово")
    base = (callback.message.caption or callback.message.text or "").split("\n\nПроверь пополнение")[0]
    await _edit(callback, base + (f"\n\n✅ Подтверждено, начислено {fmt(c.cashback)} — {who(callback.from_user)}" if c.status == "approved"
                                  else f"\n\n❌ Отклонено — {who(callback.from_user)}"))
    await _cashback_done(callback.bot, c)


@router.message(Command("cashback"))
async def cmd_cashback(message: Message, command: CommandObject):
    """/cashback 12 1500 — начислить по чеку №12, если в CRM другая сумма пополнения."""
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").replace("₽", "").split()
    if len(parts) != 2 or not parts[0].lstrip("#").isdigit() or not parts[1].isdigit():
        await message.answer("Формат: /cashback 12 1500 — номер чека и сумма пополнения в CRM")
        return
    db = SessionLocal()
    try:
        c = cashback.decide(db, int(parts[0].lstrip("#")), True, who(message.from_user), amount=int(parts[1]))
        if c is None:
            await message.answer("Чек не найден или по нему уже приняли решение")
            return
        db.expunge(c)
    finally:
        db.close()
    await message.answer(f"Чек №{c.id}: пополнение {fmt(c.amount)} ₽ → начислено {fmt(c.cashback)} колизов ({c.percent}%).")
    await _cashback_done(message.bot, c)


@router.message(Command("cashbacks"))
async def cmd_cashbacks(message: Message):
    """Чеки, которые ждут решения."""
    if not is_admin(message.from_user.id):
        return
    db = SessionLocal()
    try:
        rows = db.query(CashbackClaim).filter(CashbackClaim.status == "pending").order_by(CashbackClaim.id).limit(15).all()
        if not rows:
            await message.answer("Все чеки на кэшбэк проверены.")
            return
        for c in rows:
            cl = db.get(Client, c.client_id)
            if cl is None:
                continue
            markup = cashback_markup(c)
            if c.photo_file_id:
                await message.answer_photo(c.photo_file_id, caption=_cashback_text(c, cl), reply_markup=markup)
            else:
                await message.answer(_cashback_text(c, cl), reply_markup=markup)
    finally:
        db.close()


@router.message(Command("chatid"))
async def cmd_chatid(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(f"ID этого чата: {message.chat.id}\n"
                         "Для автопостов в группу клуба: Railway → Variables → CLUB_GROUP_ID = это число.")


async def post_to_group(bot: Bot, text: str, button: bool = True):
    if not group.CLUB_GROUP_ID:
        return
    markup = None
    if button and referrals.BOT_USERNAME:
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="🎮 Открыть кабинет COLIZEUM", url=f"https://t.me/{referrals.BOT_USERNAME}?start=group")]])
    for chat_id in group.CLUB_GROUP_ID:
        try:
            await bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("Пост в группу %s не отправлен: %s", chat_id, e)


async def notify_activations(bot: Bot, paid: list[dict]):
    for p in paid:
        await tell(bot, p["tg_id"], f"💸 {p['friend']} пополнил(а) счёт в COLIZEUM — тебе +{p['amount']} колизов "
                                    "за приглашённого друга! Пригласить ещё: /bonus")


# ---------- помощь у ПК ----------

PC_HELP_ICON = {"power": "🔌", "lag": "🐢", "gear": "🎧", "account": "🔑", "install": "💿", "time": "⏱", "other": "🛠"}


def pc_help_markup(h: PcHelp):
    row = []
    if h.status == "new":
        row.append(InlineKeyboardButton(text="🏃 Иду", callback_data=f"ph:go:{h.id}"))
    if h.status in ("new", "going"):
        row.append(InlineKeyboardButton(text="✅ Решено", callback_data=f"ph:done:{h.id}"))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


async def pc_help_notify(bot: Bot, h: PcHelp, client: Client):
    icon = PC_HELP_ICON.get(h.kind, "🛠")
    text = (f"{icon} Помощь у ПК — {h.seat}\n"
            f"Проблема: {club.KINDS.get(h.kind, h.kind)}"
            + (f"\nКомментарий: {h.comment}" if h.comment else "")
            + f"\nГость: {guest_line(client)}")
    await send_staff(bot, text, pc_help_markup(h))


async def pc_help_closed_by_guest(bot: Bot, h: PcHelp, client: Client):
    await send_staff(bot, f"👌 {h.seat}: гость отметил, что всё решилось (заявка №{h.id})")


async def game_request_notify(bot: Bot, r, client: Client):
    await send_staff(bot, f"💿 Гость просит игру: {r.title}\nГость: {guest_line(client)}")


@router.callback_query(F.data.startswith("ph:"))
async def cb_pc_help(callback: CallbackQuery):
    if not can_manage(callback):
        await callback.answer("Это кнопка для администратора", show_alert=True)
        return
    _, act, raw = (callback.data.split(":") + ["", ""])[:3]
    if not raw.isdigit() or act not in ("go", "done"):
        await callback.answer()
        return
    db = SessionLocal()
    try:
        h = club.staff_set(db, int(raw), "going" if act == "go" else "done", who(callback.from_user))
        if h is None:
            await callback.answer("Заявка уже обновлена", show_alert=True)
            return
        h_tg, h_seat, h_status, markup = h.tg_id, h.seat, h.status, pc_help_markup(h)
    finally:
        db.close()
    await callback.answer("Готово")
    base = (callback.message.text or "").split("\n\n🏃")[0].split("\n\n✅")[0]
    mark = (f"\n\n🏃 Идёт — {who(callback.from_user)}" if h_status == "going"
            else f"\n\n✅ Решено в {msk(datetime.utcnow())} — {who(callback.from_user)}")
    try:
        await callback.message.edit_text(base + mark, reply_markup=markup)
    except Exception as e:  # noqa: BLE001
        logger.info("Не удалось обновить сообщение: %s", e)
    if h_tg and h_status == "going":
        try:
            await callback.bot.send_message(h_tg, f"🏃 Администратор идёт к тебе — {h_seat}. Уже рядом!")
        except Exception as e:  # noqa: BLE001
            logger.info("Не удалось написать гостю: %s", e)

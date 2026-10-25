"""
Бот: афиша клуба и тиммейты.

Афиша (только для ADMIN_IDS):
  фото с подписью  /event 01.11 20:00 | Турнир CS2 5×5 | Описание   — новое событие с картинкой;
  то же текстом без фото — событие без картинки;
  /events — список будущих событий с кнопками «Опубликовать в группе» и «Удалить».
Тиммейты: заявки гостей уходят в TEAM_CHAT_ID (или CLUB_GROUP_ID), админ может снять
заявку командой /team_del <номер>.
"""
import hashlib
import html
import logging
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

from .database import SessionLocal
from .models import ClubEvent, TeamPost, Client, EventGoing, normalize_phone
from . import social, group, referrals
from .bot import is_admin

logger = logging.getLogger("colizeum")
router = Router()


def _deeplink(arg: str) -> str | None:
    return f"https://t.me/{referrals.BOT_USERNAME}?start={arg}" if referrals.BOT_USERNAME else None


def _contact(c: Client) -> str:
    """Как связаться с гостем в Telegram: @username или ссылка на профиль."""
    name = html.escape(c.nickname or c.tg_name or "игрок")
    if c.tg_username:
        return f"{name} — @{html.escape(c.tg_username)}"
    return f'<a href="tg://user?id={c.tg_id}">{name}</a>'


# ---------- афиша ----------

def _event_caption(e: ClubEvent) -> str:
    return (f"{e.title}\n{social.when_text(e.starts_at).capitalize()}"
            + (f"\n\n{e.description}" if e.description else ""))


def _event_admin_markup(e: ClubEvent) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Опубликовать в группе", callback_data=f"ev:pub:{e.id}"),
        InlineKeyboardButton(text="Удалить", callback_data=f"ev:del:{e.id}"),
    ]])


@router.message(Command("event"))
async def cmd_event(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    try:
        d = social.parse_event(command.args or "")
    except social.SocialError as e:
        await message.answer(str(e) + "\n\nПример: фото с подписью\n/event 01.11 20:00 | Турнир CS2 5×5 | Призовой фонд 5000 колизов")
        return
    image = None
    if message.photo:
        sizes = [p for p in message.photo if p.width <= 1280] or message.photo[:1]
        buf = BytesIO()
        await message.bot.download(sizes[-1], destination=buf)
        image = buf.getvalue()
    db = SessionLocal()
    try:
        e = ClubEvent(title=d["title"], description=d["description"], starts_at=d["starts_at"], image=image,
                      image_ver=hashlib.md5(image).hexdigest()[:8] if image else None, status="active")
        db.add(e)
        db.commit()
        db.refresh(e)
        label = social.event_kind(e.title, e.description or "")[1]
        text = ("Событие добавлено в афишу приложения:\n\n" + _event_caption(e)
                + f"\n\nЗначок в афише: {label}" + ("" if image else " (фото не прикреплено — покажем значок)")
                + "\nЗначок подбирается по словам: футбол, хоккей, UFC, CS2, Dota, FIFA, турнир, марафон, кино, квиз, вечеринка…")
        await message.answer(text, reply_markup=_event_admin_markup(e))
    finally:
        db.close()


@router.message(Command("events"))
async def cmd_events(message: Message):
    if not is_admin(message.from_user.id):
        return
    db = SessionLocal()
    try:
        evs = social.events_list(db, None)
        if not evs:
            await message.answer("В афише пусто. Добавить: фото с подписью\n/event 01.11 20:00 | Название | Описание")
            return
        for e in evs:
            ev = db.get(ClubEvent, e["id"])
            await message.answer(f"#{ev.id} " + _event_caption(ev) + f"\n\nПойдут: {e['going']}", reply_markup=_event_admin_markup(ev))
    finally:
        db.close()


@router.callback_query(F.data.startswith("ev:"))
async def cb_event(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    _, act, raw = (callback.data.split(":") + ["", ""])[:3]
    if not raw.isdigit():
        await callback.answer()
        return
    db = SessionLocal()
    try:
        e = db.get(ClubEvent, int(raw))
        if e is None or e.status != "active":
            await callback.answer("Событие уже удалено", show_alert=True)
            return
        if act == "del":
            e.status = "deleted"
            db.commit()
            await callback.answer("Удалено")
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
                await callback.message.answer(f"Событие «{e.title}» удалено из афиши.")
            except Exception:  # noqa: BLE001
                pass
            return
        if act == "pub":
            if not group.CLUB_GROUP_ID:
                await callback.answer("Не задана группа клуба (CLUB_GROUP_ID в Railway)", show_alert=True)
                return
            link = _deeplink(f"event_{e.id}")
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Пойду", url=link)]]) if link else None
            for chat_id in group.CLUB_GROUP_ID:
                try:
                    if e.image:
                        await callback.bot.send_photo(chat_id, BufferedInputFile(e.image, "event.jpg"),
                                                      caption=_event_caption(e), reply_markup=markup)
                    else:
                        await callback.bot.send_message(chat_id, _event_caption(e), reply_markup=markup)
                except Exception as ex:  # noqa: BLE001
                    logger.warning("Афиша: не отправлено в %s: %s", chat_id, ex)
            await callback.answer("Опубликовано")
    finally:
        db.close()


# ---------- тиммейты ----------

async def announce_team(bot: Bot, post: TeamPost, author: Client):
    targets = social.TEAM_CHAT_ID or group.CLUB_GROUP_ID
    if not targets:
        return
    who = "тиммейта" if post.need == 1 else "тиммейтов"
    text = (f"Ищу {post.need} {who}: {post.game}, {social.when_text(post.play_at)}\n"
            f"{author.nickname or 'Гость'}" + (f": «{post.comment}»" if post.comment else ""))
    link = _deeplink(f"team_{post.id}")
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Откликнуться", url=link)]]) if link else None
    for chat_id in targets:
        try:
            await bot.send_message(chat_id, text, reply_markup=markup)
        except Exception as e:  # noqa: BLE001
            logger.warning("Тиммейты: не отправлено в %s: %s", chat_id, e)


async def team_joined(bot: Bot, post: TeamPost, author: Client, joiner: Client):
    what = f"{html.escape(post.game)}, {social.when_text(post.play_at)}"
    try:
        await bot.send_message(author.tg_id, f"В твою команду ({what}) откликнулся {_contact(joiner)}. Напиши, чтобы договориться!",
                               parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.info("Тиммейты: не написали автору %s: %s", author.tg_id, e)
    try:
        await bot.send_message(joiner.tg_id, f"Ты откликнулся на заявку ({what}). Автор — {_contact(author)}, напиши ему.",
                               parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.info("Тиммейты: не написали откликнувшемуся %s: %s", joiner.tg_id, e)


@router.message(Command("team_del"))
async def cmd_team_del(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    raw = (command.args or "").strip().lstrip("#")
    if not raw.isdigit():
        await message.answer("Формат: /team_del 12 — номер заявки")
        return
    db = SessionLocal()
    try:
        n = db.query(TeamPost).filter(TeamPost.id == int(raw)).update({TeamPost.status: "closed"}, synchronize_session=False)
        db.commit()
    finally:
        db.close()
    await message.answer("Заявка снята." if n else "Заявка не найдена.")


@router.message(Command("profile_reset"))
async def cmd_profile_reset(message: Message, command: CommandObject):
    """Очистить «о себе» и ранг гостя (например, за грубость)."""
    if not is_admin(message.from_user.id):
        return
    norm = normalize_phone(command.args or "")
    if len(norm) < 10:
        await message.answer("Формат: /profile_reset +79991234567")
        return
    db = SessionLocal()
    try:
        c = db.query(Client).filter(Client.phone_normalized == norm).first()
        if c is None:
            await message.answer("Гость не найден")
            return
        c.about, c.rank = None, None
        db.commit()
    finally:
        db.close()
    await message.answer("Описание и ранг гостя очищены.")

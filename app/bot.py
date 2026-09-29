"""
Telegram-бот клуба COLIZEUM.

Что делает:
- /start — присылает кнопку, открывающую мини-приложение (личный кабинет).
- Админ-команды (доступны только tg_id из ADMIN_IDS) — временный способ
  вести данные о пополнениях и акциях, пока нет автоматической синхронизации
  с CRM/POS клуба. Как только появится выгрузка или API, эти команды можно
  будет заменить на автоматический импорт, ничего в остальном коде менять
  не придётся.

Бот работает поверх той же базы данных, что и веб-API мини-приложения
(см. main.py) — это один процесс, поэтому отдельный сервис под бота
разворачивать не нужно.
"""
import os

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

from .database import SessionLocal
from .models import Client, Promotion, get_tier, TIER_LABELS

WEBAPP_URL = os.getenv("WEBAPP_URL", "")
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}

router = Router()


def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS


@router.message(Command("start"))
async def cmd_start(message: Message):
    if not WEBAPP_URL:
        await message.answer(
            "Мини-приложение ещё не подключено (не задан WEBAPP_URL). "
            "Как только опубликуешь webapp, добавь адрес в настройки — и эта кнопка заработает."
        )
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Открыть личный кабинет", web_app=WebAppInfo(url=WEBAPP_URL))
        ]]
    )
    await message.answer(
        "Добро пожаловать в COLIZEUM.\n\n"
        "Здесь — твой уровень, акции клуба и мини-игры с призами. "
        "Открывай, когда удобно.",
        reply_markup=keyboard,
    )


@router.message(Command("set_topup"))
async def cmd_set_topup(message: Message, command: CommandObject):
    """Формат: /set_topup +79991234567 12000
    Устанавливает сумму пополнений клиента за месяц вручную (пока нет
    автоматической выгрузки из CRM/POS)."""
    if not is_admin(message.from_user.id):
        return

    if not command.args:
        await message.answer("Формат: /set_topup +79991234567 12000")
        return

    parts = command.args.split()
    if len(parts) != 2:
        await message.answer("Формат: /set_topup +79991234567 12000")
        return

    phone, amount_raw = parts
    try:
        amount = float(amount_raw)
    except ValueError:
        await message.answer("Сумма должна быть числом, например 12000")
        return

    db = SessionLocal()
    try:
        client = db.query(Client).filter(Client.phone == phone).first()
        if not client:
            await message.answer(
                f"Клиент с номером {phone} ещё не открывал мини-приложение — "
                f"он появится в базе после первого захода."
            )
            return
        client.monthly_topup = amount
        db.commit()
        tier = get_tier(amount)
        await message.answer(
            f"Готово: {phone} — {amount:.0f} ₽ за месяц, уровень {TIER_LABELS[tier]}."
        )
    finally:
        db.close()


@router.message(Command("add_promo"))
async def cmd_add_promo(message: Message, command: CommandObject):
    """Формат: /add_promo Название | Описание | ссылка (необязательно)
    Акции видны всем гостям клуба независимо от уровня."""
    if not is_admin(message.from_user.id):
        return

    if not command.args:
        await message.answer("Формат: /add_promo Название | Описание | ссылка (необязательно)")
        return

    parts = [p.strip() for p in command.args.split("|")]
    if len(parts) < 2:
        await message.answer("Формат: /add_promo Название | Описание | ссылка (необязательно)")
        return

    title, description = parts[0], parts[1]
    link = parts[2] if len(parts) > 2 and parts[2] else None

    db = SessionLocal()
    try:
        promo = Promotion(title=title, description=description, link=link)
        db.add(promo)
        db.commit()
        await message.answer(f"Акция добавлена: «{title}»" + (f"\nСсылка: {link}" if link else ""))
    finally:
        db.close()


@router.message(Command("promotions"))
async def cmd_list_promotions(message: Message):
    if not is_admin(message.from_user.id):
        return

    db = SessionLocal()
    try:
        promos = db.query(Promotion).filter(Promotion.active == True).all()  # noqa: E712
        if not promos:
            await message.answer("Активных акций пока нет. Добавь: /add_promo Название | Описание")
            return
        lines = [f"#{p.id} {p.title} — {p.description}" for p in promos]
        await message.answer("\n".join(lines))
    finally:
        db.close()


def build_bot_and_dispatcher() -> tuple[Bot, Dispatcher]:
    bot_token = os.getenv("BOT_TOKEN", "")
    if not bot_token:
        raise RuntimeError("Не задан BOT_TOKEN — возьми токен у @BotFather и добавь в .env")

    bot = Bot(token=bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp

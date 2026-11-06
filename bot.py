import asyncio
import json
import logging
import os
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database import Order, OrderStatus, Package, User, async_session, init_db

# Конфигурация (вынеси в .env в продакшене)
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "-1001234567890"))  # ID админ-группы или лички админа
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-domain-or-github-pages.io/index.html")
CARD_DETAILS = "💳 <b>4255 1900 0000 0000</b> (Альфа-Банк / BYN)\nПолучатель: Арсений М."

user_router = Router(name="user_router")
admin_router = Router(name="admin_router")


# --- Фабрики CallbackData ---
class PaidClickCallback(CallbackData, prefix="paid"):
    order_id: int


class SkipReceiptCallback(CallbackData, prefix="skip_rec"):
    order_id: int


class AdminOrderCallback(CallbackData, prefix="adm_ord"):
    order_id: int
    action: str  # "approve" | "reject"


# --- Состояния FSM ---
class PaymentState(StatesGroup):
    waiting_for_receipt = State()


# --- Вспомогательная функция создания заказа ---
async def create_order_and_send_requisites(bot: Bot, user_id: int, package_id: str):
    async with async_session() as session:
        pkg = await session.get(Package, package_id)
        if not pkg:
            await bot.send_message(user_id, "❌ Тариф не найден.")
            return

        order = Order(user_id=user_id, package_id=pkg.id, status=OrderStatus.PENDING)
        session.add(order)
        await session.commit()

        text = (
            f"📦 Вы выбрали: <b>{pkg.title}</b>\n"
            f"💰 Сумма к оплате: <b>{pkg.price} BYN</b>\n\n"
            f"Реквизиты для перевода:\n{CARD_DETAILS}\n\n"
            f"⚠️ <i>Обратите внимание: проверка оплат не производится с 20:00 до 10:00–11:00 утра. "
            f"При оплате в ночное время доступ будет выдан утром.</i>\n\n"
            f"После перевода нажмите кнопку <b>«Я оплатил»</b> ниже:"
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Я оплатил",
                        callback_data=PaidClickCallback(order_id=order.id).pack(),
                    )
                ]
            ]
        )
        await bot.send_message(user_id, text, reply_markup=kb)


# ==========================================
# 1. ПОЛЬЗОВАТЕЛЬСКИЕ ХЭНДЛЕРЫ
# ==========================================

@user_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()

    async with async_session() as session:
        user = await session.get(User, message.from_user.id)
        if not user:
            user = User(
                id=message.from_user.id,
                username=message.from_user.username,
                full_name=message.from_user.full_name,
            )
            session.add(user)
            await session.commit()

    # Reply-кнопка позволяет использовать Telegram.WebApp.sendData() без бэкенд-сервера
    reply_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛍 Открыть магазин", web_app=WebAppInfo(url=WEBAPP_URL))]
        ],
        resize_keyboard=True,
    )

    # Дублируем Inline-кнопку (на случай если подключен FastAPI-бэкенд)
    inline_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛍 Открыть магазин", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]
    )

    await message.answer(
        f"Привет, <b>{message.from_user.first_name}</b>! 👋\n\n"
        "Добро пожаловать в бот обучения выкупу и работе с карго от Арсения Матвеева.\n"
        "Нажми на кнопку <b>«Открыть магазин»</b>, чтобы ознакомиться с программой и выбрать тариф:",
        reply_markup=reply_kb,
    )


@user_router.message(F.web_app_data)
async def handle_web_app_data(message: Message, bot: Bot):
    """Прием данных из Mini App через Telegram.WebApp.sendData()."""
    try:
        data = json.loads(message.web_app_data.data)
        if data.get("action") == "create_order":
            package_id = data.get("package_id")
            await create_order_and_send_requisites(bot, message.from_user.id, package_id)
    except Exception as e:
        logging.error(f"Ошибка парсинга web_app_data: {e}")
        await message.answer("❌ Произошла ошибка при обработке заявки.")


@user_router.callback_query(PaidClickCallback.filter())
async def on_user_paid_click(
    callback: CallbackQuery, callback_data: PaidClickCallback, state: FSMContext
):
    """Пользователь нажал 'Я оплатил'. Запрашиваем опциональный скриншот чека."""
    await state.set_state(PaymentState.waiting_for_receipt)
    await state.update_data(order_id=callback_data.order_id)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отправить заявку без скриншота ➡️",
                    callback_data=SkipReceiptCallback(order_id=callback_data.order_id).pack(),
                )
            ]
        ]
    )

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "📎 Пришлите <b>скриншот (фото) чека</b> об оплате в ответ на это сообщение для быстрой проверки "
        "или нажмите кнопку ниже, чтобы отправить заявку без чека:",
        reply_markup=kb,
    )
    await callback.answer()


async def send_order_to_admin(bot: Bot, order_id: int, receipt_file_id: Optional[str] = None):
    """Формирование и отправка карточки заявки в админский чат."""
    async with async_session() as session:
        stmt = (
            select(Order)
            .options(selectinload(Order.user), selectinload(Order.package))
            .where(Order.id == order_id)
        )
        order = await session.scalar(stmt)
        if not order:
            return

        if receipt_file_id:
            order.receipt_file_id = receipt_file_id
            await session.commit()

        username_str = f"@{order.user.username}" if order.user.username else "нет username"
        admin_text = (
            f"🔔 <b>Новая заявка на оплату #{order.id}</b>\n\n"
            f"👤 <b>Пользователь:</b> {order.user.full_name} ({username_str})\n"
            f"🆔 <b>ID:</b> <code>{order.user.id}</code>\n"
            f"📦 <b>Тариф:</b> {order.package.title} ({order.package.price} BYN)\n"
            f"🧾 <b>Чек:</b> {'Прикреплен 👇' if receipt_file_id else 'Не прикреплен'}"
        )

        admin_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить выдачу",
                        callback_data=AdminOrderCallback(order_id=order.id, action="approve").pack(),
                    ),
                    InlineKeyboardButton(
                        text="❌ Отклонить",
                        callback_data=AdminOrderCallback(order_id=order.id, action="reject").pack(),
                    ),
                ]
            ]
        )

        if receipt_file_id:
            await bot.send_photo(
                chat_id=ADMIN_CHAT_ID,
                photo=receipt_file_id,
                caption=admin_text,
                reply_markup=admin_kb,
            )
        else:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=admin_text,
                reply_markup=admin_kb,
            )


@user_router.message(PaymentState.waiting_for_receipt, F.photo)
async def receive_receipt_photo(message: Message, state: FSMContext, bot: Bot):
    """Прием фото чека от пользователя."""
    data = await state.get_data()
    order_id = data.get("order_id")
    photo_file_id = message.photo[-1].file_id

    await state.clear()
    await send_order_to_admin(bot, order_id, receipt_file_id=photo_file_id)
    await message.answer(
        "✅ <b>Ваша заявка и чек отправлены на проверку!</b>\n"
        "Как только администратор подтвердит платеж, бот автоматически пришлет вам ссылки."
    )


@user_router.callback_query(PaymentState.waiting_for_receipt, SkipReceiptCallback.filter())
async def skip_receipt_handler(
    callback: CallbackQuery, callback_data: SkipReceiptCallback, state: FSMContext, bot: Bot
):
    """Отправка заявки без чека."""
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await send_order_to_admin(bot, callback_data.order_id, receipt_file_id=None)
    await callback.message.answer(
        "✅ <b>Заявка отправлена на проверку!</b>\n"
        "Ожидайте подтверждения от администратора."
    )
    await callback.answer()


# ==========================================
# 2. АДМИНСКИЕ CALLBACK-ХЭНДЛЕРЫ
# ==========================================

@admin_router.callback_query(AdminOrderCallback.filter())
async def handle_admin_decision(
    callback: CallbackQuery, callback_data: AdminOrderCallback, bot: Bot
):
    async with async_session() as session:
        stmt = (
            select(Order)
            .options(selectinload(Order.user), selectinload(Order.package))
            .where(Order.id == callback_data.order_id)
        )
        order = await session.scalar(stmt)

        if not order:
            await callback.answer("Заявка не найдена в БД!", show_alert=True)
            return

        # Защита от повторного нажатия (Race Condition / Double Click)
        if order.status != OrderStatus.PENDING:
            await callback.answer(
                f"Эта заявка уже обработана (статус: {order.status.value})!",
                show_alert=True,
            )
            return

        admin_name = callback.from_user.full_name

        if callback_data.action == "approve":
            order.status = OrderStatus.APPROVED
            await session.commit()

            # Формируем ссылки в зависимости от тарифа (1 для Base/Plus, 2 для VIP)
            links_Formatted = "\n".join(
                [f"👉 <a href='{item['url']}'>{item['name']}</a>" for item in order.package.links]
            )
            user_msg = (
                f"🎉 <b>Оплата по тарифу «{order.package.title}» подтверждена!</b>\n\n"
                f"Ваши материалы доступны по ссылкам ниже:\n{links_Formatted}\n\n"
                f"Желаем продуктивного обучения!"
            )

            try:
                await bot.send_message(
                    chat_id=order.user_id,
                    text=user_msg,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logging.error(f"Не удалось отправить ссылки юзеру {order.user_id}: {e}")

            status_footer = f"\n\n✅ <b>Подтверждено администратором:</b> {admin_name}"

        else:
            order.status = OrderStatus.REJECTED
            await session.commit()

            try:
                await bot.send_message(
                    chat_id=order.user_id,
                    text=(
                        f"❌ <b>Ваша заявка на тариф «{order.package.title}» была отклонена.</b>\n"
                        "Если вы считаете, что произошла ошибка, свяжитесь с поддержкой."
                    ),
                )
            except Exception as e:
                logging.error(f"Не удалось уведомить юзера {order.user_id}: {e}")

            status_footer = f"\n\n❌ <b>Отклонено администратором:</b> {admin_name}"

        # Обновляем сообщение в админ-чате (убираем кнопки и дописываем статус)
        if callback.message.photo:
            await callback.message.edit_caption(
                caption=(callback.message.caption or "") + status_footer,
                reply_markup=None,
            )
        else:
            await callback.message.edit_text(
                text=(callback.message.text or "") + status_footer,
                reply_markup=None,
            )

        await callback.answer("Статус заявки обновлен!")


# ==========================================
# ТОЧКА ВХОДА
# ==========================================

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(user_router)
    dp.include_router(admin_router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

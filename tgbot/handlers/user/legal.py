"""Документы и действующие цены доступны в любом состоянии личного бота."""
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database import tariff_repo
from loader import logger
from tgbot.keyboards.inline import legal_menu_keyboard
from tgbot.services import device_slot_service
from utils.legal import (
    DOCUMENT_DATE, PRIVACY, TERMS, REVIEW_PHRASE,
    document_messages, document_plaintext, platega_review_mode, public_tariff_rows,
)
from utils.telegram_ui import replace_message_text

legal_router = Router(name="legal")


async def show_documents(event: Message | CallbackQuery, state: FSMContext):
    await state.set_state(None)
    text = (
        "📄 <b>Документы и тарифы HamzAVPN</b>\n\n"
        f"Редакция документов: {DOCUMENT_DATE}.\n"
        "Перед оплатой доступны условия услуги, правила обработки данных и действующие цены в RUB.\n\n"
        "💬 Поддержка — личный тикет внутри бота. Нажмите «Поддержка» или отправьте /support; вступать в группу не требуется.\n\n"
        "Полные документы отправляются прямо в этот чат: весь текст по частям и файл для сохранения. "
        "Открыть их можно в любое время командой /documents и кнопкой в главном меню."
    )
    if platega_review_mode():
        text += f"\n\n{REVIEW_PHRASE}"
    if isinstance(event, CallbackQuery):
        await event.answer()
        await replace_message_text(event.message, text, reply_markup=legal_menu_keyboard())
    else:
        await event.answer(text, reply_markup=legal_menu_keyboard())


@legal_router.message(Command("documents"))
async def documents_command(message: Message, state: FSMContext):
    await show_documents(message, state)


@legal_router.callback_query(F.data == "documents")
async def documents_callback(call: CallbackQuery, state: FSMContext):
    await show_documents(call, state)


async def send_document(event: Message | CallbackQuery, state: FSMContext, document):
    await state.set_state(None)
    message = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        await event.answer()
    parts = document_messages(document)
    for text in parts:
        await message.answer(text, reply_markup=legal_menu_keyboard())
    document_name = "privacy" if document is PRIVACY else "terms"
    revision = "-".join(reversed(DOCUMENT_DATE.split(".")))
    await message.answer_document(
        BufferedInputFile(
            document_plaintext(document).encode("utf-8"),
            filename=f"hamzavpn-{document_name}-{revision}.txt",
        ),
        caption=f"{document.title} — HamzAVPN\nПолный текст для сохранения. Редакция: {DOCUMENT_DATE}.",
        reply_markup=legal_menu_keyboard(),
    )


@legal_router.message(Command("privacy"))
async def privacy_command(message: Message, state: FSMContext):
    await send_document(message, state, PRIVACY)


@legal_router.callback_query(F.data == "privacy_policy")
async def privacy_callback(call: CallbackQuery, state: FSMContext):
    await send_document(call, state, PRIVACY)


@legal_router.message(Command("terms"))
async def terms_command(message: Message, state: FSMContext):
    await send_document(message, state, TERMS)


@legal_router.callback_query(F.data == "terms_of_service")
async def terms_callback(call: CallbackQuery, state: FSMContext):
    await send_document(call, state, TERMS)


async def show_tariffs(event: Message | CallbackQuery, state: FSMContext):
    await state.set_state(None)
    message = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        await event.answer()
    try:
        active_tariffs = await tariff_repo.get_active()
        if active_tariffs:
            settings = await device_slot_service.settings()
            rows = public_tariff_rows(active_tariffs, settings)
        else:
            rows = []
    except Exception:
        logger.exception("Не удалось загрузить тарифы для документов бота")
        await message.answer("Тарифы временно недоступны. Проверьте цены позже или обратитесь в поддержку.", reply_markup=legal_menu_keyboard())
        return
    if not rows:
        await message.answer(
            "Продажи подписок пока не открыты: активные тарифы не настроены. Здесь появятся действующие цены после их настройки.",
            reply_markup=legal_menu_keyboard(),
        )
        return
    intro = (
        "💎 <b>Тарифы HamzAVPN</b>\n\n"
        "Подписка на VPN-сервис для защищенного сетевого соединения: доступ на оплаченный срок, ссылка подключения и личная поддержка.\n"
        "Оплата разовая через Platega, без автоматических списаний. Все цены в российских рублях (RUB). "
        "Промокод и дополнительные устройства учитываются в итоговой сумме перед оплатой."
    )
    if platega_review_mode():
        intro += f"\n\n{REVIEW_PHRASE}"
    await message.answer(intro)
    for tariff, row in zip(active_tariffs, rows):
        text = (
            f"<b>{escape(row['name'])}</b>\n"
            f"Цена: <b>{row['price']} RUB</b>\n"
            f"Срок: {row['duration_days']} дней\n"
            f"Включено устройств: {row['base_limit']}\n"
            f"Трафик: {row['quota']}"
        )
        if row["loyalty_price"]:
            text += f"\nПри продлении до окончания подписки: {row['loyalty_price']} RUB"
        if row["price_stars"]:
            text += f"\nВ Telegram без дополнительных устройств: {row['price_stars']} XTR (Telegram Stars), если способ доступен."
        if row["max_extra"] > 0:
            text += f"\nДополнительное устройство на срок тарифа: {row['extra_price']} RUB (до {row['max_extra']} дополнительных устройств)."
        text += "\n\nНажмите «Оплатить», чтобы проверить состав заказа и итоговую сумму перед переходом к оплате."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {row['price']} ₽", callback_data=f"select_tariff_{tariff.id}")],
            [InlineKeyboardButton(text="📄 Документы и тарифы", callback_data="documents")],
            [InlineKeyboardButton(text="💬 Личная поддержка", callback_data="support_chat_start")],
        ])
        await message.answer(text, reply_markup=keyboard)


@legal_router.message(Command("tariffs"))
async def tariffs_command(message: Message, state: FSMContext):
    await show_tariffs(message, state)


@legal_router.callback_query(F.data == "public_tariffs")
async def tariffs_callback(call: CallbackQuery, state: FSMContext):
    await show_tariffs(call, state)

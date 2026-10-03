# tgbot/handlers/user/link_email.py

import re
import random
import string
import logging
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext

from tgbot.states.email_link_states import EmailLinkFSM
from tgbot.keyboards.inline import main_menu_keyboard, back_to_main_menu_keyboard
from database import user_repo
from webapp.core.security import get_password_hash
from webapp.core.mail import send_verification_email, MailSendError

link_email_router = Router(name="link_email")
EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$')


@link_email_router.callback_query(F.data == "link_email")
async def start_link_email(callback: CallbackQuery, state: FSMContext):
    user = await user_repo.get(callback.from_user.id)
    if user and user.email:
        await callback.answer("У вас уже привязан Email!", show_alert=True)
        return

    await state.set_state(EmailLinkFSM.awaiting_email)
    await callback.message.answer(
        "📧 <b>Привязка Email</b>\n\n"
        "Введите ваш email-адрес.\n"
        "На него будет отправлен код подтверждения.",
        parse_mode="HTML",
        reply_markup=back_to_main_menu_keyboard()
    )
    await callback.answer()


@link_email_router.message(EmailLinkFSM.awaiting_email)
async def process_email(message: Message, state: FSMContext):
    email = message.text.strip().lower()

    if not EMAIL_REGEX.match(email):
        await message.answer(
            "❌ Неверный формат email. Попробуйте ещё раз.",
            reply_markup=back_to_main_menu_keyboard()
        )
        return

    # Проверяем, не занят ли email
    existing = await user_repo.get_by_email(email)
    if existing:
        await message.answer(
            "❌ Этот email уже используется. Используйте другой email или войдите на сайт.",
            reply_markup=back_to_main_menu_keyboard()
        )
        return

    # Генерируем код
    code = ''.join(random.choices(string.digits, k=6))

    # Отправляем на email
    try:
        await send_verification_email(email, code)
    except MailSendError as e:
        await message.answer(
            f"❌ {e}\nПопробуйте позже.",
            reply_markup=back_to_main_menu_keyboard()
        )
        return

    await state.update_data(
        email=email,
        code=code,
        code_sent_at=datetime.now().isoformat(),
        attempts=0
    )
    await state.set_state(EmailLinkFSM.awaiting_code)

    await message.answer(
        f"✅ Код подтверждения отправлен на <b>{email}</b>\n\n"
        "Введите 6-значный код из письма:",
        parse_mode="HTML",
        reply_markup=back_to_main_menu_keyboard()
    )


@link_email_router.message(EmailLinkFSM.awaiting_code)
async def process_code(message: Message, state: FSMContext):
    data = await state.get_data()
    code_input = message.text.strip()
    attempts = data.get("attempts", 0)

    if attempts >= 5:
        await state.clear()
        await message.answer(
            "❌ Превышено количество попыток. Начните заново.",
            reply_markup=back_to_main_menu_keyboard()
        )
        return

    if code_input != data["code"]:
        await state.update_data(attempts=attempts + 1)
        remaining = 5 - (attempts + 1)
        await message.answer(
            f"❌ Неверный код. Осталось попыток: {remaining}",
            reply_markup=back_to_main_menu_keyboard()
        )
        return

    # Код верный — переходим к вводу пароля
    await state.set_state(EmailLinkFSM.awaiting_password)
    await message.answer(
        "✅ Email подтверждён!\n\n"
        "Теперь придумайте пароль для входа на сайт (минимум 6 символов):",
        reply_markup=back_to_main_menu_keyboard()
    )


@link_email_router.message(EmailLinkFSM.awaiting_password)
async def process_password(message: Message, state: FSMContext):
    password = message.text.strip()

    if len(password) < 6:
        await message.answer(
            "❌ Пароль должен содержать минимум 6 символов. Попробуйте ещё раз:",
            reply_markup=back_to_main_menu_keyboard()
        )
        return

    data = await state.get_data()
    email = data["email"]
    hashed = get_password_hash(password)

    # Повторная проверка что email не заняли пока пользователь вводил код
    existing = await user_repo.get_by_email(email)
    if existing:
        await state.clear()
        await message.answer(
            "❌ Этот email уже был привязан к другому аккаунту. Попробуйте другой email.",
            reply_markup=back_to_main_menu_keyboard()
        )
        return

    # Сохраняем email и пароль
    await user_repo.set_email_and_password(message.from_user.id, email, hashed)
    await state.clear()

    # Удаляем сообщение с паролем для безопасности
    try:
        await message.delete()
    except Exception:
        pass

    user = await user_repo.get(message.from_user.id)
    await message.answer(
        f"✅ <b>Email успешно привязан!</b>\n\n"
        f"📧 {email}\n\n"
        "Теперь вы можете входить на сайт с этим email и паролем.",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(has_email=bool(user and user.email))
    )

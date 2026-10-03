# tgbot/handlers/user/revoke_key.py
"""
Экран "Перевыпустить ключ": сменить ссылку подписки, если она утекла.

Пара к "Моим устройствам": отвязка устройства освобождает слот, а перевыпуск
отбирает доступ у того, кто скопировал саму ссылку — отвязкой это не лечится,
потому что чужой клиент просто зарегистрируется заново.
"""
from aiogram import F, Router
from aiogram.types import CallbackQuery

from tgbot.keyboards.inline import (
    back_to_main_menu_keyboard,
    revoke_key_confirm_keyboard,
    revoked_key_keyboard,
)
from tgbot.services import key_service
from utils.telegram_ui import replace_message_text

revoke_key_router = Router()


@revoke_key_router.callback_query(F.data == "revoke_key")
@revoke_key_router.callback_query(F.data == "revoke_key:dev")
async def revoke_key_confirm(call: CallbackQuery):
    """Предупреждение перед перевыпуском — действие необратимое для всех устройств."""
    await call.answer()
    back = "my_devices" if call.data.endswith(":dev") else "my_keys"

    await replace_message_text(
        call.message,
        "♻️ <b>Перевыпустить ключ?</b>\n\n"
        "Пригодится, если ссылка на подписку попала к посторонним: "
        "старая ссылка перестанет работать у всех разом.\n\n"
        "⚠️ <b>Ваши устройства тоже отключатся.</b> На каждом из них "
        "подписку потребуется добавить заново — уже по новой ссылке.\n\n"
        "Срок подписки, трафик и тариф не меняются.",
        reply_markup=revoke_key_confirm_keyboard(back),
    )


@revoke_key_router.callback_query(F.data == "revoke_key_ok")
async def revoke_key_apply(call: CallbackQuery):
    await call.answer()
    loading = await replace_message_text(
        call.message,
        "⏳ <b>Перевыпускаем ключ…</b>\n\n"
        "Меняем ссылку подписки в VPN-панели.",
    )

    result = await key_service.revoke(call.from_user.id)

    if not result.ok:
        await replace_message_text(
            loading,
            f"❌ {result.error}",
            reply_markup=back_to_main_menu_keyboard(),
        )
        return

    released = ""
    if result.devices_released:
        released = (
            f"\nОтвязано устройств: <b>{result.devices_released}</b> — "
            f"слоты освободились.\n"
        )

    text = (
        "✅ <b>Ключ перевыпущен</b>\n\n"
        "Старая ссылка больше не работает.\n"
        f"{released}\n"
        "🔗 <b>Новая ссылка подключения:</b>\n"
        f"<code>{result.subscription_url}</code>\n\n"
        "Добавьте её в приложение на каждом своём устройстве."
    )

    # Ссылки может не быть, если панель вернула пользователя без
    # subscriptionUrl — кнопка с пустым url уронила бы всю клавиатуру.
    markup = (
        revoked_key_keyboard(result.subscription_url)
        if result.subscription_url
        else back_to_main_menu_keyboard()
    )
    await replace_message_text(loading, text, reply_markup=markup)

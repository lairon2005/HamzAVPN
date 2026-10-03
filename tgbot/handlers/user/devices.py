# tgbot/handlers/user/devices.py
"""
Экран "Мои устройства": показать привязанные устройства и освободить слот.

Нужен как пара к hwidDeviceLimit в панели — без него человек, упёршийся в
лимит, не может подключить новый телефон и идёт в поддержку.
"""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from tgbot.keyboards.inline import (
    DEVICES_PER_PAGE,
    back_to_main_menu_keyboard,
    device_delete_confirm_keyboard,
    devices_keyboard,
)
from tgbot.services import device_service, device_slot_service
from utils.telegram_ui import replace_message_text

devices_router = Router()


def _render(device_list, page: int, extra_devices: int = 0, can_buy: bool = False) -> str:
    """Текст экрана со списком устройств текущей страницы."""
    header = ["📱 <b>Мои устройства</b>\n"]

    if device_list.limit:
        slots_note = f" ({extra_devices} доп. оплачено)" if extra_devices else ""
        header.append(
            f"Занято слотов: <b>{device_list.used} из {device_list.limit}</b>{slots_note}\n"
        )
        if device_list.is_full:
            header.append(
                "⚠️ Лимит исчерпан. Удалите одно устройство из списка"
                + (" или докупите слот.\n" if can_buy else ".\n")
            )
    else:
        header.append(f"Подключено устройств: <b>{device_list.used}</b>\n")

    start = page * DEVICES_PER_PAGE
    chunk = device_list.devices[start:start + DEVICES_PER_PAGE]

    lines = []
    for i, device in enumerate(chunk, start=start + 1):
        lines.append(
            f"\n<b>{i}. {device.title}</b>\n"
            f"    {device.app} · {device.platform}\n"
            f"    последний вход: {device.last_seen_display}"
        )

    footer = (
        "\n\nНажмите на устройство, чтобы отвязать его. "
        "Подписка при этом сохраняется — устройство просто "
        "потребуется добавить заново."
    )
    return "".join(header) + "".join(lines) + footer


async def _show_devices(message: Message, user_id: int, page: int = 0) -> None:
    device_list = await device_service.list_devices(user_id)

    if device_list.error:
        await replace_message_text(
            message, device_list.error, reply_markup=back_to_main_menu_keyboard()
        )
        return

    if not device_list.devices:
        await replace_message_text(
            message,
            "📱 <b>Мои устройства</b>\n\n"
            "Пока ни одного устройства не подключено. "
            "Добавьте подписку в приложение — устройство появится здесь.",
            reply_markup=back_to_main_menu_keyboard(),
        )
        return

    # Страница могла исчезнуть, пока пользователь смотрел на неё (например,
    # удалил последнее устройство на ней) — откатываемся на существующую.
    pages = max(1, (len(device_list.devices) + DEVICES_PER_PAGE - 1) // DEVICES_PER_PAGE)
    page = max(0, min(page, pages - 1))

    # Докупка доступна не всем: нужна активная оплаченная подписка и незанятый
    # потолок слотов — это и решает quote().
    quote = await device_slot_service.quote(user_id, slots=1)

    await replace_message_text(
        message,
        _render(device_list, page, extra_devices=quote.current_extra, can_buy=quote.ok),
        reply_markup=devices_keyboard(device_list.devices, page, can_buy_slots=quote.ok),
    )


@devices_router.message(Command("devices"))
async def devices_command_handler(message: Message):
    loading = await message.answer("⏳ <b>Загружаем список устройств…</b>")
    await _show_devices(loading, message.from_user.id)


@devices_router.callback_query(F.data == "my_devices")
@devices_router.callback_query(F.data.startswith("my_devices:"))
async def devices_callback_handler(call: CallbackQuery):
    await call.answer()
    page = 0
    if ":" in call.data:
        try:
            page = int(call.data.split(":", 1)[1])
        except ValueError:
            page = 0

    loading = await replace_message_text(
        call.message,
        "⏳ <b>Загружаем список устройств…</b>\n\n"
        "Получаем данные из VPN-панели.",
    )
    await _show_devices(loading, call.from_user.id, page)


@devices_router.callback_query(F.data.startswith("dev_del:"))
async def device_delete_confirm(call: CallbackQuery):
    await call.answer()
    key = call.data.split(":", 1)[1]

    device_list = await device_service.list_devices(call.from_user.id)
    if device_list.error:
        await replace_message_text(
            call.message, device_list.error, reply_markup=back_to_main_menu_keyboard()
        )
        return

    device = next((d for d in device_list.devices if d.key == key), None)
    if not device:
        await _show_devices(call.message, call.from_user.id)
        return

    page = next(
        (i // DEVICES_PER_PAGE for i, d in enumerate(device_list.devices) if d.key == key),
        0,
    )

    await replace_message_text(
        call.message,
        f"🗑 <b>Отвязать устройство?</b>\n\n"
        f"<b>{device.title}</b>\n"
        f"{device.app} · {device.platform}\n"
        f"добавлено: {device.added_display}\n"
        f"последний вход: {device.last_seen_display}\n\n"
        f"Слот освободится сразу. На самом устройстве подписку "
        f"нужно будет добавить заново.",
        reply_markup=device_delete_confirm_keyboard(key, page),
    )


@devices_router.callback_query(F.data.startswith("dev_del_ok:"))
async def device_delete_apply(call: CallbackQuery):
    key = call.data.split(":", 1)[1]
    ok, error = await device_service.delete_device(call.from_user.id, key)

    await call.answer("Устройство отвязано" if ok else (error or "Не удалось"), show_alert=not ok)
    await _show_devices(call.message, call.from_user.id)


@devices_router.callback_query(F.data == "noop")
async def devices_noop(call: CallbackQuery):
    """Счётчик страниц — кнопка нужна только для вида."""
    await call.answer()

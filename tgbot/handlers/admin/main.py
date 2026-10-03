# tgbot/handlers/admin/main.py
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from tgbot.filters.admin import IsAdmin
from tgbot.keyboards.inline import admin_main_menu_keyboard
from tgbot.services import admin_stats_service
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loader import logger

admin_main_router = Router()
admin_main_router.message.filter(IsAdmin()) # Применяем фильтр ко всем хендлерам в этом роутере

@admin_main_router.message(Command("admin"))
async def admin_start(message: Message):
    await message.answer("Добро пожаловать в админ-панель!", reply_markup=admin_main_menu_keyboard())

@admin_main_router.callback_query(F.data == "admin_main_menu")
async def admin_main_menu(call: CallbackQuery):
    await call.message.edit_text("Добро пожаловать в админ-панель!", reply_markup=admin_main_menu_keyboard())

@admin_main_router.callback_query(F.data == "admin_stats")
async def admin_stats_handler(call: CallbackQuery):
    """Показывает расширенную статистику с разбивкой по узлам."""
    await call.answer("Собираю статистику с серверов...")

    # --- Получаем все данные через сервис ---
    stats = await admin_stats_service.get_dashboard_stats()

    total_users = stats["total_users"]
    active_subs = stats["active_subs"]
    first_payments_total = stats["first_payments"]
    users_today = stats["users_today"]
    users_week = stats["users_week"]
    users_month = stats["users_month"]
    nodes = stats["nodes"]

    # --- 2. Формируем текст ---

    text_parts = [
        "📊 <b>Расширенная статистика</b>\n",
        "<b>Пользователи:</b>",
        f"├ Всего в боте: 👥<b>{total_users}</b>",
        f"├ За сегодня: <b>{users_today}</b>\n"
        f"├ За неделю: <b>{users_week}</b>\n"
        f"├ За месяц: <b>{users_month}</b>\n"
        f"└ Активных подписок: ✅<b>{active_subs}</b>",
        "", # Пустая строка для отступа
        "<b>Конверсия:</b>",
        f"└ Всего первых оплат: <b>{first_payments_total}</b>",
        "",
        "<b>Доход (календарь, МСК):</b>",
        f"├ Сегодня: <b>{stats['revenue_today']['revenue']:.2f} ₽</b> ({stats['revenue_today']['count']})",
        f"├ Неделя: <b>{stats['revenue_week']['revenue']:.2f} ₽</b> ({stats['revenue_week']['count']})",
        f"├ Месяц: <b>{stats['revenue_month']['revenue']:.2f} ₽</b> ({stats['revenue_month']['count']})",
        f"├ Год: <b>{stats['revenue_year']['revenue']:.2f} ₽</b> ({stats['revenue_year']['count']})",
        f"└ Всего: <b>{stats['revenue_total']['revenue']:.2f} ₽</b> ({stats['revenue_total']['count']})",
        f"⭐ Stars: <b>{stats['stars_revenue_total']['stars']:.0f} ⭐</b> ({stats['stars_revenue_total']['count']})",
        "",
        "<b>Сервер VPN:</b>",
        f"└ 🖥️ Онлайн сейчас: <b>{stats['online_now']}</b>\n",
        "<b>Подключенные узлы (Nodes):</b>",
    ]

    # Показываем список узлов, их статус подключения и загрузку CPU/RAM
    if nodes:
        for i, node in enumerate(nodes):
            node_name = node.get('name') or f"Узел #{i+1}"
            is_connected = node.get('is_connected') or node.get('is_online')
            status_icon = "✅" if is_connected else "❌"

            users_online = node.get('users_online', 0)
            line = f"👥 {users_online}"

            cpu_usage = node.get('cpu_usage')
            if cpu_usage is not None:
                line += f" · CPU {cpu_usage:.0f}%"
            ram_usage = node.get('ram_usage')
            if ram_usage is not None:
                line += f" · RAM {ram_usage:.0f}%"

            is_last = (i == len(nodes) - 1)
            prefix = "└─" if is_last else "├─"

            text_parts.append(f"{prefix} {status_icon} {node_name}: {line}")
    else:
        text_parts.append("└─ 🤷‍♂️ Внешние узлы не настроены.")

    text = "\n".join(text_parts)

    # Клавиатура остается такой же
    stats_kb = InlineKeyboardBuilder()
    stats_kb.button(text="📊 Когорты / метрики", callback_data="admin_cohort_metrics")
    stats_kb.button(text="🔄 Обновить", callback_data="admin_stats")
    stats_kb.button(text="⬅️ Назад", callback_data="admin_main_menu")
    stats_kb.adjust(1)

    try:
        # Пытаемся отредактировать сообщение
        await call.message.edit_text(text, reply_markup=stats_kb.as_markup())
    except TelegramBadRequest as e:
        # Повторно отвечать на callback здесь НЕЛЬЗЯ: query уже отвечен в начале
        # хендлера (call.answer выше), а сбор статистики занимает время — к этому
        # моменту query часто уже устаревает, и call.answer() падал с
        # "query is too old and response timeout expired or query ID is invalid".
        if "message is not modified" in e.message:
            # Данные не изменились с прошлого обновления — это нормально, не ошибка.
            logger.debug(f"Stats refresh: content unchanged for admin {call.from_user.id}.")
        else:
            # Любая другая ошибка редактирования — только логируем.
            logger.error(f"Error editing stats message: {e}")


@admin_main_router.callback_query(F.data == "admin_cohort_metrics")
async def admin_cohort_metrics_handler(call: CallbackQuery):
    """
    Мини-дашборд когорт продлений — 7 еженедельных метрик из стратегии
    монетизации. Первая задача после автопродления: агрегатная статистика
    прячет утечку пользователей, поэтому здесь именно когортные показатели.
    """
    await call.answer("Считаю когорты продлений...")

    metrics = await admin_stats_service.get_cohort_metrics()

    def icon(achieved: bool) -> str:
        return "✅" if achieved else "⚠️"

    mrr = metrics["mrr"]
    renewal = metrics["renewal_rate"]
    first_pay = metrics["first_payments"]
    inflow = metrics["inflow"]
    k_factor = metrics["k_factor"]
    long_tariff = metrics["long_tariff_share"]
    winback = metrics["winback_returns"]

    winback_value_text = (
        "н/д (подсистема win-back ещё не подключена)"
        if winback["value"] is None
        else str(winback["value"])
    )

    text_parts = [
        "📊 <b>Когорты продлений — еженедельные метрики</b>\n",

        f"{icon(mrr['achieved'])} <b>1. MRR</b>",
        f"   {mrr['value']:.2f} RUB (цель ≥ {mrr['target']:,} RUB)\n".replace(",", " "),

        f"{icon(renewal['achieved'])} <b>2. Доля продлений</b>",
        f"   {renewal['value'] * 100:.1f}% — продлили {renewal['renewed']} из {renewal['expired']} "
        f"истёкших на прошлой неделе (цель ≥ {renewal['target'] * 100:.0f}%)\n",

        f"{icon(first_pay['achieved'])} <b>3. Первые оплаты</b>",
        f"   {first_pay['value']} новых платящих за месяц (цель ≥ {first_pay['target']})\n",

        f"{icon(inflow['achieved'])} <b>4. Приток</b>",
        f"   {inflow['value']} стартов бота за месяц (цель ≥ {inflow['target']})\n",

        f"{icon(k_factor['achieved'])} <b>5. K-фактор рефералки</b>",
        f"   {k_factor['value']:.2f} — {k_factor['referral_starts']} реф-стартов / "
        f"{k_factor['active_subs']} активных (цель ≥ {k_factor['target']})\n",

        f"{icon(long_tariff['achieved'])} <b>6. Доля длинных тарифов</b> (3 мес+)",
        f"   {long_tariff['value'] * 100:.1f}% — {long_tariff['long']} из {long_tariff['total']} "
        f"платежей за месяц (цель ≥ {long_tariff['target'] * 100:.0f}%)\n",

        f"{icon(winback['achieved'])} <b>7. Возвраты win-back</b>",
        f"   {winback_value_text} (цель {winback['target']}+)",
    ]
    text = "\n".join(text_parts)

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data="admin_cohort_metrics")
    kb.button(text="⬅️ К статистике", callback_data="admin_stats")
    kb.adjust(1)

    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup())
    except TelegramBadRequest as e:
        if "message is not modified" in e.message:
            await call.answer("Данные не изменились.", show_alert=False)
        else:
            logger.error(f"Error editing cohort metrics message: {e}")
            await call.answer("Произошла ошибка при обновлении.", show_alert=True)

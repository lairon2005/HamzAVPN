from real_traffic_pricing import real_traffic_pricing, real_traffic_checkout
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.methods import DeleteMessage

from tgbot.promo import get_promo_reward


ROOT = Path(__file__).resolve().parents[1]


class PromoRewardTests(unittest.TestCase):
    def test_bonus_days_are_available_for_broadcast_button(self):
        reward = get_promo_reward(SimpleNamespace(bonus_days=14, discount_percent=0))

        self.assertIsNotNone(reward)
        self.assertEqual(reward.kind, "bonus_days")
        self.assertEqual(reward.value, 14)
        self.assertEqual(reward.button_text, "🎁 Получить 14 бонусных дней")
        self.assertEqual(reward.description, "14 бонусных дней")

    def test_discount_remains_available_for_broadcast_button(self):
        reward = get_promo_reward(SimpleNamespace(bonus_days=0, discount_percent=25))

        self.assertIsNotNone(reward)
        self.assertEqual(reward.kind, "discount")
        self.assertEqual(reward.button_text, "💰 Применить скидку 25%")

    def test_empty_promo_is_not_attachable(self):
        reward = get_promo_reward(SimpleNamespace(bonus_days=0, discount_percent=0))

        self.assertIsNone(reward)

    def test_bonus_days_keep_priority_for_legacy_mixed_promo(self):
        reward = get_promo_reward(SimpleNamespace(bonus_days=7, discount_percent=10))

        self.assertEqual(reward.kind, "bonus_days")

    def test_day_word_is_inflected_in_button(self):
        one = get_promo_reward(SimpleNamespace(bonus_days=1, discount_percent=0))
        two = get_promo_reward(SimpleNamespace(bonus_days=2, discount_percent=0))
        eleven = get_promo_reward(SimpleNamespace(bonus_days=11, discount_percent=0))

        self.assertIn("1 бонусный день", one.button_text)
        self.assertIn("2 бонусных дня", two.button_text)
        self.assertIn("11 бонусных дней", eleven.button_text)


class BroadcastPromoWiringTests(unittest.TestCase):
    def test_admin_broadcast_accepts_reward_instead_of_discount_only(self):
        source = (ROOT / "tgbot/handlers/admin/broadcast.py").read_text()

        self.assertIn("reward = get_promo_reward(promo)", source)
        self.assertNotIn("promo.discount_percent == 0", source)

    def test_user_button_applies_bonus_days_through_subscription_service(self):
        source = (ROOT / "tgbot/handlers/user/payment.py").read_text()

        self.assertIn('if reward.kind == "bonus_days":', source)
        self.assertIn(
            "await promo_service.apply_bonus_days(user_id, result.promo, subscription_service)",
            source,
        )


# =============================================================================
# --- Поведенческие тесты: TOCTOU-гонка на claim'e промокода в хендлерах ---
#
# Выше — хрупкие тесты на подстроки исходника (сохранены как есть: сигнатура
# вызова apply_bonus_days не менялась, только обёрнута доп. except'ом). Здесь —
# поведенческая проверка (security review 2026-07-26): когда
# PromoCodeService.apply_bonus_days()/apply() бросает PromoClaimError (гонка
# проиграна или промокод исчерпан между validate() и claim'ом), хендлер должен
# показать пользователю "уже использован/закончился", а НЕ "обратитесь в
# поддержку" (это сообщение зарезервировано для настоящих сбоев Remnawave/БД).
#
# tgbot/handlers/user/payment.py тянет за собой loader/database/remnawave/
# tgbot.services (тяжёлые синглтоны, требуют .env и живую БД) — поэтому модуль
# грузится через spec_from_file_location с подменой sys.modules, тот же приём,
# что и в tests/test_stars_payment.py / tests/test_promo_code_service.py.
# =============================================================================

def _real_device_pricing():
    """Настоящий tgbot/services/device_pricing.py — модуль без зависимостей.

    Стабить нечем: расчёт стоимости доп. устройств и есть то, что влияет на
    сумму счёта в обработчике оплаты.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "tgbot" / "services" / "device_pricing.py"
    spec = importlib.util.spec_from_file_location("tgbot.services.device_pricing", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_module(module_name: str, relative_path: str, stubs: dict):
    module_path = ROOT / relative_path
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
    return module


def load_payment_handlers_module():
    """
    Загружает tgbot/handlers/user/payment.py изолированно, подменяя все
    "тяжёлые" зависимости (loader.config, database, remnawave, tgbot.services
    синглтоны). promo_service/subscription_service возвращаются вызывающему,
    чтобы каждый тест мог настроить свои side_effect/return_value.
    """
    loader_module = types.ModuleType("loader")
    loader_module.logger = Mock()
    loader_module.config = SimpleNamespace(
        platega=SimpleNamespace(merchant_id="x", secret="y", api_url="https://app.platega.io"),
    )

    database_module = types.ModuleType("database")
    database_module.tariff_repo = AsyncMock()
    database_module.user_repo = AsyncMock()

    remnawave_pkg = types.ModuleType("remnawave")
    remnawave_pkg.__path__ = []
    remnawave_client_module = types.ModuleType("remnawave.client")
    remnawave_client_module.RemnawaveClient = type("RemnawaveClient", (), {})

    tgbot_module = types.ModuleType("tgbot")
    tgbot_module.__path__ = []

    tgbot_promo_module = _load_module(
        "tgbot.promo.real_for_payment_handler_test", "tgbot/promo.py", {}
    )

    tgbot_services_module = types.ModuleType("tgbot.services")
    tgbot_services_module.__path__ = []
    promo_service = AsyncMock()
    subscription_service = AsyncMock()
    payment_service = AsyncMock()
    tgbot_services_module.promo_service = promo_service
    tgbot_services_module.subscription_service = subscription_service
    tgbot_services_module.payment_service = payment_service
    # Настройки доп. устройств спрашивает чекаут (шаг «сколько устройств»).
    device_slot_service = AsyncMock()
    device_pricing_module = _real_device_pricing()
    device_slot_service.settings.return_value = device_pricing_module.DeviceSettings()
    tgbot_services_module.device_slot_service = device_slot_service
    tgbot_services_module.traffic_service = SimpleNamespace(settings=AsyncMock(return_value=real_traffic_pricing().TrafficSettings()))

    promo_code_service_module = types.ModuleType("tgbot.services.promo_code_service")
    promo_code_service_module.PromoClaimError = type("PromoClaimError", (Exception,), {})

    tgbot_services_pricing_module = types.ModuleType("tgbot.services.pricing")
    tgbot_services_pricing_module.effective_price = lambda tariff, user_has_active_sub=False: 100.0
    tgbot_services_pricing_module.format_quota = lambda gb: "Безлимит"

    tgbot_services_payment_module = types.ModuleType("tgbot.services.payment")
    tgbot_services_payment_module.create_payment = Mock()
    tgbot_services_payment_module.get_payment_url = Mock()
    tgbot_services_payment_module.parse_webhook_notification = Mock()

    tgbot_handlers_module = types.ModuleType("tgbot.handlers")
    tgbot_handlers_module.__path__ = []
    tgbot_handlers_user_module = types.ModuleType("tgbot.handlers.user")
    tgbot_handlers_user_module.__path__ = []
    tgbot_handlers_user_profile_module = types.ModuleType("tgbot.handlers.user.profile")
    show_profile_logic = AsyncMock()
    tgbot_handlers_user_profile_module.show_profile_logic = show_profile_logic

    tgbot_keyboards_module = types.ModuleType("tgbot.keyboards")
    tgbot_keyboards_module.__path__ = []
    tgbot_keyboards_inline_module = types.ModuleType("tgbot.keyboards.inline")
    tgbot_keyboards_inline_module.cancel_fsm_keyboard = Mock(return_value="kb")
    tgbot_keyboards_inline_module.tariffs_keyboard = Mock(return_value="kb")
    tgbot_keyboards_inline_module.back_to_main_menu_keyboard = Mock(return_value="kb")
    tgbot_keyboards_inline_module.payment_method_choice_keyboard = Mock(return_value="kb")
    tgbot_keyboards_inline_module.tariff_slots_keyboard = Mock(return_value="kb")

    tgbot_states_module = types.ModuleType("tgbot.states")
    tgbot_states_module.__path__ = []
    tgbot_states_payment_states_module = types.ModuleType("tgbot.states.payment_states")
    # Настоящий State, а не object(): payment.py вешает его фильтром в
    # @payment_router.message(PromoApplyFSM.awaiting_code), а aiogram при
    # регистрации фильтра делает inspect.getfullargspec(callback). На object()
    # это TypeError: unsupported callable — версия aiogram, закреплённая в
    # прод-образе, роняет на нём импорт модуля целиком (локально свежее aiogram
    # это проглатывал, из-за чего расхождение всплывало только в контейнере).
    class _PromoApplyFSM(StatesGroup):
        awaiting_code = State()

    tgbot_states_payment_states_module.PromoApplyFSM = _PromoApplyFSM

    stubs = {
        "loader": loader_module,
        "database": database_module,
        "remnawave": remnawave_pkg,
        "remnawave.client": remnawave_client_module,
        "tgbot": tgbot_module,
        "tgbot.promo": tgbot_promo_module,
        "tgbot.services": tgbot_services_module,
        "tgbot.services.promo_code_service": promo_code_service_module,
        "tgbot.services.pricing": tgbot_services_pricing_module,
        "tgbot.services.traffic_pricing": real_traffic_pricing(),
            "tgbot.services.traffic_checkout": real_traffic_checkout(),
            "tgbot.services.device_pricing": device_pricing_module,
        "tgbot.services.payment": tgbot_services_payment_module,
        "tgbot.handlers": tgbot_handlers_module,
        "tgbot.handlers.user": tgbot_handlers_user_module,
        "tgbot.handlers.user.profile": tgbot_handlers_user_profile_module,
        "tgbot.keyboards": tgbot_keyboards_module,
        "tgbot.keyboards.inline": tgbot_keyboards_inline_module,
        "tgbot.states": tgbot_states_module,
        "tgbot.states.payment_states": tgbot_states_payment_states_module,
    }

    module = _load_module(
        "payment_handlers_under_test", "tgbot/handlers/user/payment.py", stubs
    )
    # Чтобы isinstance(event, CallbackQuery) в хендлерах срабатывал на фейке
    # (см. FakeCallbackQuery) — иначе ветка редактирования сообщения не тестируется.
    module.CallbackQuery = FakeCallbackQuery
    return module, SimpleNamespace(
        promo_service=promo_service,
        subscription_service=subscription_service,
        show_profile_logic=show_profile_logic,
        tariff_repo=database_module.tariff_repo,
        user_repo=database_module.user_repo,
        PromoClaimError=promo_code_service_module.PromoClaimError,
    )


class FakeCallbackQuery:
    """
    Замена aiogram.types.CallbackQuery. Именно класс, а не SimpleNamespace:
    show_tariffs_logic/_start_promo_input ветвятся через
    isinstance(event, CallbackQuery), и на SimpleNamespace проверка молча
    уходила в ветку Message — то есть редактирование сообщения (где и живёт баг
    с caption-рассылкой) тестами не покрывалось вовсе.
    load_payment_handlers_module() подменяет module.CallbackQuery на этот класс.
    """

    def __init__(self, data, user_id=123):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(
            answer=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())),
            edit_text=AsyncMock(),
            delete=AsyncMock(),
        )
        self.answer = AsyncMock()


def _callback_query(data, user_id=123):
    return FakeCallbackQuery(data, user_id)


def _photo_callback_query(data, user_id=123):
    """
    Кнопка промокода на сообщении рассылки С ФОТОГРАФИЕЙ: bot.copy_message
    копирует медиа как есть, у получателя сообщение с caption и без text,
    и edit_text по нему Telegram отклоняет.
    """
    call = _callback_query(data, user_id)
    call.message.edit_text.side_effect = TelegramBadRequest(
        method=DeleteMessage(chat_id=1, message_id=1),
        message="Bad Request: there is no text in the message to edit",
    )
    return call


def _fsm_state():
    return SimpleNamespace(
        clear=AsyncMock(),
        set_state=AsyncMock(),
        update_data=AsyncMock(),
        get_data=AsyncMock(return_value={}),
    )


class BroadcastPromoClaimRaceBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_bonus_button_shows_already_used_message_on_claim_race(self):
        module, deps = load_payment_handlers_module()
        deps.promo_service.validate.return_value = SimpleNamespace(
            is_valid=True,
            promo=SimpleNamespace(id=1, code="RACE10", bonus_days=10, discount_percent=0),
        )
        # Гонка: второе конкурентное нажатие проиграло try_claim().
        deps.promo_service.apply_bonus_days.side_effect = deps.PromoClaimError("already claimed")

        call = _callback_query("apply_promo_RACE10")
        state = _fsm_state()

        await module.apply_promo_from_broadcast(call, state)

        # call.message.answer(...) вернул loading_message с edit_text — проверяем его.
        edited_text = call.message.answer.return_value.edit_text.await_args.args[0]
        self.assertIn("уже использован", edited_text.lower())
        self.assertNotIn("обратитесь в поддержку", edited_text.lower())
        # extend() дошёл до сервиса, но не должен был начислить дни повторно —
        # apply_bonus_days сама решает claim -> extend, здесь просто проверяем,
        # что сервис вызван и ошибка не замаскирована как "внутренняя".
        deps.promo_service.apply_bonus_days.assert_awaited_once()

    async def test_discount_button_shows_already_used_message_on_claim_race(self):
        module, deps = load_payment_handlers_module()
        deps.promo_service.validate.return_value = SimpleNamespace(
            is_valid=True,
            promo=SimpleNamespace(id=2, code="SALE10", bonus_days=0, discount_percent=10),
        )
        deps.promo_service.apply.side_effect = deps.PromoClaimError("already claimed")

        call = _callback_query("apply_promo_SALE10")
        state = _fsm_state()

        await module.apply_promo_from_broadcast(call, state)

        alert_text = call.answer.await_args.args[0]
        self.assertIn("уже использован", alert_text.lower())
        self.assertNotIn("непредвиденная ошибка", alert_text.lower())


class BroadcastPromoOnPhotoMessageTests(unittest.IsolatedAsyncioTestCase):
    """
    Инцидент 2026-07-31 (рассылка промокода AUGUST с картинкой): рассылка уходит
    через bot.copy_message, поэтому кнопка промокода висит на сообщении с
    caption. show_tariffs_logic звал edit_text напрямую и получал
    "Bad Request: there is no text in the message to edit" — пользователь видел
    "непредвиденную ошибку", хотя промокод уже был погашен.
    """

    async def test_discount_button_on_photo_message_falls_back_to_new_message(self):
        module, deps = load_payment_handlers_module()
        deps.tariff_repo.get_active.return_value = [SimpleNamespace(id=1, name="Месяц")]
        deps.user_repo.get.return_value = None
        deps.promo_service.validate.return_value = SimpleNamespace(
            is_valid=True,
            promo=SimpleNamespace(id=5, code="AUGUST", bonus_days=0, discount_percent=15),
        )

        call = _photo_callback_query("apply_promo_AUGUST")
        state = _fsm_state()

        await module.apply_promo_from_broadcast(call, state)

        # edit_text попробовали, Telegram отказал — тарифы ушли новым сообщением,
        # а исходное сообщение рассылки убрано.
        call.message.edit_text.assert_awaited_once()
        sent_text = call.message.answer.await_args.args[0]
        self.assertIn("тарифный план", sent_text)
        call.message.delete.assert_awaited_once()
        # Промокод остаётся применённым — откатывать захват не за чем.
        deps.promo_service.apply.assert_awaited_once()
        deps.promo_service.release.assert_not_awaited()

    async def test_promo_claim_is_released_when_tariffs_cannot_be_shown(self):
        module, deps = load_payment_handlers_module()
        # Настоящий сбой уже ПОСЛЕ захвата промокода (в инциденте это был
        # edit_text по caption-сообщению).
        deps.tariff_repo.get_active.side_effect = RuntimeError("db is down")
        deps.user_repo.get.return_value = None
        promo = SimpleNamespace(id=6, code="AUGUST", bonus_days=0, discount_percent=15)
        deps.promo_service.validate.return_value = SimpleNamespace(is_valid=True, promo=promo)

        call = _callback_query("apply_promo_AUGUST")
        state = _fsm_state()

        await module.apply_promo_from_broadcast(call, state)

        # Иначе попытка сгорает молча: повторное нажатие упрётся в
        # "вы уже использовали этот промокод", а скидкой человек не воспользовался.
        deps.promo_service.release.assert_awaited_once_with(123, promo)
        self.assertIn("ошибка", call.answer.await_args.args[0].lower())


class ProcessPromoCodeClaimRaceBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_bonus_code_shows_already_used_message_on_claim_race(self):
        module, deps = load_payment_handlers_module()
        deps.promo_service.validate.return_value = SimpleNamespace(
            is_valid=True,
            promo=SimpleNamespace(id=3, code="RACE20", bonus_days=20, discount_percent=0),
        )
        deps.promo_service.apply_bonus_days.side_effect = deps.PromoClaimError("already claimed")

        message = SimpleNamespace(
            text="race20",
            from_user=SimpleNamespace(id=123),
            delete=AsyncMock(),
            answer=AsyncMock(),
        )
        state = _fsm_state()

        await module.process_promo_code(message, state, bot=Mock(), remnawave=Mock())

        answered_text = message.answer.await_args.args[0]
        self.assertIn("уже использован", answered_text.lower())
        self.assertNotIn("обратитесь в поддержку", answered_text.lower())
        deps.show_profile_logic.assert_not_awaited()

    async def test_manual_discount_code_shows_already_used_message_on_claim_race(self):
        module, deps = load_payment_handlers_module()
        deps.promo_service.validate.return_value = SimpleNamespace(
            is_valid=True,
            promo=SimpleNamespace(id=4, code="SALE20", bonus_days=0, discount_percent=20),
        )
        deps.promo_service.apply.side_effect = deps.PromoClaimError("already claimed")

        message = SimpleNamespace(
            text="sale20",
            from_user=SimpleNamespace(id=123),
            delete=AsyncMock(),
            answer=AsyncMock(),
        )
        state = _fsm_state()

        await module.process_promo_code(message, state, bot=Mock(), remnawave=Mock())

        answered_text = message.answer.await_args.args[0]
        self.assertIn("уже использован", answered_text.lower())
        # Скидочная ветка не должна дойти до show_tariffs_logic /
        # state.update_data при проигранной гонке.
        state.update_data.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

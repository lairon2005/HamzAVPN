from aiogram.fsm.state import State, StatesGroup


class DeviceSettingsFSM(StatesGroup):
    """Ввод нового значения настройки доп. устройств (цена / база / потолок)."""
    edit_value = State()

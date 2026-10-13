from aiogram.fsm.state import State, StatesGroup


class TrafficSettingsFSM(StatesGroup):
    """Ввод нового значения настройки трафика (база / пакет / цена / потолок)."""
    edit_value = State()

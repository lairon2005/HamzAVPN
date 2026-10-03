from aiogram.fsm.state import State, StatesGroup


class PromoFSM(StatesGroup):
    get_code = State()
    get_type = State()
    get_value = State()
    get_max_uses = State()

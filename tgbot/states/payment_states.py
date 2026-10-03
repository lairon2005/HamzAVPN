from aiogram.fsm.state import State, StatesGroup


class PromoApplyFSM(StatesGroup):
    awaiting_code = State()

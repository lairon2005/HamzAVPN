from aiogram.fsm.state import State, StatesGroup


class BroadcastFSM(StatesGroup):
    choose_audience = State()
    get_message = State()
    attach_promo = State()
    awaiting_promo = State()
    confirm = State()

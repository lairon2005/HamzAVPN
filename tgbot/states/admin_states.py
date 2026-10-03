from aiogram.fsm.state import State, StatesGroup


class AdminFSM(StatesGroup):
    find_user = State()
    add_days_user_id = State()
    add_days_amount = State()

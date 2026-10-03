from aiogram.fsm.state import StatesGroup, State


class EmailLinkFSM(StatesGroup):
    awaiting_email = State()
    awaiting_code = State()
    awaiting_password = State()

from aiogram.fsm.state import State, StatesGroup

class SupportFSM(StatesGroup):
    in_chat = State()
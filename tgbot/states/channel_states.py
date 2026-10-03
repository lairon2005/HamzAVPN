from aiogram.fsm.state import State, StatesGroup


class AdminChannelsFSM(StatesGroup):
    add_channel_id = State()
    delete_channel_id = State()

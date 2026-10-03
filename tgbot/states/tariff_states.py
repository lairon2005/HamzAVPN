from aiogram.fsm.state import State, StatesGroup


class TariffFSM(StatesGroup):
    add_name = State()
    add_price = State()
    add_duration = State()
    # --- Тарифы 2.0: квота трафика / лоялти-цена / подсветка (необязательные шаги) ---
    add_data_limit = State()
    add_loyalty_price = State()
    add_highlight = State()
    edit_field = State()

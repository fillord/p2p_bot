from aiogram.fsm.state import State, StatesGroup

class OrderCreation(StatesGroup):
    enter_title = State()
    enter_description = State()
    enter_price = State()
    confirm_order = State()

# Новое состояние для отклика
class MakeOffer(StatesGroup):
    enter_message = State()
    confirm_offer = State()
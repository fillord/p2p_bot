from aiogram.fsm.state import State, StatesGroup

class OrderCreation(StatesGroup):
    enter_category = State()
    enter_title = State()
    enter_description = State()
    enter_price = State()
    confirm_order = State()

class MakeOffer(StatesGroup):
    enter_message = State()
    confirm_offer = State()
    
# Новое состояние для отзыва
class LeaveReview(StatesGroup):
    enter_rating = State()
    enter_text = State()

class Withdrawal(StatesGroup):
    enter_amount = State()
    enter_address = State()
    confirm_withdrawal = State()
    
class SupportChat(StatesGroup):
    in_chat = State()

class AdminBalanceChange(StatesGroup):
    enter_amount = State()
    confirm_change = State()
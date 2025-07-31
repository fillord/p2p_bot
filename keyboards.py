from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

# --- Клавиатура главного меню ---
create_order_btn = KeyboardButton(text="📝 Создать заказ")
my_orders_btn = KeyboardButton(text="📂 Мои заказы")
balance_btn = KeyboardButton(text="💰 Мой баланс")
support_btn = KeyboardButton(text="🆘 Поддержка")

main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [create_order_btn, my_orders_btn],
        [balance_btn, support_btn]
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)


# --- Инлайн-клавиатура для меню баланса ---
top_up_btn = InlineKeyboardButton(text="➕ Пополнить", callback_data="top_up")

balance_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [top_up_btn]
    ]
)
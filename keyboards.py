from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

create_order_btn = KeyboardButton(text="📝 Создать заказ")
my_orders_btn = KeyboardButton(text="📂 Мои заказы")
feed_btn = KeyboardButton(text="🔥 Лента заказов")
profile_btn = KeyboardButton(text="👤 Мой профиль")
support_btn = KeyboardButton(text="🆘 Поддержка")

main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [create_order_btn, my_orders_btn],
        [feed_btn, profile_btn],
        [support_btn]
    ],
    resize_keyboard=True
)

top_up_btn = InlineKeyboardButton(text="➕ Пополнить", callback_data="top_up")
withdraw_btn = InlineKeyboardButton(text="➖ Вывести", callback_data="withdraw")
deals_history_btn = InlineKeyboardButton(text="📜 История сделок", callback_data="deals_history")
finance_history_btn = InlineKeyboardButton(text="💸 История баланса", callback_data="finance_history")
buy_vip_btn = InlineKeyboardButton(text="👑 Купить VIP", callback_data="buy_vip")


profile_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [top_up_btn, withdraw_btn],
        [deals_history_btn, finance_history_btn],
        [buy_vip_btn]
    ]
)
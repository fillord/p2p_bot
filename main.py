from dotenv import load_dotenv
load_dotenv()

# --- ШАГ 2: Теперь импортируем все остальное ---
import os
import asyncio
import logging
from decimal import Decimal
from functools import wraps
import math

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from datetime import datetime, timedelta, UTC 
from sqlalchemy import select, update, func, or_
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db_models import (
    Base, User, Transaction, Order, Offer,
    ChatMessage, Review, FinancialTransaction, Setting,
    Category
)
from keyboards import main_menu_keyboard, profile_keyboard # Исправлен импорт
from crypto_logic import generate_new_wallet, check_new_transactions, create_payout
from states import OrderCreation, MakeOffer, LeaveReview, Withdrawal, AdminBalanceChange, SupportChat

# Настраиваем логирование и константы
logging.basicConfig(level=logging.INFO)
PAGE_SIZE = 3
# --- Настройка базы данных и ID ---
DB_URL = f"postgresql+asyncpg://{os.getenv('DB_USER')}:{os.getenv('DB_PASS')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
ADMIN_ID = int(os.getenv("ADMIN_ID"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))
ORDER_CHANNEL_ID = os.getenv("ORDER_CHANNEL_ID")

engine = create_async_engine(DB_URL)
async_session = async_sessionmaker(engine, expire_on_commit=False)

# --- Настройка FSM хранилища и бота ---
storage = MemoryStorage()
BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=storage)

VIP_PLANS = {
    30: Decimal("5.00"),  # 30 дней за 5 USDT
    90: Decimal("12.00"), # 90 дней за 12 USDT
}

# --- Фабрики колбэков ---
class OrderCallback(CallbackData, prefix="order"):
    action: str
    order_id: int
class OfferCallback(CallbackData, prefix="offer"):
    action: str
    offer_id: int
class ReviewCallback(CallbackData, prefix="review"):
    action: str
    order_id: int
    reviewee_id: int
class AdminCallback(CallbackData, prefix="admin"):
    action: str
    user_id: int
class Paginator(CallbackData, prefix="pag"):
    action: str
    page: int
class VIPCallback(CallbackData, prefix="vip"):
    action: str
    days: int
class CategoryCallback(CallbackData, prefix="category"):
    action: str # 'select'
    category_id: int

# --- Декоратор для проверки прав администратора ---
def admin_only(func):
    @wraps(func)
    async def wrapper(message: types.Message, *args, **kwargs):
        if message.from_user.id != ADMIN_ID:
            return await message.answer("Эта команда доступна только администратору.")
        return await func(message, *args, **kwargs)
    return wrapper


def block_check(func):
    @wraps(func)
    async def wrapper(event: types.TelegramObject, *args, **kwargs):
        user_id = event.from_user.id
        async with async_session() as session:
            user = await session.scalar(select(User).where(User.telegram_id == user_id))
            
            if not user:
                if isinstance(event, types.CallbackQuery):
                    await event.answer("Пожалуйста, сначала запустите бота командой /start", show_alert=True)
                else:
                    await bot.send_message(user_id, "Пожалуйста, сначала запустите бота командой /start для регистрации.")
                return

            if user.is_blocked:
                if isinstance(event, types.CallbackQuery):
                    await event.answer("🔴 Ваш аккаунт заблокирован.", show_alert=True)
                else:
                    await bot.send_message(user_id, "🔴 Ваш аккаунт заблокирован. Обратитесь в поддержку.")
                return
        return await func(event, *args, **kwargs)
    return wrapper

async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# --- Фоновый процесс проверки платежей ---
async def check_payments():
    async with async_session() as session:
        users_with_wallets = await session.execute(select(User).where(User.wallet_address.isnot(None)))
        for user in users_with_wallets.scalars().all():
            new_transactions = await check_new_transactions(user.wallet_address)
            for tx in new_transactions:
                existing_tx = await session.execute(select(Transaction).where(Transaction.txid == tx['txid']))
                if existing_tx.scalar_one_or_none(): continue
                user.balance += tx['amount']
                session.add(FinancialTransaction(user_id=user.telegram_id, type='deposit', amount=tx['amount']))
                new_tx_record = Transaction(txid=tx['txid'])
                session.add(new_tx_record)
                try:
                    await bot.send_message(user.telegram_id, f"✅ Ваш баланс пополнен на <b>{tx['amount']:.2f} USDT</b>!")
                except Exception as e:
                    logging.error(f"Не удалось отправить уведомление о пополнении пользователю {user.telegram_id}: {e}")
        await session.commit()

def create_pagination_keyboard(page: int, total_pages: int):
    buttons = []
    if page > 0:
        buttons.append(types.InlineKeyboardButton(text="◀️ Назад", callback_data=Paginator(action="prev", page=page-1).pack()))
    
    if total_pages > 1:
        buttons.append(types.InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="ignore"))

    if page < total_pages - 1:
        buttons.append(types.InlineKeyboardButton(text="Вперед ▶️", callback_data=Paginator(action="next", page=page+1).pack()))
    
    if not buttons:
        return None
    return types.InlineKeyboardMarkup(inline_keyboard=[buttons])

async def format_orders_page(orders: list):
    if not orders:
        return "На данный момент нет доступных заказов. Загляните позже!"
    text = "<b>🔥 Доступные заказы:</b>\n\n"
    for order in orders:
        customer_username = f"@{order.customer.username}" if order.customer.username else "Скрыт"
        category_name = order.category.name if order.category else "Без категории" # Получаем имя категории
        text += (f"<b>Заказ №{order.id}</b> | {order.title}\n"
                 f"<b>Категория:</b> {category_name}\n"
                 f"<b>Цена:</b> {order.price:.2f} USDT\n"
                 f"<b>Заказчик:</b> {customer_username}\n"
                 f"<i>{order.description[:100]}...</i>\n"
                 f"➡️ /order {order.id} - для деталей и отклика\n\n")
    return text

async def show_user_profile(message_or_callback: types.Message | types.CallbackQuery, user_id: int):
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == user_id))
        if not user:
            return await message_or_callback.answer(f"Пользователь с ID '{user_id}' не найден.")
        status = "🔴 Заблокирован" if user.is_blocked else "🟢 Активен"
        user_info_text = (f"<b>👤 Информация о пользователе:</b>\n\n"
                          f"<b>ID:</b> <code>{user.telegram_id}</code>\n"
                          f"<b>Username:</b> @{user.username if user.username else 'N/A'}\n"
                          f"<b>Баланс:</b> {user.balance:.2f} USDT\n"
                          f"<b>Рейтинг:</b> {user.rating:.2f} ⭐ ({user.reviews_count} отзывов)\n"
                          f"<b>Статус:</b> {status}\n"
                          f"<b>Дата регистрации:</b> {user.registration_date.strftime('%Y-%m-%d %H:%M')}")
        block_action = "unblock" if user.is_blocked else "block"
        block_text = "Разблокировать" if user.is_blocked else "Заблокировать"
        admin_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=block_text, callback_data=AdminCallback(action=block_action, user_id=user.telegram_id).pack())],
            [types.InlineKeyboardButton(text="➕ Начислить", callback_data=AdminCallback(action="credit", user_id=user.telegram_id).pack()),
             types.InlineKeyboardButton(text="➖ Списать", callback_data=AdminCallback(action="debit", user_id=user.telegram_id).pack())]
        ])
        # Отвечаем в зависимости от того, откуда вызвана функция
        if isinstance(message_or_callback, types.CallbackQuery):
             await message_or_callback.message.answer(user_info_text, reply_markup=admin_keyboard)
        else:
             await message_or_callback.answer(user_info_text, reply_markup=admin_keyboard)

@dp.message(CommandStart())
async def handle_start(message: types.Message, state: FSMContext, command: CommandObject = None):
    await state.clear()
    
    # Обработка deep-link
    if command and command.args and command.args.startswith("offer_"):
        async with async_session() as session:
            user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
            if not user:
                await message.answer("Добро пожаловать! Пожалуйста, отправьте /start еще раз, чтобы завершить регистрацию, прежде чем откликаться на заказы.")
                session.add(User(telegram_id=message.from_user.id, username=message.from_user.username))
                await session.commit()
                return

            is_vip = user.vip_expires_at and user.vip_expires_at > datetime.now(UTC)
            if not is_vip:
                offers_count = await session.scalar(select(func.count(Offer.id)).where(Offer.executor_id == message.from_user.id))
                if offers_count >= 3:
                    await message.answer("❌ Вы достигли лимита на отклики (10).")
                    return

        try:
            order_id = int(command.args.split("_")[1])
            await state.set_state(MakeOffer.enter_message)
            await state.update_data(order_id=order_id)
            await message.answer(f"Вы хотите откликнуться на заказ №{order_id}.\nВведите сопроводительное сообщение:")
            return
        except (IndexError, ValueError):
            pass 
            
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        welcome_text = ""
        if user:
            if user.is_blocked:
                return await message.answer("🔴 Ваш аккаунт заблокирован.")
            welcome_text = f"С возвращением, {message.from_user.first_name}!"
        else:
            new_user = User(telegram_id=message.from_user.id, username=message.from_user.username)
            session.add(new_user)
            await session.commit()
            welcome_text = f"Добро пожаловать, {message.from_user.first_name}! Вы успешно зарегистрированы."
        await message.answer(welcome_text, reply_markup=main_menu_keyboard)

@dp.message(Command("cancel"))
@dp.message(F.text.casefold() == "отмена")
async def cancel_handler(message: types.Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is None: return
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu_keyboard)

@dp.message(Command("stats"))
@admin_only
async def get_stats(message: types.Message):
    async with async_session() as session:
        total_users = await session.scalar(select(func.count(User.id)))
        
        # Считаем заказы по статусам
        open_orders = await session.scalar(select(func.count(Order.id)).where(Order.status == "open"))
        in_progress_orders = await session.scalar(select(func.count(Order.id)).where(Order.status == "in_progress"))
        dispute_orders = await session.scalar(select(func.count(Order.id)).where(Order.status == "dispute"))
        completed_orders = await session.scalar(select(func.count(Order.id)).where(Order.status == "completed"))
        
        # Считаем сумму, зарезервированную в сделках
        hold_amount_res = await session.scalar(select(func.sum(Order.price)).where(Order.status == "in_progress"))
        hold_amount = hold_amount_res or Decimal("0.00")

        stats_text = (
            "<b>📊 Статистика бота:</b>\n\n"
            f"<b>Всего пользователей:</b> {total_users}\n\n"
            "<b>Заказы:</b>\n"
            f"  - 🟢 Открытые: {open_orders}\n"
            f"  - 🟡 В процессе: {in_progress_orders}\n"
            f"  - 🔴 В споре: {dispute_orders}\n"
            f"  - ⚪️ Завершенные: {completed_orders}\n\n"
            f"<b>Финансы:</b>\n"
            f"  - 💰 Зарезервировано в сделках: {hold_amount:.2f} USDT"
        )
        await message.answer(stats_text)




# --- Логика создания заказа (FSM) ---
@dp.message(F.text == "📝 Создать заказ")
@block_check
async def order_creation_start(message: types.Message, state: FSMContext):
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        
        # --- ВОЗВРАЩАЕМ ПРОВЕРКУ ЛИМИТА НА СОЗДАНИЕ ЗАКАЗОВ ---
        is_vip = user.vip_expires_at and user.vip_expires_at > datetime.now(UTC)
        if not is_vip:
            orders_count = await session.scalar(
                select(func.count(Order.id)).where(Order.customer_id == message.from_user.id)
            )
            if orders_count >= 10:
                return await message.answer(
                    "❌ Вы достигли лимита на создание заказов (10).\n"
                    "Чтобы снять ограничения, приобретите VIP-статус."
                )
        # =======================================================

        # Получаем категории из базы
        categories_result = await session.execute(select(Category).order_by(Category.name))
        categories = categories_result.scalars().all()
        if not categories:
            return await message.answer("Категории еще не созданы. Администратор скоро их добавит.")

    # Создаем клавиатуру с категориями
    buttons = [
        [types.InlineKeyboardButton(text=cat.name, callback_data=CategoryCallback(action="select", category_id=cat.id).pack())]
        for cat in categories
    ]
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await state.set_state(OrderCreation.enter_category)
    await message.answer("Пожалуйста, выберите категорию для вашего заказа:", reply_markup=keyboard)

@dp.callback_query(OrderCreation.enter_category, CategoryCallback.filter(F.action == "select"))
async def enter_category(callback: CallbackQuery, callback_data: CategoryCallback, state: FSMContext):
    await state.update_data(category_id=callback_data.category_id)
    await state.set_state(OrderCreation.enter_title)
    await callback.message.edit_text("Категория выбрана. Теперь введите название вашего заказа.\n\nДля отмены введите /cancel")


@dp.message(OrderCreation.enter_title)
@block_check
async def enter_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(OrderCreation.enter_description)
    await message.answer("Отлично! Теперь введите подробное описание задачи.")
@dp.message(OrderCreation.enter_description)
@block_check
async def enter_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(OrderCreation.enter_price)
    await message.answer("Теперь укажите цену заказа в USDT. Например: 50.5")

@dp.message(OrderCreation.enter_price)
@block_check
async def enter_price(message: types.Message, state: FSMContext):
    try:
        price = Decimal(message.text)
        if price < 0:
            await message.answer("Цена не может быть отрицательной. Попробуйте еще раз.")
            return
    except Exception:
        await message.answer("Неверный формат цены. Введите число. Например: 50.5")
        return
        
    await state.update_data(price=price)
    order_data = await state.get_data()
    
    async with async_session() as session:
        # Получаем имя категории для отображения
        category = await session.get(Category, order_data['category_id'])
        category_name = category.name if category else "Не выбрана"
        # Сохраняем имя в состояние для следующего шага
        await state.update_data(category_name=category_name)

        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        if not user or (price > 0 and user.balance < price):
            balance = user.balance if user else Decimal("0.00")
            await message.answer(f"На вашем балансе недостаточно средств ({balance:.2f} USDT). Пожалуйста, пополните баланс и попробуйте снова.", reply_markup=main_menu_keyboard)
            await state.clear()
            return

    text = (
        f"<b>Пожалуйста, проверьте данные вашего заказа:</b>\n\n"
        f"<b>Категория:</b> {category_name}\n"
        f"<b>Название:</b> {order_data['title']}\n"
        f"<b>Описание:</b> {order_data['description']}\n"
        f"<b>Цена:</b> {price:.2f} USDT\n\n"
        "Нажмите '✅ Создать', чтобы разместить заказ."
    )
    confirm_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="✅ Создать", callback_data="order_confirm")], [types.InlineKeyboardButton(text="❌ Отменить", callback_data="order_cancel")]])
    await state.set_state(OrderCreation.confirm_order)
    await message.answer(text, reply_markup=confirm_keyboard)

@dp.callback_query(OrderCreation.confirm_order, F.data == "order_confirm")
async def confirm_order_creation(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Создаем заказ...")
    order_data = await state.get_data()
    
    async with async_session() as session:
        price = order_data['price']
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        
        if not user or (price > 0 and user.balance < price):
            await callback.message.edit_text("Ошибка! На вашем балансе больше недостаточно средств.")
            await state.clear()
            return

        new_order = Order(
            title=order_data['title'],
            description=order_data['description'],
            price=price,
            customer_id=callback.from_user.id,
            category_id=order_data['category_id']
        )
        session.add(new_order)
        
        if price > 0:
            user.balance -= price
            await session.flush([new_order])
            session.add(FinancialTransaction(user_id=user.telegram_id, type='order_payment', amount=-price, order_id=new_order.id))
        
        await session.commit()
        
        await callback.message.edit_text(f"✅ Ваш заказ №{new_order.id} успешно создан!", reply_markup=None)
        
        try:
            bot_info = await bot.get_me()
            bot_username = bot_info.username
            
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
                types.InlineKeyboardButton(
                    text="🚀 Откликнуться", 
                    url=f"https://t.me/{bot_username}?start=offer_{new_order.id}"
                )
            ]])
            
            # Берем имя категории из сохраненного состояния
            category_name = order_data.get('category_name', 'Без категории')
            
            order_text = (
                f"<b>🟢 Новый заказ №{new_order.id}</b>\n\n"
                f"<b>Название:</b> {new_order.title}\n"
                f"<b>Категория:</b> {category_name}\n"
                f"<b>Цена:</b> {new_order.price:.2f} USDT\n\n"
                f"<i>{new_order.description}</i>"
            )
            await bot.send_message(ORDER_CHANNEL_ID, order_text, reply_markup=keyboard)
        except Exception as e:
            logging.error(f"Не удалось отправить заказ {new_order.id} в канал: {e}")
            await callback.message.answer("Не удалось опубликовать заказ в канале. Обратитесь к администратору.")
            
    await state.clear()

    
@dp.callback_query(OrderCreation.confirm_order, F.data == "order_cancel")
async def cancel_order_creation(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Создание заказа отменено.", reply_markup=None)

# --- ОБРАБОТЧИКИ КОНКРЕТНЫХ КНОПОК И КОМАНД ---
@dp.message(F.text == "🔥 Лента заказов")
@block_check
async def handle_order_feed(message: types.Message):
    async with async_session() as session:
        total_orders_res = await session.scalar(
            select(func.count(Order.id)).where(Order.status == "open", Order.customer_id != message.from_user.id)
        )
        total_pages = math.ceil(total_orders_res / PAGE_SIZE)
        
        stmt = (
            select(Order)
            .where(Order.status == "open", Order.customer_id != message.from_user.id)
            .options(joinedload(Order.customer), joinedload(Order.category))
            .order_by(Order.creation_date.desc())
            .limit(PAGE_SIZE).offset(0)
        )
        orders = (await session.execute(stmt)).scalars().all()
        
        text = await format_orders_page(orders)
        keyboard = create_pagination_keyboard(page=0, total_pages=total_pages)
        
        await message.answer(text, reply_markup=keyboard)
    
@dp.callback_query(Paginator.filter())
@block_check
async def handle_order_feed_page(callback: CallbackQuery, callback_data: Paginator):
    page = callback_data.page
    async with async_session() as session:
        total_orders_res = await session.scalar(
            select(func.count(Order.id)).where(Order.status == "open", Order.customer_id != callback.from_user.id)
        )
        total_pages = math.ceil(total_orders_res / PAGE_SIZE)

        offset = page * PAGE_SIZE
        stmt = (
        select(Order).where(Order.status == "open", Order.customer_id != callback.from_user.id)
        .options(joinedload(Order.customer), joinedload(Order.category)) # ДОБАВЛЕНО joinedload(Order.category)
        .order_by(Order.creation_date.desc())
        .limit(PAGE_SIZE).offset(offset)
    )
        orders = (await session.execute(stmt)).scalars().all()
        
        text = await format_orders_page(orders)
        keyboard = create_pagination_keyboard(page=page, total_pages=total_pages)

        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer()


@dp.message(F.text == "📂 Мои заказы")
@block_check
async def handle_my_orders(message: types.Message):
    async with async_session() as session:
        user_id = message.from_user.id
        
        # === ИСПРАВЛЕНИЕ: Добавляем joinedload(Order.category) ===
        created_orders_stmt = (
            select(Order)
            .where(Order.customer_id == user_id)
            .options(joinedload(Order.category)) # <--- Добавлено
            .order_by(Order.status.desc(), Order.creation_date.desc())
        )
        created_orders_res = await session.execute(created_orders_stmt)
        created_orders = created_orders_res.scalars().unique().all()

        executing_orders_stmt = (
            select(Order)
            .where(Order.executor_id == user_id)
            .options(joinedload(Order.category)) # <--- Добавлено
            .order_by(Order.status.desc(), Order.creation_date.desc())
        )
        executing_orders_res = await session.execute(executing_orders_stmt)
        executing_orders = executing_orders_res.scalars().unique().all()
        # =======================================================

        if not created_orders and not executing_orders:
            return await message.answer("У вас пока нет активных заказов. \nСоздайте свой или найдите в ленте /feed")
        
        response_text = ""
        status_emoji = {"open": "🟢", "in_progress": "🟡", "pending_approval": "🔵", "completed": "⚪️", "dispute": "🔴"}

        if created_orders:
            response_text += "<b>🗂️ Ваши созданные заказы:</b>\n"
            for order in created_orders:
                category_name = f" ({order.category.name})" if order.category else ""
                response_text += f"{status_emoji.get(order.status, '')} №{order.id}: {order.title}{category_name}\n"
        
        if executing_orders:
            response_text += "\n<b>💼 Заказы, которые вы выполняете:</b>\n"
            for order in executing_orders:
                category_name = f" ({order.category.name})" if order.category else ""
                response_text += f"{status_emoji.get(order.status, '')} №{order.id}: {order.title}{category_name}\n"
        
        response_text += "\n\nℹ️ Для просмотра деталей и действий по заказу, используйте команду /order `id_заказа`"
        await message.answer(response_text)
        
@dp.message(Command("order"))
@block_check
async def view_specific_order(message: types.Message, command: CommandObject):
    if not command.args or not command.args.isdigit():
        return await message.answer("Пожалуйста, укажите ID заказа. Пример: /order 123")
    
    order_id = int(command.args)
    user_id = message.from_user.id
    
    async with async_session() as session:
        # Подгружаем связанные данные о заказчике сразу
        order = await session.get(Order, order_id, options=[joinedload(Order.customer), joinedload(Order.category)]) # ДОБАВЛЕНО
        
        if not order:
            return await message.answer("Заказ не найден.")

        # Новая логика проверки доступа
        is_participant = order.customer_id == user_id or order.executor_id == user_id
        if order.status != 'open' and not is_participant:
            return await message.answer("У вас нет доступа к этому заказу, так как он уже в работе или завершен.")

        status_emoji = {"open": "🟢", "in_progress": "🟡", "pending_approval": "🔵", "completed": "⚪️", "dispute": "🔴"}
        customer_username = f"@{order.customer.username}" if order.customer.username else "Скрыт"
        category_name = order.category.name if order.category else "Без категории"
        text = (
            f"{status_emoji.get(order.status, '')} <b>Заказ №{order.id}: {order.title}</b>\n\n"
            f"<b>Категория:</b> {category_name}\n"
            f"<b>Описание:</b> {order.description}\n\n"
            f"<b>Цена:</b> {order.price:.2f} USDT\n"
            f"<b>Статус:</b> {order.status}\n"
            f"<b>Заказчик:</b> {customer_username}"
        )
        
        keyboard = None
        # Определяем, какие кнопки показать
        if order.status == "open":
            if order.customer_id == user_id: # Если это наш заказ
                offers_count = await session.scalar(select(func.count(Offer.id)).where(Offer.order_id == order.id))
                if offers_count > 0:
                    text += f"\n<b>Откликов:</b> {offers_count}"
                    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(
                        text="Посмотреть отклики", callback_data=OrderCallback(action="view", order_id=order.id).pack())]])
            else: # Если это чужой открытый заказ
                 keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(
                    text="🚀 Откликнуться", callback_data=OrderCallback(action="offer", order_id=order.id).pack())]])

        elif order.status == "in_progress" and order.executor_id == user_id:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(
                text="✅ Сдать работу", callback_data=OrderCallback(action="submit_work", order_id=order.id).pack())]])
        
        await message.answer(text, reply_markup=keyboard)

@dp.callback_query(F.data == "buy_vip")
@block_check
async def buy_vip_handler(callback: CallbackQuery):
    await callback.answer()
    
    keyboard_buttons = []
    for days, price in VIP_PLANS.items():
        keyboard_buttons.append([
            types.InlineKeyboardButton(
                text=f"{days} дней - {price:.2f} USDT",
                callback_data=VIPCallback(action="buy", days=days).pack()
            )
        ])

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.answer("Выберите план подписки:", reply_markup=keyboard)

# НОВЫЙ ОБРАБОТЧИК ПОКУПКИ КОНКРЕТНОГО ПЛАНА
@dp.callback_query(VIPCallback.filter(F.action == "buy"))
@block_check
async def process_vip_buy(callback: CallbackQuery, callback_data: VIPCallback):
    days = callback_data.days
    price = VIP_PLANS.get(days)

    if not price:
        return await callback.answer("План не найден.", show_alert=True)

    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        
        if user.balance < price:
            await callback.answer("На вашем балансе недостаточно средств.", show_alert=True)
            return

        # Списываем деньги и обновляем VIP
        user.balance -= price
        session.add(FinancialTransaction(user_id=user.telegram_id, type='vip_payment', amount=-price))
        
        current_expiry = user.vip_expires_at or datetime.now(UTC)
        if current_expiry < datetime.now(UTC):
            current_expiry = datetime.now(UTC)
        
        user.vip_expires_at = current_expiry + timedelta(days=days)
        await session.commit()
    
    await callback.message.edit_text(
        f"🎉 Поздравляем! Вы успешно приобрели VIP-статус на {days} дней.\n"
        f"Он активен до {user.vip_expires_at.strftime('%d.%m.%Y')}."
    )
    await callback.answer()

@dp.message(F.text == "👤 Мой профиль")
@block_check
async def handle_profile(message: types.Message):
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        if not user:
            return await message.answer("Произошла ошибка. Пожалуйста, нажмите /start для регистрации.")
        
        # Проверяем VIP-статус
        vip_status = "Активен ✅" if user.vip_expires_at and user.vip_expires_at > datetime.now(UTC) else "Неактивен ❌"
        
        profile_text = (
            f"<b>👤 Ваш профиль</b>\n\n"
            f"<b>Баланс:</b> <code>{user.balance:.2f} USDT</code>\n"
            f"<b>Рейтинг:</b> {user.rating:.2f} ⭐ ({user.reviews_count} отзывов)\n"
            f"<b>VIP Статус:</b> {vip_status}"
        )
        if vip_status == "Активен ✅":
            profile_text += f"\n  (до {user.vip_expires_at.strftime('%d.%m.%Y')})"
            
        await message.answer(profile_text, reply_markup=profile_keyboard)

# --- НОВАЯ АДМИН-КОМАНДА: ВЫДАЧА VIP ---
@dp.message(Command("grant_vip"))
@admin_only
async def grant_vip(message: types.Message, command: CommandObject):
    args = (command.args or "").split()
    if len(args) != 2:
        return await message.answer("Неверный формат. Используйте: /grant_vip <user_id> <days>")
    
    try:
        user_id = int(args[0])
        days = int(args[1])
    except ValueError:
        return await message.answer("ID пользователя и количество дней должны быть числами.")

    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == user_id))
        if not user:
            return await message.answer(f"Пользователь с ID {user_id} не найден.")
            
        # Устанавливаем или продлеваем VIP
        current_expiry = user.vip_expires_at or datetime.now(UTC)
        if current_expiry < datetime.now(UTC):
            current_expiry = datetime.now(UTC)
            
        user.vip_expires_at = current_expiry + timedelta(days=days)
        await session.commit()
        
        await message.answer(f"✅ VIP-статус для пользователя {user_id} успешно продлен на {days} дней.\n"
                             f"Новая дата окончания: {user.vip_expires_at.strftime('%d.%m.%Y')}")
        
        try:
            await bot.send_message(user_id, f"🎉 Поздравляем! Администратор выдал вам VIP-статус на {days} дней.")
        except Exception as e:
            logging.error(f"Не удалось уведомить пользователя {user_id} о VIP-статусе: {e}")


# === ИЗМЕНЕНИЕ 2: Упрощаем команду /user ===
@dp.message(Command("user"))
@admin_only
async def get_user_info_command(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer("Укажите ID или @username пользователя. Пример: /user 123456789")
        
    user_identifier = command.args
    async with async_session() as session:
        if user_identifier.isdigit():
            user = await session.scalar(select(User).where(User.telegram_id == int(user_identifier)))
        else:
            user = await session.scalar(select(User).where(User.username == user_identifier.replace("@", "")))
        
        if not user:
            return await message.answer(f"Пользователь '{user_identifier}' не найден.")
        
        # Вызываем нашу новую функцию для отображения
        await show_user_profile(message, user.telegram_id)


# === ИЗМЕНЕНИЕ 3: Исправляем обработчик блокировки ===
@dp.callback_query(AdminCallback.filter(F.action.in_(["block", "unblock"])))
async def handle_block_user(callback: CallbackQuery, callback_data: AdminCallback):
    user_id_to_change = callback_data.user_id
    action = callback_data.action
    
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == user_id_to_change))
        if not user:
            return await callback.answer("Пользователь не найден.", show_alert=True)

        if action == "block":
            user.is_blocked = True
            await callback.answer("Пользователь заблокирован.", show_alert=True)
            try: await bot.send_message(user_id_to_change, "🔴 Ваш аккаунт был заблокирован администратором.")
            except Exception: pass
        else: # unblock
            user.is_blocked = False
            await callback.answer("Пользователь разблокирован.", show_alert=True)
            try: await bot.send_message(user_id_to_change, "🟢 Ваш аккаунт был разблокирован администратором.")
            except Exception: pass
            
        await session.commit()
    
    # Удаляем старое сообщение и показываем обновленный профиль
    await callback.message.delete()
    await show_user_profile(callback.message, user_id_to_change)


@dp.callback_query(AdminCallback.filter(F.action.in_(["credit", "debit"])))
async def start_balance_change(callback: CallbackQuery, callback_data: AdminCallback, state: FSMContext):
    await state.set_state(AdminBalanceChange.enter_amount)
    await state.update_data(
        user_id=callback_data.user_id,
        action=callback_data.action,
        message_id_to_delete=callback.message.message_id # Сохраняем ID для удаления
    )
    action_text = "начислить" if callback_data.action == "credit" else "списать"
    await callback.answer()
    await callback.message.answer(f"Введите сумму, которую нужно {action_text} пользователю {callback_data.user_id}:")

@dp.message(AdminBalanceChange.enter_amount)
async def process_balance_change_amount(message: types.Message, state: FSMContext):
    try:
        amount = Decimal(message.text)
        if amount <= 0:
            return await message.answer("Сумма должна быть положительным числом.")
    except Exception:
        return await message.answer("Неверный формат. Введите число.")
        
    data = await state.get_data()
    action = data.get("action")
    user_id = data.get("user_id")
    
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == user_id))
        if not user:
            await message.answer("Пользователь не найден.")
            return await state.clear()

        if action == "credit":
            user.balance += amount
            session.add(FinancialTransaction(user_id=user_id, type='admin_credit', amount=amount))
        
            final_text = f"✅ Успешно начислено {amount:.2f} USDT пользователю {user_id}."
        else: # debit
            if user.balance < amount:
                await message.answer(f"Недостаточно средств. Баланс пользователя: {user.balance:.2f} USDT.")
                return await state.clear()
            user.balance -= amount
            session.add(FinancialTransaction(user_id=user_id, type='admin_debit', amount=-amount))
        
            final_text = f"✅ Успешно списано {amount:.2f} USDT с баланса пользователя {user_id}."

        await session.commit()
    
    await message.answer(final_text)
    
    # Удаляем старую карточку пользователя и сообщение с запросом суммы
    await bot.delete_message(message.chat.id, data.get("message_id_to_delete"))
    await message.delete()
    
    # Показываем обновленную карточку
    new_message = await message.answer(f"/user {user_id}")
    await get_user_info(new_message, CommandObject(command=Command(commands=['user']), args=str(user_id)))
    await state.clear()




# --- Логика отклика (FSM) ---
@dp.callback_query(OrderCallback.filter(F.action == "offer"))
@block_check
async def handle_make_offer_start(callback: CallbackQuery, callback_data: OrderCallback, state: FSMContext):
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        if not user:
            await callback.answer("Чтобы откликнуться, пожалуйста, сначала запустите бота командой /start", show_alert=True)
            return

        # --- ИЗМЕНЕНИЕ: Проверка лимита на отклики ---
        is_vip = user.vip_expires_at and user.vip_expires_at > datetime.now(UTC)
        if not is_vip:
            offers_count = await session.scalar(
                select(func.count(Offer.id)).where(Offer.executor_id == callback.from_user.id)
            )
            if offers_count >= 2:
                await callback.answer(
                    "Вы достигли лимита на отклики (10). Чтобы снять ограничения, приобретите VIP-статус.",
                    show_alert=True
                )
                return
        # =======================================================

        existing_offer = await session.scalar(
            select(Offer).where(Offer.order_id == callback_data.order_id, Offer.executor_id == callback.from_user.id)
        )
        if existing_offer:
            await callback.answer("Вы уже откликались на этот заказ.", show_alert=True)
            return

    await state.set_state(MakeOffer.enter_message)
    await state.update_data(order_id=callback_data.order_id)
    await callback.answer()
    await callback.message.answer("Введите сопроводительное сообщение для заказчика:\n\nДля отмены введите /cancel")


# ... (остальной код FSM отклика без изменений)
@dp.message(MakeOffer.enter_message)
async def handle_offer_message(message: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    async with async_session() as session:
        new_offer = Offer(order_id=order_id, executor_id=message.from_user.id, message=message.text)
        session.add(new_offer)
        order = await session.get(Order, order_id)
        if order:
            await session.commit()
            await message.answer("✅ Ваш отклик успешно отправлен!")
            try:
                await bot.send_message(order.customer_id, f"🔔 По вашему заказу №{order.id} ('{order.title}') новый отклик!")
            except Exception as e:
                logging.error(f"Не удалось уведомить заказчика {order.customer_id} о новом отклике: {e}")
        else:
            await message.answer("❌ Произошла ошибка. Заказ не найден.")
    await state.clear()


# --- Логика вывода средств (FSM) ---
@dp.callback_query(F.data == "withdraw")
async def start_withdrawal(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(Withdrawal.enter_amount)
    await callback.message.answer("Введите сумму для вывода в USDT.\n\nДля отмены введите /cancel")
# ... (остальной код FSM вывода без изменений)
@dp.message(Withdrawal.enter_amount)
async def enter_withdrawal_amount(message: types.Message, state: FSMContext):
    try:
        amount = Decimal(message.text)
        if amount <= 0:
            await message.answer("Сумма должна быть больше нуля. Попробуйте еще раз.")
            return
    except Exception:
        await message.answer("Неверный формат суммы. Введите число. Например: 15.5")
        return
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        if user.balance < amount:
            await message.answer(f"На вашем балансе недостаточно средств ({user.balance:.2f} USDT).")
            return
    await state.update_data(amount=amount)
    await state.set_state(Withdrawal.enter_address)
    await message.answer("Теперь введите ваш TRC-20 адрес для получения USDT.")
@dp.message(Withdrawal.enter_address)
async def enter_withdrawal_address(message: types.Message, state: FSMContext):
    address = message.text
    if not (address.startswith("T") and len(address) == 34):
        await message.answer("Неверный формат адреса TRC-20. Адрес должен начинаться с 'T' и состоять из 34 символов. Попробуйте еще раз.")
        return
    await state.update_data(address=address)
    data = await state.get_data()
    amount = data.get("amount")
    text = (f"<b>Пожалуйста, подтвердите вывод средств:</b>\n\n<b>Сумма:</b> {amount:.2f} USDT\n<b>На адрес:</b> <code>{address}</code>\n\n"
            "⚠️ **Внимание!** Проверьте адрес внимательно. В случае ошибки средства будут утеряны.")
    confirm_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_withdrawal_yes")], [types.InlineKeyboardButton(text="❌ Отменить", callback_data="confirm_withdrawal_no")]])
    await state.set_state(Withdrawal.confirm_withdrawal)
    await message.answer(text, reply_markup=confirm_keyboard)
@dp.callback_query(Withdrawal.confirm_withdrawal, F.data == "confirm_withdrawal_yes")
async def confirm_withdrawal(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("⏳ Обрабатываем ваш запрос на вывод...")
    data = await state.get_data()
    amount = data.get("amount")
    address = data.get("address")
    success, result = await create_payout(address, amount)
    if success:
        async with async_session() as session:
            user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
            if user and user.balance >= amount:
                user.balance -= amount
                session.add(FinancialTransaction(user_id=user.telegram_id, type='withdrawal', amount=-amount))
                await session.commit()
                await callback.message.edit_text(f"✅ Запрос на вывод {amount:.2f} USDT успешно создан. Средства поступят на ваш кошелек в ближайшее время.")
            else:
                await callback.message.edit_text("❌ Произошла ошибка сверки баланса. Обратитесь в поддержку.")
    else:
        await callback.message.edit_text(f"❌ Не удалось создать запрос на вывод.\nПричина: {result}\n\nПопробуйте позже или обратитесь в поддержку.")
    await state.clear()
@dp.callback_query(Withdrawal.confirm_withdrawal, F.data == "confirm_withdrawal_no")
async def cancel_withdrawal(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Вывод средств отменен.")


# --- Логика отзывов (FSM) ---
@dp.callback_query(ReviewCallback.filter(F.action == "start"))
async def start_review(callback: CallbackQuery, callback_data: ReviewCallback, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.set_state(LeaveReview.enter_rating)
    await state.update_data(order_id=callback_data.order_id, reviewee_id=callback_data.reviewee_id)
    rating_kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=f"{'⭐'*i}", callback_data=f"rating_{i}") for i in range(1, 6)]
    ])
    await callback.message.answer("Пожалуйста, оцените вашу сделку по 5-звездочной шкале:", reply_markup=rating_kb)
@dp.callback_query(LeaveReview.enter_rating, F.data.startswith("rating_"))
async def enter_rating(callback: CallbackQuery, state: FSMContext):
    rating = int(callback.data.split("_")[1])
    await state.update_data(rating=rating)
    await state.set_state(LeaveReview.enter_text)
    await callback.message.edit_text("Спасибо за оценку! Теперь, пожалуйста, напишите текстовый отзыв.")
@dp.message(LeaveReview.enter_text)
async def enter_review_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    rating = data.get("rating")
    order_id = data.get("order_id")
    reviewee_id = data.get("reviewee_id")
    async with async_session() as session:
        new_review = Review(
            order_id=order_id,
            reviewer_id=message.from_user.id,
            reviewee_id=reviewee_id,
            rating=rating,
            text=message.text
        )
        session.add(new_review)
        reviewee_user_stmt = select(User).where(User.telegram_id == reviewee_id)
        reviewee_user = await session.scalar(reviewee_user_stmt)
        if reviewee_user:
            old_total_rating = reviewee_user.rating * reviewee_user.reviews_count
            new_review_count = reviewee_user.reviews_count + 1
            new_average_rating = (old_total_rating + rating) / new_review_count
            reviewee_user.rating = new_average_rating
            reviewee_user.reviews_count = new_review_count
        await session.commit()
    await message.answer("✅ Спасибо, ваш отзыв принят!")
    await state.clear()




@dp.message(Command("profile"))
@block_check
async def get_public_profile(message: types.Message, command: CommandObject):
    user_identifier = command.args or str(message.from_user.id)
    
    async with async_session() as session:
        if user_identifier.isdigit():
            user = await session.scalar(select(User).where(User.telegram_id == int(user_identifier)))
        else:
            user = await session.scalar(select(User).where(User.username == user_identifier.replace("@", "")))

        if not user:
            return await message.answer("Пользователь не найден.")

        # Считаем завершенные сделки
        completed_deals = await session.scalar(
            select(func.count(Order.id)).where(
                or_(Order.customer_id == user.telegram_id, Order.executor_id == user.telegram_id),
                Order.status == 'completed'
            )
        )
        
        profile_text = (
            f"<b>👤 Профиль пользователя @{user.username if user.username else 'N/A'}</b>\n\n"
            f"<b>Рейтинг:</b> {user.rating:.2f} ⭐ ({user.reviews_count} отзывов)\n"
            f"<b>Завершено сделок:</b> {completed_deals}\n"
            f"<b>На сервисе с:</b> {user.registration_date.strftime('%d.%m.%Y')}"
        )
        await message.answer(profile_text)
        
        # Показываем последние 3 отзыва
        reviews = await session.scalars(
            select(Review).where(Review.reviewee_id == user.telegram_id).order_by(Review.id.desc()).limit(3)
        )
        
        reviews_list = reviews.all()
        if reviews_list:
            review_text = "\n<b>Последние отзывы:</b>\n"
            for review in reviews_list:
                review_text += f"  - <i>«{review.text}»</i> ({review.rating}⭐)\n"
            await message.answer(review_text)


@dp.callback_query(F.data == "deals_history")
@block_check
async def handle_deals_history(callback: CallbackQuery):
    await callback.answer()
    async with async_session() as session:
        completed_orders_result = await session.scalars(
            select(Order).where(
                or_(Order.customer_id == callback.from_user.id, Order.executor_id == callback.from_user.id),
                Order.status == 'completed'
            ).order_by(Order.creation_date.desc()).limit(10)
        )
        
        completed_orders = completed_orders_result.all()

        history_text = "<b>📜 История 10 последних завершенных сделок:</b>\n\n"
        if not completed_orders:
            history_text = "У вас пока нет завершенных сделок."
        else:
            for order in completed_orders:
                role = "Заказчик" if order.customer_id == callback.from_user.id else "Исполнитель"
                history_text += f"• <b>№{order.id}:</b> {order.title} ({order.price:.2f} USDT) - <i>Роль: {role}</i>\n"

        await callback.message.answer(history_text)

@dp.callback_query(F.data == "finance_history")
@block_check
async def handle_finance_history(callback: CallbackQuery):
    await callback.answer()
    async with async_session() as session:
        transactions_result = await session.scalars(
            select(FinancialTransaction)
            .where(FinancialTransaction.user_id == callback.from_user.id)
            .order_by(FinancialTransaction.timestamp.desc())
            .limit(15)
        )
        
        transactions = transactions_result.all()

        history_text = "<b>💸 История 15 последних операций с балансом:</b>\n\n"
        types_map = {
            'deposit': '✅ Пополнение', 'withdrawal': '➖ Вывод', 'order_payment': '🧾 Оплата заказа',
            'order_reward': '💰 Вознаграждение', 'dispute_resolution': '⚖️ Решение по спору',
            'admin_credit': '⚙️ Начисление', 'admin_debit': '⚙️ Списание'
        }

        if not transactions:
            history_text = "У вас пока не было операций с балансом."
        else:
            for trans in transactions:
                sign = "+" if trans.amount > 0 else ""
                type_str = types_map.get(trans.type, trans.type)
                history_text += f"• {trans.timestamp.strftime('%d.%m.%y %H:%M')}: {sign}{trans.amount:.2f} USDT ({type_str})\n"

        await callback.message.answer(history_text)

@dp.message(F.text == "🆘 Поддержка")
async def start_support_chat(message: types.Message, state: FSMContext):
    await state.set_state(SupportChat.in_chat)
    await message.answer(
        "Вы вошли в чат с поддержкой. Напишите ваше сообщение, и администратор скоро ответит.\n\n"
        "Чтобы выйти из чата, отправьте команду /cancel."
    )
# Шаг 2: Пользователь отправляет сообщение в поддержку
@dp.message(SupportChat.in_chat, F.text)
async def forward_to_admin(message: types.Message, state: FSMContext):
    user_info = f"@{message.from_user.username}" if message.from_user.username else f"ID: {message.from_user.id}"
    
    # Пересылаем сообщение админу
    await bot.send_message(
        ADMIN_ID,
        f"<b>Новое сообщение в поддержку от {user_info}</b> (ID: `{message.from_user.id}`)\n\n"
        f"Текст: {message.text}"
    )
    await message.answer("Ваше сообщение отправлено администратору.")

# Шаг 3: Админ отвечает на сообщение пользователя (используя функцию "Ответить")
@dp.message(F.reply_to_message, lambda msg: msg.from_user.id == ADMIN_ID)
async def forward_to_user(message: types.Message):
    # Получаем ID пользователя из текста оригинального сообщения
    try:
        replied_message_text = message.reply_to_message.text
        # Ищем ID пользователя в строке "(ID: `123456789`)"
        user_id_str = replied_message_text.split("(ID: `")[1].split("`)")[0]
        user_id = int(user_id_str)

        await bot.send_message(user_id, f"<b>Ответ от поддержки:</b>\n\n{message.text}")
        await message.answer("✅ Ваш ответ отправлен пользователю.")
    except (IndexError, ValueError):
        await message.answer("❌ Не удалось отправить ответ. Убедитесь, что вы отвечаете на правильное сообщение от бота.")
    except Exception as e:
        logging.error(f"Ошибка при ответе админа пользователю: {e}")
        await message.answer("❌ Произошла ошибка при отправке ответа.")

@dp.callback_query(F.data == "top_up")
async def handle_top_up(callback: CallbackQuery):
    await callback.answer()
    async with async_session() as session:
        stmt = select(User).where(User.telegram_id == callback.from_user.id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        if not user:
            await callback.message.answer("Произошла ошибка. Пожалуйста, нажмите /start.")
            return
        wallet = user.wallet_address
        if not wallet:
            new_wallet_address = await generate_new_wallet()
            if new_wallet_address:
                user.wallet_address = new_wallet_address
                wallet = new_wallet_address
                await session.commit()
            else:
                await callback.message.answer("Не удалось сгенерировать адрес для пополнения. Попробуйте позже.")
                return
        top_up_text = (f"Для пополнения баланса, переведите **USDT (в сети TRC-20)** на ваш персональный адрес:\n\n<code>{wallet}</code>\n\n"
                       "⚠️ **Внимание!** Отправляйте только USDT в сети TRC-20.")
        await callback.message.answer(top_up_text)

@dp.callback_query(OrderCallback.filter(F.action == "view"))
async def view_order_offers(callback: CallbackQuery, callback_data: OrderCallback):
    await callback.answer()
    async with async_session() as session:
        stmt = (select(Offer).where(Offer.order_id == callback_data.order_id)
                .options(joinedload(Offer.executor), joinedload(Offer.order)))
        result = await session.execute(stmt)
        offers = result.scalars().unique().all()
        if not offers:
            await callback.message.answer("На этот заказ пока нет откликов.")
            return
        order_title = offers[0].order.title if offers else "..."
        await callback.message.answer(f"<b>Отклики на заказ №{callback_data.order_id} ('{order_title}'):</b>")
        for offer in offers:
            executor = offer.executor
            executor_username = f"@{executor.username}" if executor.username else "Скрыт"
            text = (f"<b>Исполнитель:</b> {executor_username}\n<b>Рейтинг:</b> {executor.rating:.2f} ⭐ ({executor.reviews_count} отзывов)\n\n"
                    f"<b>Сообщение:</b> «<i>{offer.message}</i>»")
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(
                text="Выбрать этого исполнителя", callback_data=OfferCallback(action="select", offer_id=offer.id).pack())]])
            await callback.message.answer(text, reply_markup=keyboard)

@dp.callback_query(OfferCallback.filter(F.action == "select"))
async def select_executor(callback: CallbackQuery, callback_data: OfferCallback):
    async with async_session() as session:
        offer = await session.get(Offer, callback_data.offer_id, options=[joinedload(Offer.order)])
        if not offer or not offer.order:
            await callback.answer("Ошибка: отклик или заказ не найден.", show_alert=True)
            return
        order = offer.order
        if order.customer_id != callback.from_user.id:
            await callback.answer("Это не ваш заказ.", show_alert=True)
            return
        if order.status != "open":
            await callback.answer("Исполнитель для этого заказа уже выбран.", show_alert=True)
            return
        order.status = "in_progress"
        order.executor_id = offer.executor_id
        await session.commit()
        await callback.message.edit_text(f"✅ Исполнитель выбран для заказа №{order.id}!")
        try:
            await bot.send_message(offer.executor_id, f"🎉 Поздравляем! Вас выбрали исполнителем для заказа №{order.id} ('{order.title}'). Теперь вы можете общаться с заказчиком через этот чат.")
            await callback.message.answer(f"Вы можете начать общение с исполнителем через этот чат. Ваши сообщения будут пересылаться ему.")
        except Exception as e:
            logging.error(f"Не удалось уведомить исполнителя {offer.executor_id}: {e}")

@dp.callback_query(OrderCallback.filter(F.action == "submit_work"))
async def submit_work(callback: CallbackQuery, callback_data: OrderCallback):
    async with async_session() as session:
        order = await session.get(Order, callback_data.order_id)
        if not order or order.executor_id != callback.from_user.id:
            await callback.answer("Это не ваш заказ.", show_alert=True)
            return
        if order.status != "in_progress":
            await callback.answer("Этот заказ не в работе.", show_alert=True)
            return
        order.status = "pending_approval"
        await session.commit()
        await callback.message.edit_text("Вы сдали работу. Ожидаем подтверждения от заказчика.")
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="👍 Принять работу", callback_data=OrderCallback(action="accept_work", order_id=order.id).pack())],
            [types.InlineKeyboardButton(text="⛔️ Открыть спор", callback_data=OrderCallback(action="dispute", order_id=order.id).pack())]])
        await bot.send_message(order.customer_id, f"🔔 Исполнитель сдал работу по заказу №{order.id} ('{order.title}').\nПожалуйста, проверьте и примите работу.", reply_markup=keyboard)

@dp.callback_query(OrderCallback.filter(F.action == "accept_work"))
async def accept_work(callback: CallbackQuery, callback_data: OrderCallback):
    async with async_session() as session:
        order = await session.get(Order, callback_data.order_id, options=[joinedload(Order.executor)])
        
        if not order or order.customer_id != callback.from_user.id or order.status != "pending_approval":
            await callback.answer("Действие не может быть выполнено.", show_alert=True)
            return
        
        # --- Расчет и удержание комиссии ---
        executor = order.executor
        payout_amount = order.price
        commission_amount = Decimal("0.00")
        
        # Получаем комиссию из настроек
        commission_setting = await session.get(Setting, "commission_percent")
        if commission_setting and order.price > 0:
            commission_percent = Decimal(commission_setting.value)
            commission_amount = (order.price * commission_percent) / 100
            payout_amount = order.price - commission_amount

        # Зачисляем деньги и логируем транзакцию
        if payout_amount > 0:
            executor.balance += payout_amount
            session.add(FinancialTransaction(user_id=executor.telegram_id, type='order_reward', amount=payout_amount, order_id=order.id))
        
        order.status = "completed"
        await session.commit()
        
        await callback.message.edit_text(f"✅ Вы успешно приняли работу по заказу №{order.id}! Сделка завершена.")
        
        # Уведомляем исполнителя с учетом комиссии
        payout_info = f"{payout_amount:.2f} USDT зачислены на ваш баланс."
        if commission_amount > 0:
            payout_info += f" (удержана комиссия {commission_amount:.2f} USDT)"

        # Создаем клавиатуры для отзыва
        customer_review_kb = types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text="Оставить отзыв исполнителю", 
                                       callback_data=ReviewCallback(action="start", order_id=order.id, reviewee_id=order.executor_id).pack())
        ]])
        executor_review_kb = types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text="Оставить отзыв заказчику", 
                                       callback_data=ReviewCallback(action="start", order_id=order.id, reviewee_id=order.customer_id).pack())
        ]])

        # Отправляем уведомления и предложение оставить отзыв
        await bot.send_message(
            order.executor_id,
            f"🎉 Заказчик принял работу по заказу №{order.id} ('{order.title}').\n{payout_info}",
            reply_markup=executor_review_kb
        )
        await callback.message.answer("Спасибо за использование сервиса! Пожалуйста, оставьте отзыв о работе исполнителя.", reply_markup=customer_review_kb)
        
# --- ИЗМЕНЕНИЕ: РЕАЛИЗУЕМ ЛОГИКУ КНОПКИ "ОТКРЫТЬ СПОР" ---
@dp.callback_query(OrderCallback.filter(F.action == "dispute"))
async def open_dispute(callback: CallbackQuery, callback_data: OrderCallback):
    async with async_session() as session:
        order = await session.get(Order, callback_data.order_id)
        # ... (проверки заказа без изменений)
        if not order or order.customer_id != callback.from_user.id:
            await callback.answer("Это не ваш заказ.", show_alert=True)
            return
        if order.status not in ["pending_approval", "in_progress"]:
            await callback.answer("Спор по этому заказу уже нельзя открыть.", show_alert=True)
            return
        
        order.status = "dispute"
        await session.commit()

        await callback.message.edit_text(f"Вы открыли спор по заказу №{callback_data.order_id}. Администратор скоро свяжется с вами.")
        
        # Уведомляем администратора с кнопкой
        log_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text="📜 Получить лог чата", callback_data=OrderCallback(action="get_log", order_id=order.id).pack())
        ]])
        
        try:
            await bot.send_message(ADMIN_ID, f"⚠️ <b>Новый спор!</b>\nЗаказ №{order.id}: {order.title}\n\n"
                                             f"Используйте /dispute_info {order.id} для просмотра деталей.", reply_markup=log_keyboard)
            if order.executor_id:
                await bot.send_message(order.executor_id, f"🔴 Заказчик открыл спор по заказу №{order.id} ('{order.title}').\n"
                                                         "Ожидайте решения администратора.")
        except Exception as e:
            logging.error(f"Не удалось отправить уведомления о споре по заказу {order.id}: {e}")


# --- НОВЫЕ КОМАНДЫ ДЛЯ АДМИНИСТРАТОРА ---

@dp.message(Command("set_commission"))
@admin_only
async def set_commission(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer("Укажите процент комиссии. Пример: /set_commission 5")
    try:
        percent = Decimal(command.args)
        if not (0 <= percent <= 100):
            raise ValueError
    except Exception:
        return await message.answer("Ошибка. Введите число от 0 до 100.")

    async with async_session() as session:
        # Ищем настройку, или создаем новую
        commission_setting = await session.get(Setting, "commission_percent")
        if not commission_setting:
            commission_setting = Setting(key="commission_percent", value=str(percent))
            session.add(commission_setting)
        else:
            commission_setting.value = str(percent)
        
        await session.commit()
    await message.answer(f"✅ Новая комиссия установлена: {percent}%")

@dp.message(Command("dispute_info"))
@admin_only
async def get_dispute_info(message: types.Message, command: CommandObject):
    # Теперь эта команда показывает только краткую информацию
    # ... (код без изменений)
    if not command.args:
        return await message.answer("Пожалуйста, укажите ID заказа. Пример: /dispute_info 123")
    try:
        order_id = int(command.args)
    except ValueError:
        return await message.answer("ID заказа должен быть числом.")
    async with async_session() as session:
        order = await session.get(Order, order_id, options=[joinedload(Order.customer), joinedload(Order.executor)])
        if not order: return await message.answer(f"Заказ с ID {order_id} не найден.")
        customer_username = f"@{order.customer.username}" if order.customer.username else "N/A"
        executor_username = f"@{order.executor.username}" if order.executor and order.executor.username else "N/A"
        info_text = (f"<b>ℹ️ Инфо по спору (Заказ №{order.id})</b>\n\n"
                     f"<b>Название:</b> {order.title}\n<b>Цена:</b> {order.price:.2f} USDT\n<b>Статус:</b> {order.status}\n"
                     f"<b>Заказчик:</b> {customer_username} (ID: {order.customer_id})\n<b>Исполнитель:</b> {executor_username} (ID: {order.executor_id})\n\n"
                     f"Чтобы решить спор, используйте /resolve {order.id} customer|executor")
        await message.answer(info_text)

# НОВЫЙ ОБРАБОТЧИК ДЛЯ КНОПКИ "ПОЛУЧИТЬ ЛОГ"
@dp.callback_query(OrderCallback.filter(F.action == "get_log"))
async def get_chat_log_handler(callback: CallbackQuery, callback_data: OrderCallback):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("Эта кнопка только для администратора.", show_alert=True)
        
    order_id = callback_data.order_id
    await callback.answer(f"Отправляю лог заказа №{order_id} в канал...")
    
    async with async_session() as session:
        order = await session.get(Order, order_id, options=[joinedload(Order.customer), joinedload(Order.executor)])
        await bot.send_message(LOG_CHANNEL_ID, f"--- Лог чата для заказа №{order_id}: {order.title} ---")
        
        # === ИСПРАВЛЕНИЕ: Сразу получаем все сообщения в список ===
        chat_log_result = await session.scalars(select(ChatMessage).where(ChatMessage.order_id == order_id).order_by(ChatMessage.timestamp))
        chat_log = chat_log_result.all()
        # =======================================================
        
        if not chat_log:
             await bot.send_message(LOG_CHANNEL_ID, "Лог чата пуст.")
             await callback.message.answer(f"✅ Лог чата для заказа №{order_id} отправлен в канал (он пуст).")
             return

        for msg in chat_log:
            sender_role = "Заказчик" if msg.sender_id == order.customer_id else "Исполнитель"
            timestamp = msg.timestamp.strftime('%Y-%m-%d %H:%M')
            caption = f"<i>[{timestamp}]</i> <b>{sender_role}:</b>"
            
            if msg.content_type == 'text':
                await bot.send_message(LOG_CHANNEL_ID, f"{caption} {msg.text_content}")
            elif msg.content_type == 'photo':
                await bot.send_photo(LOG_CHANNEL_ID, msg.file_id, caption=caption)
            elif msg.content_type == 'voice':
                await bot.send_voice(LOG_CHANNEL_ID, msg.file_id, caption=caption)

    await callback.message.answer(f"✅ Лог чата для заказа №{order_id} отправлен в канал.")



@dp.message(Command("resolve"))
@admin_only
async def resolve_dispute(message: types.Message, command: CommandObject):
    args = (command.args or "").split()
    # === ИСПРАВЛЕНИЕ: МЕНЯЕМ ТЕКСТ ОШИБКИ ===
    if len(args) != 2:
        return await message.answer("Неверный формат. Используйте: /resolve `id_заказа` `winner`\n"
                                    "Где `winner` - 'customer' или 'executor'.")
    
    try:
        order_id = int(args[0])
        winner = args[1].lower()
        if winner not in ["customer", "executor"]:
            raise ValueError
    except ValueError:
        return await message.answer("Неверные аргументы. Пример: /resolve 123 customer")

    async with async_session() as session:
        order = await session.get(Order, order_id, options=[joinedload(Order.customer), joinedload(Order.executor)])
        if not order:
            return await message.answer(f"Заказ с ID {order_id} не найден.")
        if order.status != "dispute":
            return await message.answer(f"Заказ №{order_id} не находится в статусе спора.")

        if winner == "customer":
            if order.price > 0: order.customer.balance += order.price
            session.add(FinancialTransaction(user_id=order.customer_id, type='dispute_resolution', amount=order.price, order_id=order.id))
            
            winner_user = order.customer
            loser_user = order.executor
            resolution_text = f"Спор по заказу №{order.id} решен в пользу заказчика. Сумма {order.price:.2f} USDT возвращена на его баланс."
        else: # winner == "executor"
            if order.price > 0: order.executor.balance += order.price
            session.add(FinancialTransaction(user_id=order.executor_id, type='dispute_resolution', amount=order.price, order_id=order.id))
            
            winner_user = order.executor
            loser_user = order.customer
            resolution_text = f"Спор по заказу №{order.id} решен в пользу исполнителя. Сумма {order.price:.2f} USDT переведена на его баланс."
            
        order.status = "completed"
        await session.commit()
        
        await message.answer(f"✅ Спор успешно решен.\n{resolution_text}")
        
        try:
            await bot.send_message(winner_user.telegram_id, f"🟢 {resolution_text}")
            if loser_user: await bot.send_message(loser_user.telegram_id, f"🔴 {resolution_text}")
        except Exception as e:
            logging.error(f"Не удалось отправить уведомления о решении спора по заказу {order.id}: {e}")

@dp.message(F.document)
@block_check
async def handle_document_rejection(message: types.Message, state: FSMContext):
    # Проверяем, находится ли пользователь в активном чате
    if await state.get_state() is not None: return # Игнорируем, если пользователь в FSM
    user_id = message.from_user.id
    async with async_session() as session:
        active_order = await session.scalar(
            select(Order).where(Order.status == 'in_progress', or_(Order.customer_id == user_id, Order.executor_id == user_id))
        )
        if active_order:
            await message.reply("❌ Файлы должны отправляться только через файлообменник. В этом чате разрешено отправлять только ссылки.")


# --- ОБРАБОТЧИК ДЛЯ ЧАТА (ДОЛЖЕН БЫТЬ В САМОМ КОНЦЕ!) ---
@dp.message(F.text | F.photo | F.voice)
@block_check
async def handle_chat_messages(message: types.Message, state: FSMContext):
    if await state.get_state() is not None:
        if message.content_type != types.ContentType.TEXT: return
        if message.text in ["📝 Создать заказ", "📂 Мои заказы", "🔥 Лента заказов", "👤 Мой профиль", "🆘 Поддержка"]: return
        await message.answer("Пожалуйста, сначала завершите текущее действие или отмените его командой /cancel.")
        return

    user_id = message.from_user.id
    async with async_session() as session:
        active_order = await session.scalar(
            select(Order).where(Order.status == 'in_progress', or_(Order.customer_id == user_id, Order.executor_id == user_id))
        )
        if not active_order: return

        if user_id == active_order.customer_id:
            recipient_id, sender_prefix = active_order.executor_id, "<b>[Заказчик]:</b>"
        else:
            recipient_id, sender_prefix = active_order.customer_id, f"<b>[Исполнитель по заказу №{active_order.id}]:</b>"
        
        content_type = message.content_type.value
        text_content, file_path_to_save = None, None
        
        # --- НОВАЯ ЛОГИКА СКАЧИВАНИЯ ---
        if message.text:
            text_content = message.text
        elif message.photo:
            file_id = message.photo[-1].file_id
            text_content = message.caption
            file_info = await bot.get_file(file_id)
            file_ext = file_info.file_path.split('.')[-1]
            file_path_to_save = f"media/{file_info.file_unique_id}.{file_ext}"
            await bot.download_file(file_info.file_path, file_path_to_save)
        elif message.voice:
            file_id = message.voice.file_id
            file_info = await bot.get_file(file_id)
            file_ext = file_info.file_path.split('.')[-1]
            file_path_to_save = f"media/{file_info.file_unique_id}.{file_ext}"
            await bot.download_file(file_info.file_path, file_path_to_save)
        # --------------------------------

        session.add(ChatMessage(order_id=active_order.id, sender_id=user_id, content_type=content_type, text_content=text_content, file_path=file_path_to_save))
        await session.commit()
        
        try:
            if content_type == 'text':
                await bot.send_message(recipient_id, f"{sender_prefix}\n{text_content}")
            elif content_type == 'photo':
                await bot.send_photo(recipient_id, file_id, caption=f"{sender_prefix}\n{text_content or ''}")
            elif content_type == 'voice':
                await bot.send_voice(recipient_id, file_id, caption=sender_prefix)
        except Exception as e:
            logging.error(f"Не удалось переслать сообщение от {user_id} к {recipient_id}: {e}")
            await message.answer("❌ Не удалось доставить сообщение.")

# --- Основная функция для запуска ---
async def main():
    if not all([ADMIN_ID, LOG_CHANNEL_ID, ORDER_CHANNEL_ID]):
        logging.critical("Один или несколько обязательных ID (ADMIN_ID, LOG_CHANNEL_ID, ORDER_CHANNEL_ID) не указаны в .env файле!")
        return
        
    await create_tables()
    scheduler = AsyncIOScheduler(timezone="Etc/GMT")
    scheduler.add_job(check_payments, 'interval', minutes=2)
    scheduler.start()
    
    # Удаляем старые вебхуки и запускаем polling
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)
    
    await engine.dispose()
    scheduler.shutdown()

if __name__ == "__main__":
    print("Запускаем бота и проверку платежей...")
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Работа бота остановлена.")
# --- ШАГ 1: Загружаем переменные окружения в первую очередь ---
from dotenv import load_dotenv
load_dotenv()

# --- ШАГ 2: Теперь импортируем все остальное ---
import os
import asyncio
import logging
from decimal import Decimal

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from sqlalchemy import select, update, func, or_
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db_models import Base, User, Transaction, Order, Offer, ChatMessage # Добавили ChatMessage
from keyboards import main_menu_keyboard, balance_keyboard
from crypto_logic import generate_new_wallet, check_new_transactions
from states import OrderCreation, MakeOffer # Добавили MakeOffer

# ... (все настройки до хендлеров остаются без изменений) ...
logging.basicConfig(level=logging.INFO)
DB_URL = f"postgresql+asyncpg://{os.getenv('DB_USER')}:{os.getenv('DB_PASS')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
engine = create_async_engine(DB_URL)
async_session = async_sessionmaker(engine, expire_on_commit=False)
storage = MemoryStorage()
BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=storage)

class OrderCallback(CallbackData, prefix="order"):
    action: str
    order_id: int

class OfferCallback(CallbackData, prefix="offer"):
    action: str
    offer_id: int

async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def check_payments():
    # ... (код check_payments без изменений)
    async with async_session() as session:
        users_with_wallets = await session.execute(select(User).where(User.wallet_address.isnot(None)))
        for user in users_with_wallets.scalars().all():
            new_transactions = await check_new_transactions(user.wallet_address)
            for tx in new_transactions:
                existing_tx = await session.execute(select(Transaction).where(Transaction.txid == tx['txid']))
                if existing_tx.scalar_one_or_none(): continue
                user.balance += tx['amount']
                new_tx_record = Transaction(txid=tx['txid'])
                session.add(new_tx_record)
                try:
                    await bot.send_message(user.telegram_id, f"✅ Ваш баланс пополнен на <b>{tx['amount']:.2f} USDT</b>!")
                except Exception as e:
                    logging.error(f"Не удалось отправить уведомление о пополнении пользователю {user.telegram_id}: {e}")
        await session.commit()


# --- Хендлеры ---
# ... (handle_start, cancel_handler, и вся FSM для order_creation остаются без изменений) ...
@dp.message(CommandStart())
async def handle_start(message: types.Message, state: FSMContext):
    await state.clear()
    async with async_session() as session:
        stmt = select(User).where(User.telegram_id == message.from_user.id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        welcome_text = ""
        if user:
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
    if current_state is None:
        return
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu_keyboard)

@dp.message(F.text == "📝 Создать заказ")
async def order_creation_start(message: types.Message, state: FSMContext):
    await state.set_state(OrderCreation.enter_title)
    await message.answer("Введите название вашего заказа. Например, 'Разработать логотип'.\n\nДля отмены введите /cancel")

@dp.message(OrderCreation.enter_title)
async def enter_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(OrderCreation.enter_description)
    await message.answer("Отлично! Теперь введите подробное описание задачи.")

@dp.message(OrderCreation.enter_description)
async def enter_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(OrderCreation.enter_price)
    await message.answer("Теперь укажите цену заказа в USDT. Например: 50.5")

@dp.message(OrderCreation.enter_price)
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
        stmt = select(User).where(User.telegram_id == message.from_user.id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        if not user or (price > 0 and user.balance < price):
            balance = user.balance if user else Decimal("0.00")
            await message.answer(f"На вашем балансе недостаточно средств ({balance:.2f} USDT). Пожалуйста, пополните баланс и попробуйте снова.", reply_markup=main_menu_keyboard)
            await state.clear()
            return
    text = (f"<b>Пожалуйста, проверьте данные вашего заказа:</b>\n\n<b>Название:</b> {order_data['title']}\n<b>Описание:</b> {order_data['description']}\n<b>Цена:</b> {price:.2f} USDT\n\n"
            "Нажмите '✅ Создать', чтобы разместить заказ. С вашего баланса будет зарезервирована указанная сумма.")
    confirm_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="✅ Создать", callback_data="order_confirm")], [types.InlineKeyboardButton(text="❌ Отменить", callback_data="order_cancel")]])
    await state.set_state(OrderCreation.confirm_order)
    await message.answer(text, reply_markup=confirm_keyboard)

@dp.callback_query(OrderCreation.confirm_order, F.data == "order_confirm")
async def confirm_order_creation(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Создаем заказ...")
    order_data = await state.get_data()
    async with async_session() as session:
        price = order_data['price']
        stmt = select(User).where(User.telegram_id == callback.from_user.id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        if not user or (price > 0 and user.balance < price):
            await callback.message.edit_text("Ошибка! На вашем балансе больше недостаточно средств.")
            await state.clear()
            return
        if price > 0:
            user.balance -= price
        new_order = Order(title=order_data['title'], description=order_data['description'], price=price, customer_id=callback.from_user.id)
        session.add(new_order)
        await session.commit()
        await callback.message.edit_text(f"✅ Ваш заказ №{new_order.id} успешно создан!", reply_markup=None)
    await state.clear()

@dp.callback_query(OrderCreation.confirm_order, F.data == "order_cancel")
async def cancel_order_creation(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Создание заказа отменено.", reply_markup=None)


# --- ЛЕНТА ЗАКАЗОВ (без изменений) ---
@dp.message(Command("feed"))
async def handle_order_feed(message: types.Message):
    # ... (код handle_order_feed без изменений) ...
    async with async_session() as session:
        stmt = (select(Order).where(Order.status == "open", Order.customer_id != message.from_user.id)
                .options(joinedload(Order.customer)).order_by(Order.creation_date.desc()))
        result = await session.execute(stmt)
        orders = result.scalars().all()
        if not orders:
            await message.answer("На данный момент нет доступных заказов. Загляните позже!")
            return
        await message.answer("<b>🔥 Доступные заказы:</b>")
        for order in orders:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="🚀 Откликнуться", callback_data=OrderCallback(action="offer", order_id=order.id).pack())]])
            customer_username = f"@{order.customer.username}" if order.customer.username else "Скрыт"
            order_text = (f"<b>Заказ №{order.id}</b> | {order.title}\n<b>Цена:</b> {order.price:.2f} USDT\n<b>Заказчик:</b> {customer_username}\n\n<i>{order.description[:150]}...</i>")
            await message.answer(order_text, reply_markup=keyboard)


# --- НОВАЯ ЛОГИКА ОТКЛИКА НА ЗАКАЗ (FSM) ---
@dp.callback_query(OrderCallback.filter(F.action == "offer"))
async def handle_make_offer_start(callback: CallbackQuery, callback_data: OrderCallback, state: FSMContext):
    async with async_session() as session:
        existing_offer = await session.execute(
            select(Offer).where(Offer.order_id == callback_data.order_id, Offer.executor_id == callback.from_user.id)
        )
        if existing_offer.scalar_one_or_none():
            await callback.answer("Вы уже откликались на этот заказ.", show_alert=True)
            return

    await state.set_state(MakeOffer.enter_message)
    await state.update_data(order_id=callback_data.order_id)
    await callback.answer()
    await callback.message.answer("Введите сопроводительное сообщение для заказчика:")

@dp.message(MakeOffer.enter_message)
async def handle_offer_message(message: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    
    async with async_session() as session:
        new_offer = Offer(
            order_id=order_id,
            executor_id=message.from_user.id,
            message=message.text
        )
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


# --- МОИ ЗАКАЗЫ (логика для заказчика, без изменений) ---
@dp.message(F.text == "📂 Мои заказы")
async def handle_my_orders(message: types.Message):
    # ... (код handle_my_orders без изменений) ...
    async with async_session() as session:
        stmt = (select(Order).where(Order.customer_id == message.from_user.id)
                .order_by(Order.status.desc(), Order.creation_date.desc()))
        result = await session.execute(stmt)
        my_orders = result.scalars().all()
        if not my_orders:
            await message.answer("У вас пока нет созданных заказов. Чтобы найти работу, используйте команду /feed")
            return
        await message.answer("<b>🗂️ Ваши созданные заказы:</b>")
        for order in my_orders:
            offers_count_stmt = select(func.count(Offer.id)).where(Offer.order_id == order.id)
            offers_count = await session.scalar(offers_count_stmt)
            status_emoji = {"open": "🟢", "in_progress": "🟡", "completed": "⚪️", "dispute": "🔴"}
            text = (f"{status_emoji.get(order.status, '')} <b>Заказ №{order.id}:</b> {order.title}\n<b>Статус:</b> {order.status} | <b>Откликов:</b> {offers_count}")
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="Посмотреть отклики", callback_data=OrderCallback(action="view", order_id=order.id).pack())]])
            await message.answer(text, reply_markup=keyboard)


# --- Просмотр откликов (отображаем сообщение) ---
@dp.callback_query(OrderCallback.filter(F.action == "view"))
async def view_order_offers(callback: CallbackQuery, callback_data: OrderCallback):
    await callback.answer()
    async with async_session() as session:
        stmt = (
            select(Offer)
            .where(Offer.order_id == callback_data.order_id)
            .options(joinedload(Offer.executor), joinedload(Offer.order))
        )
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
            text = (
                f"<b>Исполнитель:</b> {executor_username}\n"
                f"<b>Рейтинг:</b> {executor.rating:.2f} ⭐ ({executor.reviews_count} отзывов)\n\n"
                f"<b>Сообщение:</b> «<i>{offer.message}</i>»"
            )
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(
                    text="Выбрать этого исполнителя",
                    callback_data=OfferCallback(action="select", offer_id=offer.id).pack()
                )]
            ])
            await callback.message.answer(text, reply_markup=keyboard)


# --- Выбор исполнителя (без изменений) ---
@dp.callback_query(OfferCallback.filter(F.action == "select"))
async def select_executor(callback: CallbackQuery, callback_data: OfferCallback):
    # ... (код select_executor без изменений) ...
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

# --- НОВЫЙ ОБРАБОТЧИК ДЛЯ ЧАТА ---
@dp.message(F.text)
async def handle_chat_messages(message: types.Message, state: FSMContext):
    # Убеждаемся, что пользователь не в процессе создания заказа/отклика
    if await state.get_state() is not None:
        await message.answer("Пожалуйста, сначала завершите текущее действие (создание заказа или отклика) или отмените его командой /cancel.")
        return

    user_id = message.from_user.id
    async with async_session() as session:
        # Ищем активный заказ, где пользователь либо заказчик, либо исполнитель
        stmt = select(Order).where(
            Order.status == 'in_progress',
            or_(Order.customer_id == user_id, Order.executor_id == user_id)
        )
        result = await session.execute(stmt)
        active_order = result.scalar_one_or_none()
        
        if not active_order:
            # Если активного заказа нет, перенаправляем на обработчики кнопок
            # (aiogram 3 сделает это автоматически, если этот хендлер не сработает)
            # Для надежности можно вернуть управление или просто ничего не делать
            return

        # Определяем, кто получатель
        if user_id == active_order.customer_id:
            recipient_id = active_order.executor_id
            sender_prefix = "<b>[Заказчик]:</b>"
        else:
            recipient_id = active_order.customer_id
            sender_prefix = f"<b>[Исполнитель по заказу №{active_order.id}]:</b>"
        
        # 1. Логируем сообщение в базу
        new_chat_message = ChatMessage(
            order_id=active_order.id,
            sender_id=user_id,
            message_text=message.text
        )
        session.add(new_chat_message)
        await session.commit()
        
        # 2. Пересылаем сообщение получателю
        try:
            await bot.send_message(recipient_id, f"{sender_prefix}\n{message.text}")
            await message.answer("✅ Сообщение отправлено.")
        except Exception as e:
            logging.error(f"Не удалось переслать сообщение от {user_id} к {recipient_id}: {e}")
            await message.answer("❌ Не удалось доставить сообщение.")


# --- Остальные хендлеры (баланс, пополнение, поддержка) без изменений ---
@dp.message(F.text == "💰 Мой баланс")
async def handle_balance(message: types.Message):
    async with async_session() as session:
        stmt = select(User).where(User.telegram_id == message.from_user.id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        if user:
            balance_text = f"<b>Ваш текущий баланс:</b>\n<code>{user.balance:.2f} USDT</code>"
            await message.answer(balance_text, reply_markup=balance_keyboard)
        else:
            await message.answer("Произошла ошибка. Пожалуйста, нажмите /start для регистрации.")

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

@dp.message(F.text == "🆘 Поддержка")
async def handle_support(message: types.Message):
    await message.answer("Вы выбрали 'Поддержка'. Этот функционал в разработке.")


# --- Основная функция для запуска ---
async def main():
    await create_tables()
    scheduler = AsyncIOScheduler(timezone="Etc/GMT")
    scheduler.add_job(check_payments, 'interval', minutes=2)
    scheduler.start()
    await dp.start_polling(bot)
    await engine.dispose()
    scheduler.shutdown()

if __name__ == "__main__":
    print("Запускаем бота и проверку платежей...")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Работа бота остановлена.")
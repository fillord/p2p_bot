import os
import sys
import secrets
from decimal import Decimal
from dotenv import load_dotenv

from fastapi import FastAPI, Request, Depends, HTTPException, status, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from passlib.context import CryptContext
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select, func
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from fastapi.staticfiles import StaticFiles

load_dotenv()

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from db_models import User, Order, FinancialTransaction, ChatMessage, Setting, Category

DB_URL = f"postgresql+asyncpg://{os.getenv('DB_USER')}:{os.getenv('DB_PASS')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
engine = create_async_engine(DB_URL)
async_session = async_sessionmaker(engine, expire_on_commit=False)

app = FastAPI(title="Admin Panel")
app.mount("/media", StaticFiles(directory="media"), name="media")
templates = Jinja2Templates(directory="admin_panel/templates")
bot = Bot(token=os.getenv("BOT_TOKEN"), default=DefaultBotProperties(parse_mode="HTML"))

security = HTTPBasic()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")

def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    correct_password = pwd_context.verify(credentials.password, ADMIN_PASSWORD_HASH)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            message="Incorrect email or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

@app.get("/", response_class=HTMLResponse, dependencies=[Depends(verify_credentials)])
async def read_root(request: Request):
    async with async_session() as session:
        users_result = await session.execute(select(User).order_by(User.registration_date.desc()))
        users = users_result.scalars().all()
        orders_result = await session.execute(
            select(Order).options(
                joinedload(Order.customer), 
                joinedload(Order.executor),
                joinedload(Order.category)
            ).order_by(Order.creation_date.desc())
        )
        orders = orders_result.scalars().unique().all()
        commission_setting = await session.get(Setting, "commission_percent")
        current_commission = commission_setting.value if commission_setting else "0"
        categories_result = await session.execute(select(Category).order_by(Category.name))
        categories = categories_result.scalars().all()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "users": users,
            "orders": orders,
            "commission_percent": current_commission,
            "categories": categories
        }
    )

@app.post("/categories/add", dependencies=[Depends(verify_credentials)])
async def add_category(category_name: str = Form(...)):
    if category_name and len(category_name) > 2:
        async with async_session() as session:
            existing_category = await session.scalar(select(Category).where(func.lower(Category.name) == category_name.lower()))
            if not existing_category:
                session.add(Category(name=category_name))
                await session.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/categories/{category_id}/delete", dependencies=[Depends(verify_credentials)])
async def delete_category(category_id: int):
    async with async_session() as session:
        category = await session.get(Category, category_id)
        if category:
            await session.delete(category)
            await session.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/settings/commission", dependencies=[Depends(verify_credentials)])
async def update_commission(percent: Decimal = Form(...)):
    if not (0 <= percent <= 100):
        return RedirectResponse(url="/", status_code=303)

    async with async_session() as session:
        commission_setting = await session.get(Setting, "commission_percent")
        if not commission_setting:
            commission_setting = Setting(key="commission_percent", value=str(percent))
            session.add(commission_setting)
        else:
            commission_setting.value = str(percent)
        await session.commit()
        
    return RedirectResponse(url="/", status_code=303)

@app.get("/orders/{order_id}/chat_log", response_class=HTMLResponse, dependencies=[Depends(verify_credentials)])
async def get_chat_log(request: Request, order_id: int):
    async with async_session() as session:
        order = await session.get(Order, order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        chat_log_res = await session.scalars(
            select(ChatMessage).where(ChatMessage.order_id == order_id).order_by(ChatMessage.timestamp)
        )
        chat_log = chat_log_res.all()

    return templates.TemplateResponse(
        "chat_log.html",
        {"request": request, "order": order, "chat_log": chat_log}
    )

@app.post("/orders/{order_id}/resolve", dependencies=[Depends(verify_credentials)])
async def resolve_dispute_from_panel(order_id: int, winner: str = Form(...)):
    async with async_session() as session:
        order = await session.get(Order, order_id, options=[joinedload(Order.customer), joinedload(Order.executor)])
        if not order or order.status != "dispute":
            return RedirectResponse(url="/", status_code=303)

        if winner == "customer":
            if order.price > 0:
                order.customer.balance += order.price
                session.add(FinancialTransaction(user_id=order.customer_id, type='dispute_resolution', amount=order.price, order_id=order.id))
            winner_user, loser_user = order.customer, order.executor
            resolution_text = f"Спор по заказу №{order.id} решен в пользу заказчика. Сумма {order.price:.2f} USDT возвращена на его баланс."
        elif winner == "executor":
            if order.price > 0:
                order.executor.balance += order.price
                session.add(FinancialTransaction(user_id=order.executor_id, type='dispute_resolution', amount=order.price, order_id=order.id))
            winner_user, loser_user = order.executor, order.customer
            resolution_text = f"Спор по заказу №{order.id} решен в пользу исполнителя. Сумма {order.price:.2f} USDT переведена на его баланс."
        else:
            return RedirectResponse(url="/", status_code=303)

        order.status = "completed"
        await session.commit()

        try:
            await bot.send_message(winner_user.telegram_id, f"🟢 {resolution_text}")
            if loser_user: await bot.send_message(loser_user.telegram_id, f"🔴 {resolution_text}")
        except Exception as e:
            print(f"Не удалось отправить уведомления о решении спора по заказу {order.id}: {e}")

    return RedirectResponse(url="/", status_code=303)

@app.post("/users/{user_id}/block", dependencies=[Depends(verify_credentials)])
async def block_user(user_id: int):
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == user_id))
        if user:
            user.is_blocked = True
            await session.commit()
            try:
                await bot.send_message(user_id, "🔴 Ваш аккаунт был заблокирован администратором.")
            except Exception as e:
                print(f"Не удалось отправить уведомление о блокировке пользователю {user_id}: {e}")
    return RedirectResponse(url="/", status_code=303)

@app.post("/users/{user_id}/unblock", dependencies=[Depends(verify_credentials)])
async def unblock_user(user_id: int):
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == user_id))
        if user:
            user.is_blocked = False
            await session.commit()
            try:
                await bot.send_message(user_id, "🟢 Ваш аккаунт был разблокирован администратором.")
            except Exception as e:
                print(f"Не удалось отправить уведомление о разблокировке пользователю {user_id}: {e}")
    return RedirectResponse(url="/", status_code=303)

@app.post("/users/{user_id}/credit", dependencies=[Depends(verify_credentials)])
async def credit_user_balance(user_id: int, amount: Decimal = Form(...)):
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == user_id))
        if user and amount > 0:
            user.balance += amount
            session.add(FinancialTransaction(user_id=user_id, type='admin_credit', amount=amount))
            await session.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/users/{user_id}/debit", dependencies=[Depends(verify_credentials)])
async def debit_user_balance(user_id: int, amount: Decimal = Form(...)):
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == user_id))
        if user and amount > 0 and user.balance >= amount:
            user.balance -= amount
            session.add(FinancialTransaction(user_id=user_id, type='admin_debit', amount=-amount))
            await session.commit()
    return RedirectResponse(url="/", status_code=303)
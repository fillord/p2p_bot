import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Numeric,
    BigInteger,
    ForeignKey,
    Text
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(32), nullable=True)
    balance = Column(Numeric(10, 2), default=0.00)
    wallet_address = Column(String(64), nullable=True, unique=True)
    rating = Column(Numeric(3, 2), default=5.00)
    reviews_count = Column(Integer, default=0)
    registration_date = Column(DateTime, default=datetime.datetime.utcnow)

    created_orders = relationship("Order", foreign_keys="Order.customer_id", back_populates="customer")
    executed_orders = relationship("Order", foreign_keys="Order.executor_id", back_populates="executor")
    offers = relationship("Offer", back_populates="executor")


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    title = Column(String(100), nullable=False)
    description = Column(String(1000), nullable=True)
    price = Column(Numeric(10, 2), nullable=False)
    status = Column(String(20), default="open", nullable=False)
    customer_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    executor_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=True)
    creation_date = Column(DateTime, default=datetime.datetime.utcnow)

    customer = relationship("User", foreign_keys=[customer_id], back_populates="created_orders")
    executor = relationship("User", foreign_keys=[executor_id], back_populates="executed_orders")
    offers = relationship("Offer", back_populates="order", cascade="all, delete-orphan")
    # Связь с сообщениями чата
    chat_messages = relationship("ChatMessage", back_populates="order", cascade="all, delete-orphan")


class Offer(Base):
    __tablename__ = "offers"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    executor_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    # Новое поле для сообщения
    message = Column(Text, nullable=True)
    
    order = relationship("Order", back_populates="offers")
    executor = relationship("User", back_populates="offers")


# Новая таблица для сообщений в чате
class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    sender_id = Column(BigInteger, nullable=False)
    message_text = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)

    order = relationship("Order", back_populates="chat_messages")


class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    txid = Column(String(128), unique=True, nullable=False, index=True)
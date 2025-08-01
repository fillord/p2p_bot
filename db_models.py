import datetime
from sqlalchemy import (
    Column, Integer, String, DateTime, Numeric,
    BigInteger, ForeignKey, Text, Boolean
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
    is_blocked = Column(Boolean, default=False, nullable=False)
    
    created_orders = relationship("Order", foreign_keys="Order.customer_id", back_populates="customer")
    executed_orders = relationship("Order", foreign_keys="Order.executor_id", back_populates="executor")
    offers = relationship("Offer", back_populates="executor")
    reviews_written = relationship("Review", foreign_keys="Review.reviewer_id", back_populates="reviewer")
    reviews_received = relationship("Review", foreign_keys="Review.reviewee_id", back_populates="reviewee")
    # Эта связь теперь будет работать
    financial_transactions = relationship("FinancialTransaction", back_populates="user")

# ... (Классы Order, Offer, ChatMessage, Review, Transaction остаются без изменений)
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
    chat_messages = relationship("ChatMessage", back_populates="order", cascade="all, delete-orphan")
    reviews = relationship("Review", back_populates="order", cascade="all, delete-orphan")
class Offer(Base):
    __tablename__ = "offers"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    executor_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    message = Column(Text, nullable=True)
    order = relationship("Order", back_populates="offers")
    executor = relationship("User", back_populates="offers")
class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    sender_id = Column(BigInteger, nullable=False)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    content_type = Column(String(20), nullable=False)
    text_content = Column(Text, nullable=True)
    file_id = Column(String(255), nullable=True)
    order = relationship("Order", back_populates="chat_messages")
class Review(Base):
    __tablename__ = "reviews"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    reviewer_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    reviewee_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    rating = Column(Integer, nullable=False)
    text = Column(Text, nullable=True)
    order = relationship("Order", back_populates="reviews")
    reviewer = relationship("User", foreign_keys=[reviewer_id], back_populates="reviews_written")
    reviewee = relationship("User", foreign_keys=[reviewee_id], back_populates="reviews_received")
class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    txid = Column(String(128), unique=True, nullable=False, index=True)
class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(50), primary_key=True)
    value = Column(String(255), nullable=False)
class FinancialTransaction(Base):
    __tablename__ = "financial_transactions"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    type = Column(String(50), nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    order_id = Column(Integer, nullable=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)

    # === ИСПРАВЛЕНИЕ: ДОБАВЛЯЕМ ОБРАТНУЮ СВЯЗЬ ===
    user = relationship("User", back_populates="financial_transactions")
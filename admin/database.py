"""Database models and connection for admin service"""
import os
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text, Date, BigInteger
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime
import uuid

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/xui_central")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# Payment status constants
class PaymentStatus:
    TEST = 1           # Тест
    PAID = 2           # Оплатил
    NOT_PAID = 3       # Не оплатил
    PROMO = 4          # Промокод


class Node(Base):
    __tablename__ = "nodes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False)
    url = Column(String(512), nullable=False)  # API URL (Tailscale IP, e.g., https://100.64.1.5:2053)
    domain = Column(String(255), nullable=False)  # Public domain for VLESS URLs (e.g., vienna.example.com)
    username = Column(String(255), nullable=False)
    password = Column(String(255), nullable=False)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    keys = relationship("Key", back_populates="node", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    name = Column(String(255))
    payment_status = Column(Integer, default=PaymentStatus.TEST)
    limit_ip = Column(Integer, default=0)  # 0 = unlimited
    tag = Column(String(100))
    payment_date = Column(Date)
    renewal_date = Column(Date)  # For TEST users: created_at + 72 hours
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 1:1 relationship with Client (cascade delete to client when user is deleted)
    client = relationship("Client", back_populates="user", uselist=False, cascade="all, delete")


class Client(Base):
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    enabled = Column(Boolean, default=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    keys = relationship("Key", back_populates="client", cascade="all, delete-orphan")
    user = relationship("User", back_populates="client")


class Key(Base):
    __tablename__ = "keys"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    node_id = Column(Integer, ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    inbound_id = Column(Integer, nullable=False)
    uuid = Column(UUID(as_uuid=True), nullable=False, default=uuid.uuid4)
    vless_url = Column(Text, nullable=False)
    manual = Column(Boolean, default=False)  # True for manually entered keys, False for auto-generated
    created_at = Column(DateTime, default=datetime.utcnow)

    client = relationship("Client", back_populates="keys")
    node = relationship("Node", back_populates="keys")


def get_db():
    """Dependency to get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

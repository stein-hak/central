"""Database models and connection for admin service"""
import os
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text, Date, BigInteger, ARRAY
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
    upgraded = Column(Boolean, default=False)  # True if node has synced clients (uses HA ports)
    proxy_only = Column(Boolean, default=False)  # If True, only show through proxy (hide direct access)
    created_at = Column(DateTime, default=datetime.utcnow)

    keys = relationship("Key", back_populates="node", cascade="all, delete-orphan")
    proxy_backends = relationship("ProxyBackend", back_populates="node", cascade="all, delete-orphan")


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


class Domain(Base):
    """Public domains for VLESS URLs"""
    __tablename__ = "domains"

    id = Column(Integer, primary_key=True, index=True)
    domain = Column(String(255), unique=True, nullable=False, index=True)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class NodeDomain(Base):
    """Many-to-many: nodes can serve multiple domains"""
    __tablename__ = "node_domains"

    id = Column(Integer, primary_key=True, index=True)
    node_id = Column(Integer, ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    domain_id = Column(Integer, ForeignKey("domains.id", ondelete="CASCADE"), nullable=False)
    is_primary = Column(Boolean, default=False)
    enabled = Column(Boolean, default=True)
    display_name = Column(String(100))  # Override node name to appear as different server
    created_at = Column(DateTime, default=datetime.utcnow)

    domain = relationship("Domain")
    node = relationship("Node")


class SubscriptionDomain(Base):
    """Domains for subscription service (to handle blocks/rotation)"""
    __tablename__ = "subscription_domains"

    id = Column(Integer, primary_key=True, index=True)
    domain = Column(String(255), unique=True, nullable=False, index=True)
    enabled = Column(Boolean, default=True)
    is_primary = Column(Boolean, default=False)  # Primary domain used by default
    notes = Column(Text)  # Optional notes (e.g., "Blocked on 2025-05-27")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Proxy(Base):
    """HAProxy front-end servers for client access"""
    __tablename__ = "proxies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False, index=True)
    domain = Column(String(255), nullable=False)
    fake_snis = Column(ARRAY(Text))  # Array of fake SNI domains
    sni_strategy = Column(String(20), default='random')  # random | fixed | rotate
    allowed_transport = Column(String(20), default='xhttp')  # xhttp | grpc | tcp - transport filter
    enabled = Column(Boolean, default=True)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    proxy_backends = relationship("ProxyBackend", back_populates="proxy", cascade="all, delete-orphan")


class ProxyBackend(Base):
    """Many-to-many: which backend nodes are behind which proxies"""
    __tablename__ = "proxy_backends"

    id = Column(Integer, primary_key=True, index=True)
    proxy_id = Column(Integer, ForeignKey("proxies.id", ondelete="CASCADE"), nullable=False)
    node_id = Column(Integer, ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    weight = Column(Integer, default=1)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    proxy = relationship("Proxy", back_populates="proxy_backends")
    node = relationship("Node", back_populates="proxy_backends")


def get_db():
    """Dependency to get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

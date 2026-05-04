"""Read-only database access for subscription service"""
import os
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.dialects.postgresql import UUID

# Use read-only database user
DATABASE_URL = os.getenv("DATABASE_URL_READONLY", "postgresql://sub_readonly:sub_readonly_password@localhost:5432/xui_central")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Node(Base):
    __tablename__ = "nodes"

    id = Column(Integer, primary_key=True)
    name = Column(String(255))
    domain = Column(String(255))
    enabled = Column(Boolean)
    upgraded = Column(Boolean)  # True if node has synced clients (uses HA ports)

    keys = relationship("Key", back_populates="node")


class Client(Base):
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False)
    enabled = Column(Boolean)

    keys = relationship("Key", back_populates="client")


class Key(Base):
    __tablename__ = "keys"

    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"))
    node_id = Column(Integer, ForeignKey("nodes.id"))
    vless_url = Column(Text, nullable=False)
    uuid = Column(UUID(as_uuid=True))  # Client UUID for URL regeneration

    client = relationship("Client", back_populates="keys")
    node = relationship("Node", back_populates="keys")


class Domain(Base):
    """Public domains for VLESS URLs"""
    __tablename__ = "domains"

    id = Column(Integer, primary_key=True)
    domain = Column(String(255), unique=True, nullable=False)
    enabled = Column(Boolean, default=True)


class NodeDomain(Base):
    """Many-to-many: nodes can serve multiple domains"""
    __tablename__ = "node_domains"

    id = Column(Integer, primary_key=True)
    node_id = Column(Integer, ForeignKey("nodes.id"))
    domain_id = Column(Integer, ForeignKey("domains.id"))
    is_primary = Column(Boolean, default=False)
    enabled = Column(Boolean, default=True)
    display_name = Column(String(100))  # Override node name (e.g., "node-france")

    domain = relationship("Domain")
    node = relationship("Node")


def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

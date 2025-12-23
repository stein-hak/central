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
    vless_url = Column(Text, nullable=False)

    client = relationship("Client", back_populates="keys")


def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

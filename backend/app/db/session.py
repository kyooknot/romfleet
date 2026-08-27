from __future__ import annotations
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from app.core.config import settings
from app.db.models import Base

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
    connect_args={"client_encoding": "utf8"},   # game titles/filenames are UTF-8
)
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create all tables (Phase 1 — Alembic migrations come with the API layer)."""
    Base.metadata.create_all(engine)

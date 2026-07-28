import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


# Development connects to PostgreSQL in Docker Compose by default.
# Override it with the DATABASE_URL environment variable when deploying.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://sitepulse:sitepulse_dev@localhost:5432/sitepulse",
)

# The engine manages the connection pool; it is not an individual connection.
engine = create_async_engine(
    DATABASE_URL,
    echo=True,  # Log SQL while learning; disable this in production.
    pool_pre_ping=True,
)

# A session is the unit of work for database operations.
SessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy table models."""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides an isolated database session per request."""

    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise

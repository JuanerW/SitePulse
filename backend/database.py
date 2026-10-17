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
    # SQL logging floods the JSON logs; turn it on locally with SQL_ECHO=1.
    echo=os.getenv("SQL_ECHO") == "1",
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

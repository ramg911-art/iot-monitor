"""Async database engine and session management."""
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.models import Base

logger = logging.getLogger(__name__)

_settings = get_settings()


def get_engine():
    """Create async engine. Use StaticPool for SQLite to avoid threading issues."""
    if "sqlite" in _settings.database_url:
        return create_async_engine(
            _settings.database_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            echo=_settings.debug,
        )
    return create_async_engine(_settings.database_url, echo=_settings.debug)


engine = get_engine()
async_session_maker = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


async def init_db() -> None:
    """Create all tables and default admin user."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Create default admin if none exists
    from app.models import User
    from app.services.auth_service import get_password_hash
    from app.config import get_settings
    from sqlalchemy import select
    async with async_session_maker() as session:
        result = await session.execute(select(User).limit(1))
        if result.scalars().first() is None:
            settings = get_settings()
            admin = User(
                username=settings.admin_username,
                password_hash=get_password_hash(settings.admin_password),
                is_active=True,
            )
            session.add(admin)
            await session.commit()
            logger.info("Created default admin user: %s", settings.admin_username)
    logger.info("Database initialized")


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional scope for database operations."""
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for database session."""
    async with get_session() as session:
        yield session

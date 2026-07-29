"""
数据库连接与会话管理
默认使用 SQLite + aiosqlite（开发/演示），生产环境通过 DATABASE_URL 切换为 PostgreSQL。

P0-3: PostgreSQL 连接池调优
- pool_size=20: 常驻连接数（覆盖正常并发）
- max_overflow=10: 突发流量可临时扩容的额外连接
- pool_recycle=3600: 1小时回收，防止长连接被 DB 端断开（MySQL wait_timeout / PG idle_timeout）
- pool_pre_ping=True: 取连接前发 SELECT 1 检测存活，避免使用已断开的连接
- SQLite 使用 NullPool（单文件数据库无需连接池，避免线程安全问题）
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import NullPool

from core.config import get_settings

settings = get_settings()

DATABASE_URL = settings.database_url

_is_sqlite = DATABASE_URL.startswith("sqlite")

# P0-3: 按数据库类型选择连接池策略
if _is_sqlite:
    # SQLite: NullPool（单文件 DB 无需池化，避免线程安全 + WAL 锁竞争）
    engine = create_async_engine(
        DATABASE_URL,
        echo=settings.debug,
        future=True,
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
else:
    # PostgreSQL / MySQL: 显式配置连接池参数
    engine = create_async_engine(
        DATABASE_URL,
        echo=settings.debug,
        future=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=settings.db_pool_pre_ping,
    )

# SQLite 启用 WAL 模式提升并发写入
if _is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

Base = declarative_base()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：获取数据库会话"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """业务层/非 FastAPI 路径用的 async context manager。

    与 get_db 区别:get_db 是 FastAPI Depends 用的 generator(无法独立 async with);
    get_db_session 是 asynccontextmanager,可在 service/loader 内 `async with` 使用。
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db() -> None:
    """创建所有数据表"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """关闭数据库连接"""
    await engine.dispose()

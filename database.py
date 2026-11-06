import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, func, select
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Для продакшена замени на: postgresql+asyncpg://user:pass@localhost/dbname
DATABASE_URL = "sqlite+aiosqlite:///./courses_bot.db"

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # Telegram ID
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    orders: Mapped[List["Order"]] = relationship(back_populates="user")


class Package(Base):
    __tablename__ = "packages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # 'base', 'plus', 'vip'
    title: Mapped[str] = mapped_column(String(64))
    price: Mapped[int] = mapped_column(Integer)  # Цена в BYN
    description: Mapped[str] = mapped_column(Text)
    # Список словарей вида: [{"name": "Название", "url": "https://..."}]
    links: Mapped[list] = mapped_column(JSON)

    orders: Mapped[List["Order"]] = relationship(back_populates="package")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    package_id: Mapped[str] = mapped_column(String(32), ForeignKey("packages.id"))
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus), default=OrderStatus.PENDING, index=True
    )
    receipt_file_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="orders")
    package: Mapped["Package"] = relationship(back_populates="orders")


async def init_db():
    """Создание таблиц и предзаполнение тарифов при первом запуске."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as session:
        existing = await session.scalar(select(Package).limit(1))
        if not existing:
            packages = [
                Package(
                    id="base",
                    title="Тариф Base",
                    price=50,
                    description="Базовая информация о выкупе товаров.",
                    links=[{"name": "📘 Базовые материалы по выкупу", "url": "https://t.me/+base_secret_link"}],
                ),
                Package(
                    id="plus",
                    title="Тариф Plus",
                    price=250,
                    description="Проверенная база карго-компаний для быстрой логистики.",
                    links=[{"name": "🚚 База карго-компаний", "url": "https://t.me/+plus_cargo_link"}],
                ),
                Package(
                    id="vip",
                    title="Тариф VIP",
                    price=500,
                    description="Всё вышеперечисленное + база поставщиков и индивидуальный подход.",
                    links=[
                        {"name": "🚚 База карго-компаний", "url": "https://t.me/+vip_cargo_link"},
                        {"name": "🏭 Закрытая база поставщиков", "url": "https://t.me/+vip_suppliers_link"},
                    ],
                ),
            ]
            session.add_all(packages)
            await session.commit()

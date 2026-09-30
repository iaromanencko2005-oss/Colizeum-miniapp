"""
Настройки подключения к базе данных.

По умолчанию используется локальный файл SQLite (colizeum.db). ВАЖНО: на
Railway без отдельной базы этот файл стирается при каждом деплое — вместе
с балансами гостей. Для постоянного хранения добавь в проект Railway
сервис PostgreSQL и пропиши переменную DATABASE_URL — код подхватит её сам.
"""
import logging
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

logger = logging.getLogger("colizeum")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./colizeum.db")
# Некоторые сервисы выдают адрес вида postgres://, а SQLAlchemy 2 ждёт postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

IS_SQLITE = DATABASE_URL.startswith("sqlite")
connect_args = {"check_same_thread": False} if IS_SQLITE else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _sql_literal(value) -> str | None:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def add_missing_columns():
    """Досоздаёт колонки, которые появились в models.py после того, как
    таблица уже была создана. Base.metadata.create_all() создаёт только
    новые таблицы, а в существующие колонки не добавляет — без этого шага
    любое обновление с новым полем ломало бы постоянную базу (PostgreSQL).
    Работает только на добавление, ничего не удаляет и не меняет."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                coltype = col.type.compile(dialect=engine.dialect)
                ddl = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {coltype}'
                default = None
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    default = _sql_literal(col.default.arg)
                if default is not None:
                    ddl += f" DEFAULT {default}"
                elif not col.nullable:
                    logger.warning("Пропускаю колонку %s.%s: NOT NULL без значения по умолчанию",
                                   table.name, col.name)
                    continue
                conn.execute(text(ddl))
                logger.info("Добавлена колонка %s.%s", table.name, col.name)

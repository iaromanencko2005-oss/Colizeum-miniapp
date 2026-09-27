"""
Настройки подключения к базе данных.

По умолчанию используется локальный файл SQLite (colizeum.db) — этого
достаточно для теста на локальной машине и даже для самого старта
на Railway. Как только клиентов станет больше и понадобится надёжность,
достаточно задать переменную окружения DATABASE_URL (Railway сам
подставит адрес PostgreSQL, если добавить его как отдельный сервис) —
код менять не придётся.
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./colizeum.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

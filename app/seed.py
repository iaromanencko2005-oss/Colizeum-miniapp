"""
Разовый скрипт: добавляет пример акций, чтобы мини-приложение не было
пустым при первом запуске. Запуск: python -m app.seed (из папки backend).
Безопасно запускать повторно — не дублирует, если акции уже есть.
"""
from .database import Base, engine, SessionLocal
from .models import Promotion

Base.metadata.create_all(bind=engine)


def run():
    db = SessionLocal()
    try:
        if db.query(Promotion).count() > 0:
            print("Акции уже есть в базе — пропускаю.")
            return

        db.add_all([
            Promotion(
                title="Счастливые часы 10:00–14:00",
                description="-20% на все тарифы каждый будний день до 14:00",
                min_tier="silver",
            ),
            Promotion(
                title="День рождения",
                description="Бесплатный час игры в день рождения — покажи админу паспорт",
                min_tier="silver",
            ),
            Promotion(
                title="Gold-бонус",
                description="Приоритетная бронь places без очереди",
                min_tier="gold",
            ),
            Promotion(
                title="Premium-зона",
                description="Доступ в приватную VIP-зону без доплаты",
                min_tier="premium",
            ),
        ])
        db.commit()
        print("Добавлены тестовые акции.")
    finally:
        db.close()


if __name__ == "__main__":
    run()

"""
Разовый скрипт: добавляет актуальные акции клуба, чтобы мини-приложение
не было пустым при первом запуске. Запуск: python -m app.seed.
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
                title="Счастливые часы",
                description="-20% на все тарифы каждый будний день до 14:00",
            ),
            Promotion(
                title="День рождения",
                description=(
                    "Бесплатный час игры в день рождения — покажи админу паспорт. "
                    "Бесплатный час получают все приглашённые"
                ),
            ),
            Promotion(
                title="Приведи друга",
                description="По 200 бонусов тебе и другу за первое посещение по твоей рекомендации",
            ),
            Promotion(
                title="Ночной пакет",
                description="-15% на короткую ночь — с 22:00 до 6:00",
            ),
            Promotion(
                title="Отзыв на картах",
                description="Оставь отзыв о клубе на Яндекс.Картах и получи 100 бонусных рублей",
                link="https://yandex.ru/maps/org/colizeum/227193289093/reviews/?ll=37.568227%2C55.738865&z=16.56",
            ),
        ])
        db.commit()
        print("Добавлены актуальные акции клуба.")
    finally:
        db.close()


if __name__ == "__main__":
    run()

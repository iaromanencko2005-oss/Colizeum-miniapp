"""
Справочник клуба для разделов «Игры в клубе» и «Зоны и ПК».

Список игр можно править прямо здесь (через GitHub): одна строка — одна игра.
Поля: название, жанр, метки (короткие пометки, которые увидит гость).
Жанры — ключи из GENRES. Метки — любые короткие слова: «FACEIT», «свой аккаунт», «PS5».
"""

GENRES = {
    "shooter": "Шутеры",
    "moba": "MOBA",
    "br": "Королевская битва",
    "sport": "Спорт и гонки",
    "coop": "Кооп и выживание",
    "other": "Другое",
}

# (название, жанр, метки)
GAMES = [
    ("Counter-Strike 2", "shooter", ["FACEIT", "Premier"]),
    ("Valorant", "shooter", ["свой аккаунт"]),
    ("Rainbow Six Siege", "shooter", []),
    ("Overwatch 2", "shooter", []),
    ("Escape from Tarkov", "shooter", ["свой аккаунт"]),
    ("Warface", "shooter", []),
    ("Dota 2", "moba", ["Faceit"]),
    ("Deadlock", "moba", []),
    ("League of Legends", "moba", ["свой аккаунт"]),
    ("PUBG: Battlegrounds", "br", []),
    ("Apex Legends", "br", []),
    ("Fortnite", "br", []),
    ("Call of Duty: Warzone", "br", []),
    ("Marvel Rivals", "shooter", []),
    ("EA SPORTS FC", "sport", ["PS5"]),
    ("UFC 5", "sport", ["PS5"]),
    ("Mortal Kombat 1", "sport", ["PS5"]),
    ("Forza Horizon 5", "sport", []),
    ("Assetto Corsa", "sport", []),
    ("Мир танков", "other", []),
    ("GTA V / GTA Online", "other", ["PS5"]),
    ("Rust", "coop", ["свой аккаунт"]),
    ("Minecraft", "coop", ["свой аккаунт"]),
    ("Lethal Company", "coop", []),
    ("Phasmophobia", "coop", []),
    ("Dead by Daylight", "coop", []),
    ("Genshin Impact", "other", []),
    ("World of Warcraft", "other", ["свой аккаунт"]),
]


def games_payload() -> dict:
    return {
        "genres": [{"id": k, "name": v} for k, v in GENRES.items()],
        "games": [{"name": n, "genre": g if g in GENRES else "other", "tags": list(t)} for n, g, t in GAMES],
    }

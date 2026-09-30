def normalize_phone(phone) -> str:
    """Оставляет только цифры и берёт последние 10 — так номера сверяются
    правильно, даже если где-то записаны по-разному: +7 999 123-45-67,
    89991234567, 79991234567, 9991234567 дают один и тот же результат."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits

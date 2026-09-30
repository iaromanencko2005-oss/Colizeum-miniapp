"""
Разбор Excel-файла с пополнениями, который админ присылает боту.

Понимает три формата:
0. Выгрузка из CRM «Лог финансовых операций» — ОСНОВНОЙ: все пополнения
   (касса, личный кабинет, приложение). Телефон берётся из колонки
   «Название операции» вида «Телефон (9991234567)», сумма — из «Сумма
   операции», дата — из «Дата операции». Строки «Пополнение» прибавляются,
   строки с «возврат» в типе операции вычитаются, прочие пропускаются.
1. Выгрузка из CRM «Лог ручных начислений/списаний» — только касса.
   Бот сам находит колонки «Телефон», «Баланс», «Дата», берёт только строки
   с пополнением > 0 (бонусы за рулетку с нулевым «Балансом» пропускает) и
   считает для каждого гостя среднее в месяц:
       сумма пополнений ÷ число месяцев с первого пополнения гостя
       до последней даты в файле (включительно).
2. Простой файл из двух столбцов: телефон и уже посчитанная сумма в месяц.
"""
from datetime import date, datetime

from .phones import normalize_phone

DATE_FORMATS = ("%d.%m.%y %H:%M", "%d.%m.%Y %H:%M", "%d.%m.%y", "%d.%m.%Y",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def _to_float(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _to_datetime(x):
    if isinstance(x, datetime):
        return x
    if isinstance(x, date):
        return datetime(x.year, x.month, x.day)
    s = str(x or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _months_inclusive(start: datetime, end: datetime) -> int:
    return (end.year - start.year) * 12 + end.month - start.month + 1


def parse_topups(rows):
    """Возвращает (формат, {телефон: {"avg": ..., "total": ...}}, сводка)."""
    rows = list(rows)
    for i, row in enumerate(rows[:10]):
        cells = [str(c).strip().lower() if c is not None else "" for c in row]
        if "название операции" in cells and "сумма операции" in cells and "дата операции" in cells:
            return _parse_operations_log(
                rows[i + 1:], cells.index("название операции"), cells.index("сумма операции"),
                cells.index("дата операции"),
                cells.index("тип операции") if "тип операции" in cells else None,
            )
        if "телефон" in cells and "баланс" in cells and "дата" in cells:
            return _parse_crm_log(rows[i + 1:], cells.index("телефон"),
                                  cells.index("баланс"), cells.index("дата"))
    return _parse_simple(rows)


def _averages(totals, first, period_end):
    stats = {}
    for phone, total in totals.items():
        if total <= 0:
            continue
        months = _months_inclusive(first[phone], period_end)
        stats[phone] = {"avg": round(total / months, 2), "total": total}
    return stats


def _parse_operations_log(rows, i_name, i_amount, i_date, i_type):
    totals: dict[str, float] = {}
    first: dict[str, datetime] = {}
    used = skipped = bad = 0
    period_start = period_end = None

    for row in rows:
        if not row or len(row) <= max(i_name, i_amount, i_date):
            continue
        op_type = str(row[i_type] or "").strip().lower() if i_type is not None else "пополнение"
        if "возврат" in op_type:
            sign = -1
        elif "пополнение" in op_type:
            sign = 1
        else:
            skipped += 1          # другие типы операций на уровень не влияют
            continue
        phone = normalize_phone(row[i_name])
        amount = _to_float(row[i_amount])
        dt = _to_datetime(row[i_date])
        if len(phone) < 10 or amount is None or dt is None:
            bad += 1
            continue
        used += 1
        totals[phone] = totals.get(phone, 0.0) + sign * abs(amount)
        if sign > 0 and (phone not in first or dt < first[phone]):
            first[phone] = dt
        period_start = dt if period_start is None or dt < period_start else period_start
        period_end = dt if period_end is None or dt > period_end else period_end

    totals = {p: t for p, t in totals.items() if p in first}
    info = {"used": used, "bonus_rows": skipped, "bad": bad,
            "period_start": period_start, "period_end": period_end}
    return "operations_log", _averages(totals, first, period_end), info


def _parse_crm_log(rows, i_phone, i_money, i_date):
    totals: dict[str, float] = {}
    first: dict[str, datetime] = {}
    used = bonus_rows = bad = 0
    period_start = period_end = None

    for row in rows:
        if not row or len(row) <= max(i_phone, i_money, i_date):
            continue
        phone = normalize_phone(row[i_phone])
        money = _to_float(row[i_money])
        dt = _to_datetime(row[i_date])
        if len(phone) < 10 or money is None or dt is None:
            bad += 1
            continue
        if money <= 0:
            bonus_rows += 1   # строки с бонусами (рулетка и т.п.) — это не деньги
            continue
        used += 1
        totals[phone] = totals.get(phone, 0.0) + money
        if phone not in first or dt < first[phone]:
            first[phone] = dt
        period_start = dt if period_start is None or dt < period_start else period_start
        period_end = dt if period_end is None or dt > period_end else period_end

    stats = {}
    for phone, total in totals.items():
        months = _months_inclusive(first[phone], period_end)
        stats[phone] = {"avg": round(total / months, 2), "total": total}

    info = {"used": used, "bonus_rows": bonus_rows, "bad": bad,
            "period_start": period_start, "period_end": period_end}
    return "crm_log", stats, info


def _parse_simple(rows):
    stats = {}
    used = bad = 0
    for row in rows:
        if not row or len(row) < 2 or row[0] is None or row[1] is None:
            continue
        phone = normalize_phone(row[0])
        amount = _to_float(row[1])
        if len(phone) < 10 or amount is None:
            bad += 1          # сюда же попадает строка заголовка
            continue
        used += 1
        stats[phone] = {"avg": amount, "total": None}
    return "simple", stats, {"used": used, "bad": bad}

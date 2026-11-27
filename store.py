import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from random import Random


DEMO = Path(__file__).with_name("demo.sqlite3")
MSK = timezone(timedelta(hours=3))
SCHEMA = """CREATE TABLE daily (
    day TEXT NOT NULL, sku TEXT NOT NULL, sold INTEGER NOT NULL,
    revenue_rub INTEGER NOT NULL, purchased INTEGER NOT NULL,
    purchase_rub INTEGER NOT NULL, stock INTEGER NOT NULL,
    updated_at TEXT NOT NULL, PRIMARY KEY(day,sku)
)"""


def today():
    return datetime.now(MSK).date()


def seed():
    rng = Random(42)
    with closing(sqlite3.connect(DEMO)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(SCHEMA.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS "))
        columns = {r[1] for r in db.execute("PRAGMA table_info(daily)")}
        for name in ("orders", "ad_spend_rub", "returns", "fees_rub", "logistics_rub", "cogs_rub"):
            if name not in columns:
                db.execute(f"ALTER TABLE daily ADD COLUMN {name} INTEGER")
        # Only the generated fixture rolls forward. External databases never enter seed().
        yesterday = (today() - timedelta(days=1)).isoformat()
        if db.execute("SELECT MAX(day), COUNT(orders) = COUNT(*) FROM daily").fetchone() == (yesterday, 1):
            return
        db.execute("DELETE FROM daily")
        for sku, price, cost in [("Худи", 3490, 1450), ("Костюм", 5490, 2300), ("Футболка", 1790, 620)]:
            stock = 65
            for offset in range(35, 0, -1):
                day = today() - timedelta(days=offset)
                bought = rng.randint(30, 55) if offset % 7 == 0 else 0
                sold = min(stock + bought, rng.randint(3, 10))
                stock += bought - sold
                revenue = sold * price
                # Example ledger: paid sales net of refunds, advertising charged independently.
                returned = int(offset % 9 == 0)
                spend = 290 + (offset * 97 % 600) + (520 if sku == "Худи" and offset <= 7 else 0)
                db.execute("""INSERT INTO daily(day,sku,sold,revenue_rub,purchased,purchase_rub,stock,
                    updated_at,orders,ad_spend_rub,returns,fees_rub,logistics_rub,cogs_rub)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (day.isoformat(), sku, sold, revenue, bought, bought * cost, stock,
                     f"{day}T23:59:00+03:00", sold + offset % 4, spend, returned,
                     round(revenue * 0.18), sold * 95 + returned * 80, sold * cost))


class QuestionError(ValueError):
    pass


def retrieve(metric, start, end, sku):
    if metric not in ("sales", "purchases", "stock"):
        raise QuestionError("Могу проверить продажи, закупки или остатки. Какой показатель нужен?")
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (ValueError, TypeError):
        raise QuestionError("Уточните даты периода.") from None
    days = (last - first).days + 1
    if not 1 <= days <= 90 or last > today():
        raise QuestionError("Выберите период до 90 дней, заканчивающийся сегодня или раньше.")
    path = Path(os.getenv("SELLER_DB") or DEMO).resolve()
    fields = {"sales": "SUM(sold), SUM(revenue_rub)", "purchases": "SUM(purchased), SUM(purchase_rub)",
              "stock": "SUM(stock), NULL"}[metric]
    if metric == "stock":
        first, days = last, 1
    if sku is not None and not isinstance(sku, str):
        raise QuestionError("Уточните название артикула.")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.create_function("sku_key", 1, lambda value: value.casefold() if value else value)
        db.execute("BEGIN")
        rows = db.execute(f"""SELECT sku, {fields}, COUNT(*), MAX(updated_at) FROM daily
            WHERE day BETWEEN ? AND ? AND (? IS NULL OR sku_key(sku) = sku_key(?))
            GROUP BY sku ORDER BY 2 DESC, sku""", (str(first), str(last), sku, sku)).fetchall()
        known = {r[0] for r in db.execute("SELECT DISTINCT sku FROM daily")}
    if not rows:
        raise QuestionError("В источнике нет строк за этот период с таким фильтром.")
    partial = any(r[3] != days for r in rows) or (sku is None and {r[0] for r in rows} != known)
    total = sum(r[1] for r in rows)
    money = sum(r[2] for r in rows) if metric != "stock" else None
    title = {"sales": "Продажи", "purchases": "Закупки", "stock": "Остатки на конец дня"}[metric]
    def amounts(units, rub):
        return f"{units:,} шт." + (f" · {rub:,} ₽" if rub is not None else "")
    lines = [f"{title} · {first}" + (f" — {last}" if first != last else ""),
             ("По доступным строкам" if partial else "Всего") + " · " + amounts(total, money)]
    for name, units, rub, count, _ in rows[:10]:
        lines.append(f"{name} · {amounts(units, rub)}")
    if len(rows) > 10:
        lines.append("Показаны первые 10 SKU по количеству.")
    if partial:
        lines.append("Покрытие неполное. Пропущенные строки не считаются нулями.")
    lines += [f"Источник: {'демо · ' if path == DEMO.resolve() else ''}daily · {sum(r[3] for r in rows)} строк. Ключ строки: дата + SKU.",
              "Последнее обновление среди строк: " + max(r[4] for r in rows)]
    return "\n".join(lines)

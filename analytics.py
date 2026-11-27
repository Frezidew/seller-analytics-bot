import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from time import monotonic

import store
from store import QuestionError


REQUIRED_COLUMNS = (
    "day", "sku", "sold", "revenue_rub", "purchased", "purchase_rub", "stock", "updated_at",
)
OPTIONAL_COLUMNS = ("orders", "ad_spend_rub", "returns", "fees_rub", "logistics_rub", "cogs_rub")
FUNCTIONS = frozenset("""
    abs avg cast ceil ceiling coalesce count date datetime dense_rank floor glob group_concat
    ifnull iif instr julianday lag lead length like lower ltrim max min nullif printf
    rank replace round row_number rtrim strftime substr substring sum total trim typeof
    unixepoch upper
""".split())
MAX_ROWS = 60
MAX_RESULT_BYTES = 16_000
MAX_SQL_BYTES = 8_000


def stock_cover():
    """Coverage in days as of the latest snapshot, skipping incomplete histories."""
    result = run_query("""WITH latest AS (SELECT MAX(day) AS last FROM daily),
        period AS (SELECT day, sku, sold, stock, last FROM daily CROSS JOIN latest
                   WHERE day BETWEEN date(last,'-13 days') AND last)
        SELECT sku, last, COUNT(*) AS days, SUM(sold) AS units,
               MAX(CASE WHEN day=last THEN stock END) AS closing_stock,
               SUM(CASE WHEN stock=0 THEN 1 ELSE 0 END) AS zero_days
        FROM period GROUP BY sku,last ORDER BY
            CASE WHEN COUNT(*)=14 AND SUM(sold)>0 AND MAX(CASE WHEN day=last THEN stock END)
                IS NOT NULL THEN 0 ELSE 1 END,
            MAX(CASE WHEN day=last THEN stock END)*1.0/NULLIF(SUM(sold),0), sku""")
    if not result["rows"]:
        raise QuestionError("Нет данных для оценки запаса.")
    lines = [f"Запас на {result['day_max']} · по продажам за 14 календарных дней"]
    for sku, _, days, sold, stock, zero_days in result["rows"][:10]:
        if days != 14 or stock is None:
            details = f"неполный период ({days}/14 дней)"
        elif not sold:
            details = "продаж за период нет, срок не определён"
        else:
            details = f"{stock * 14 / sold:.1f} дн. · остаток {stock} шт. · продано {sold} шт."
        if zero_days:
            details += f" · дней без остатка: {zero_days}, спрос мог быть выше"
        lines.append(f"{sku} — {details}")
    if result["truncated"] or len(result["rows"]) > 10:
        lines.append("Показаны первые 10 строк рейтинга.")
    lines.append(f"Источник: {result['source']} · последнее обновление базы {result['updated_at']}")
    return "\n".join(lines)


def _open():
    path = Path(os.getenv("SELLER_DB") or store.DEMO).resolve()
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2, cached_statements=0)
    source = "демо · daily" if path == store.DEMO.resolve() else "daily"
    return db, source


def _prepare(db):
    db.execute("PRAGMA trusted_schema = OFF")
    if hasattr(db, "setlimit"):
        for category, limit in (
            (sqlite3.SQLITE_LIMIT_LENGTH, 65_536),
            (sqlite3.SQLITE_LIMIT_COLUMN, 64),
            (sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 40),
            (sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 12),
        ):
            db.setlimit(category, limit)
    deadline = monotonic() + 0.8
    work = 0

    def progress():
        nonlocal work
        work += 1_000
        return work >= 2_000_000 or monotonic() >= deadline

    db.set_progress_handler(progress, 1_000)
    db.execute("BEGIN")
    schema = {row[1].lower() for row in db.execute("PRAGMA main.table_info('daily')")}
    if not {"day", "sku", "sold", "updated_at"}.issubset(schema):
        raise QuestionError("В источнике daily отсутствуют обязательные поля.")
    return [col for col in REQUIRED_COLUMNS + OPTIONAL_COLUMNS if col in schema]


def _bounds(db, source):
    day_min, day_max, updated_at = db.execute(
        "SELECT MIN(day), MAX(day), MAX(updated_at) FROM main.daily"
    ).fetchone()
    return {"source": source, "day_min": day_min, "day_max": day_max, "updated_at": updated_at}


def catalog():
    try:
        db, source = _open()
        with closing(db):
            columns = _prepare(db)
            summary = _bounds(db, source)
            examples = db.execute("SELECT DISTINCT sku FROM main.daily LIMIT 12")
            return {**summary, "columns": columns, "sku_examples": [row[0][:120] for row in examples]}
    except sqlite3.Error:
        raise QuestionError("Не удалось прочитать описание источника daily.") from None


def _execute(db, sql, allowed_columns, database_name):
    reads_daily = False
    readable_columns = {*allowed_columns, ""}

    def authorizer(action, name, detail, database, context):
        nonlocal reads_daily
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and detail in FUNCTIONS:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            if name.lower() == "daily" and database in (None, database_name) and detail.lower() in readable_columns:
                reads_daily = True
                return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    db.set_authorizer(authorizer)
    result = db.execute(sql)
    if not reads_daily:
        raise QuestionError("Запрос должен использовать источник daily.")
    columns = [field[0] for field in result.description]
    size = len(json.dumps(columns, ensure_ascii=False).encode("utf-8"))
    if size > 2_000:
        raise QuestionError("Слишком длинные названия столбцов результата.")
    rows = []
    for row in result:
        if len(rows) == MAX_ROWS:
            return columns, rows, True
        try:
            encoded = json.dumps(row, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            raise QuestionError("Запрос вернул данные в неподдерживаемом формате.") from None
        size += len(encoded) + 2
        if size > MAX_RESULT_BYTES:
            return columns, rows, True
        rows.append(list(row))
    return columns, rows, False


def run_query(sql):
    if (
        not isinstance(sql, str)
        or len(sql) > MAX_SQL_BYTES
        or len(sql.encode("utf-8", errors="surrogatepass")) > MAX_SQL_BYTES
        or not re.match(r"\s*(SELECT|WITH)\b", sql, re.IGNORECASE)
    ):
        raise QuestionError("Ожидается один запрос SELECT к daily.")
    try:
        db, source = _open()
        with closing(db):
            allowed_columns = _prepare(db)
            summary = _bounds(db, source)
            rootpage, = db.execute(
                "SELECT rootpage FROM main.sqlite_schema WHERE name = 'daily' COLLATE NOCASE"
            ).fetchone()
            database_name = "main"
            if rootpage == 0:
                # Keep view dependencies outside the scope of model-written queries.
                db.execute("PRAGMA temp_store = MEMORY")
                projection = ", ".join(allowed_columns)
                db.execute(f"CREATE TEMP TABLE daily AS SELECT {projection} FROM main.daily")
                database_name = "temp"
            db.execute("PRAGMA query_only = ON")
            columns, rows, truncated = _execute(db, sql, allowed_columns, database_name)
            return {**summary, "columns": columns, "rows": rows, "truncated": truncated}
    except (sqlite3.Error, UnicodeError):
        raise QuestionError("Запрос к daily отклонён или превысил лимит вычислений.") from None

import os
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

from store import DEMO, SCHEMA


REQUIRED = ("day", "sku", "sold", "revenue_rub", "purchased", "purchase_rub", "stock", "updated_at")
OPTIONAL = ("orders", "ad_spend_rub", "returns", "fees_rub", "logistics_rub", "cogs_rub")
META = "seller_sheets_snapshot"
SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


class SyncError(RuntimeError):
    pass


def _configuration():
    sheet = os.getenv("SELLER_SHEET_ID", "").strip()
    if not sheet:
        return None
    db = os.getenv("SELLER_DB", "").strip()
    if not db or Path(db).resolve() == DEMO.resolve():
        raise SyncError("Для Google Sheets укажите отдельный SELLER_DB.")
    try:
        max_age = int(os.getenv("SELLER_SHEET_MAX_AGE_SECONDS", "300"))
        if max_age <= 0:
            raise ValueError
    except ValueError:
        raise SyncError("SELLER_SHEET_MAX_AGE_SECONDS должен быть положительным целым числом.") from None
    return Path(db).resolve(), sheet, os.getenv("SELLER_SHEET_RANGE", "daily!A:Z"), max_age


def _fetch(sheet, cell_range):
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
    except ImportError:
        raise SyncError("Установите зависимости Google Sheets: google-auth и requests.") from None
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{quote(sheet, safe='')}/values/{quote(cell_range, safe='')}"
    try:
        credentials, _ = google.auth.default(scopes=[SCOPE])
        with AuthorizedSession(credentials, refresh_timeout=15) as session:
            response = session.get(url, params={"valueRenderOption": "UNFORMATTED_VALUE"}, timeout=20)
        if response.status_code != 200:
            raise SyncError(f"Google Sheets вернул HTTP {response.status_code}.")
        return response.json().get("values", [])
    except SyncError:
        raise
    except Exception:
        raise SyncError("Не удалось прочитать Google Sheets. Проверьте ADC, доступ к таблице и сеть.") from None


def _integer(value):
    if type(value) is int:
        number = value
    elif type(value) is float and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        number = int(value.strip())
    else:
        raise ValueError
    if not 0 <= number < 2**63:
        raise ValueError
    return number


def _day(value):
    if isinstance(value, str):
        day = date.fromisoformat(value.strip())
    else:
        day = date(1899, 12, 30) + timedelta(days=_integer(value))
    return day.isoformat()


def _updated_at(value):
    if not isinstance(value, str):
        raise ValueError
    stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError
    return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _sku(value):
    if type(value) in (int, float):
        value = str(_integer(value))
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError
    return value.strip()


def _parse(values):
    if not values:
        raise SyncError("Диапазон Google Sheets пуст.")
    headers = [str(value).strip() for value in values[0]]
    named = [name for name in headers if name]
    if len(set(named)) != len(named):
        raise SyncError("В Google Sheets повторяются имена столбцов.")
    missing = [name for name in REQUIRED if name not in headers]
    if missing:
        raise SyncError("В Google Sheets отсутствуют столбцы " + ", ".join(missing) + ".")
    extra = tuple(name for name in OPTIONAL if name in headers)
    columns = REQUIRED + extra
    positions = [headers.index(name) for name in columns]
    parsers = {"day": _day, "sku": _sku, "updated_at": _updated_at}
    rows, keys = [], set()
    for index, source in enumerate(values[1:], 2):
        if all(value is None or value == "" for value in source):
            continue
        row = []
        for name, position in zip(columns, positions):
            value = source[position] if position < len(source) else None
            try:
                row.append(None if name in extra and value in (None, "") else parsers.get(name, _integer)(value))
            except (ValueError, OverflowError):
                raise SyncError(f"Строка {index} диапазона Google Sheets, недопустимое поле {name}.") from None
        key = (row[0], row[1].casefold())
        if key in keys:
            raise SyncError(f"Строка {index} диапазона Google Sheets повторяет пару дата и SKU.")
        keys.add(key)
        rows.append(tuple(row))
    if not rows:
        raise SyncError("Google Sheets не содержит строк данных.")
    return extra, rows


def _last_sync(db, sheet, cell_range):
    try:
        rows = db.execute(f"SELECT sheet, cell_range, synced_at FROM {META}").fetchall()
        if len(rows) == 1 and rows[0][:2] == (sheet, cell_range) and isinstance(rows[0][2], (float, int)):
            return rows[0][2]
    except sqlite3.Error:
        pass
    raise SyncError("SELLER_DB уже существует и не является снимком этого диапазона Google Sheets.")


def _write_snapshot(path, sheet, cell_range, started, extra, rows, *, new):
    target = path if new else path.as_uri() + "?mode=rw"
    with closing(sqlite3.connect(target, uri=not new)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if new:
            db.execute(f"CREATE TABLE {META} (sheet TEXT NOT NULL, cell_range TEXT NOT NULL, synced_at REAL NOT NULL)")
        else:
            if _last_sync(db, sheet, cell_range) >= started:
                return
            db.execute("DROP TABLE daily")
        db.execute(SCHEMA)
        for name in extra:
            db.execute(f"ALTER TABLE daily ADD COLUMN {name} INTEGER")
        placeholders = ",".join("?" for _ in range(len(REQUIRED) + len(extra)))
        db.executemany(f"INSERT INTO daily VALUES ({placeholders})", rows)
        db.execute(f"DELETE FROM {META}")
        db.execute(f"INSERT INTO {META} VALUES (?, ?, ?)", (sheet, cell_range, started))


def ensure_fresh(*, force=False):
    config = _configuration()
    if config is None:
        if force:
            raise SyncError("Укажите SELLER_SHEET_ID и отдельный SELLER_DB.")
        return
    path, sheet, cell_range, max_age = config
    started = time.time()
    try:
        if path.exists():
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                synced_at = _last_sync(db, sheet, cell_range)
            if not force and 0 <= started - synced_at < max_age:
                return
        extra, rows = _parse(_fetch(sheet, cell_range))
        if not path.exists():
            with TemporaryDirectory(dir=path.parent) as directory:
                temporary = Path(directory) / "snapshot.sqlite3"
                _write_snapshot(temporary, sheet, cell_range, started, extra, rows, new=True)
                try:
                    os.link(temporary, path)
                    return
                except FileExistsError:
                    pass
        _write_snapshot(path, sheet, cell_range, started, extra, rows, new=False)
    except (OSError, sqlite3.Error):
        raise SyncError("Не удалось обновить SQLite-снимок Google Sheets.") from None


if __name__ == "__main__":
    try:
        ensure_fresh(force=True)
    except SyncError as error:
        raise SystemExit(str(error)) from None
    print("Снимок Google Sheets обновлён.")

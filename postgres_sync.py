"""Create a local snapshot of a PostgreSQL reporting view."""
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from analytics import OPTIONAL_COLUMNS, REQUIRED_COLUMNS


def sync():
    import psycopg
    from psycopg import sql

    path = Path(os.environ["SELLER_DB"]).resolve()
    if path.name == "demo.sqlite3":
        raise ValueError("Выберите отдельный файл снимка.")
    schema = os.getenv("SELLER_PG_SCHEMA", "public")
    view = os.getenv("SELLER_PG_VIEW", "daily")
    with psycopg.connect(os.environ["SELLER_PG_DSN"], connect_timeout=10) as source:
        source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        source.execute("SET LOCAL statement_timeout='30s'")
        name = sql.Identifier(schema, view)
        info = source.execute(sql.SQL("SELECT * FROM {} LIMIT 0").format(name)).description
        present = {field.name for field in info}
        if not {"day","sku","sold","updated_at"} <= present:
            raise ValueError("Представление должно содержать day, sku, sold и updated_at.")
        columns = [c for c in REQUIRED_COLUMNS + OPTIONAL_COLUMNS if c in present]
        select = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(map(sql.Identifier,columns)), name)
        identity = (schema, view)
        if path.exists():
            with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)) as old:
                if old.execute("SELECT schema_name, view_name FROM pg_snapshot").fetchall() != [identity]:
                    raise ValueError("Выберите новый путь для снимка.")
        with TemporaryDirectory(dir=path.parent) as folder:
            temp = Path(folder)/"daily.sqlite3"
            with closing(sqlite3.connect(temp)) as db, db, source.cursor(name="daily_export") as records:
                fields = [c + (" TEXT" if c in ("sku","day","updated_at") else " INTEGER") for c in columns]
                db.execute("CREATE TABLE daily("+",".join(fields)+", PRIMARY KEY(day,sku))")
                db.execute("CREATE TABLE pg_snapshot(schema_name TEXT,view_name TEXT)")
                db.execute("INSERT INTO pg_snapshot VALUES (?,?)",identity)
                records.execute(select)
                count = 0
                for batch in iter(lambda:records.fetchmany(1000),[]):
                    values = []
                    for row in batch:
                        item = []
                        for column,value in zip(columns,row):
                            if value is None:
                                if column in ("day","sku","sold","updated_at"):
                                    raise ValueError("Пустое обязательное поле " + column)
                                item.append(None)
                            elif column in ("day","sku","updated_at"):
                                item.append(str(value))
                            elif int(value) == value:
                                item.append(int(value))
                            else:
                                raise ValueError("Ожидалось целое число в " + column)
                        values.append(item)
                    db.executemany("INSERT INTO daily VALUES ("+",".join("?" for _ in columns)+")",values)
                    count += len(values)
                if not count:
                    raise ValueError("Исходное представление пусто.")
            temp.replace(path)
    return count


if __name__ == "__main__":
    print("Обновлено строк:",sync())

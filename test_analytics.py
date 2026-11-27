import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import analytics
from store import QuestionError


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "store.sqlite3"
        environment = patch.dict("os.environ", SELLER_DB=str(self.path))
        environment.start()
        self.addCleanup(environment.stop)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE daily (
                day TEXT NOT NULL, sku TEXT NOT NULL, sold INTEGER NOT NULL,
                revenue_rub INTEGER NOT NULL, purchased INTEGER NOT NULL,
                purchase_rub INTEGER NOT NULL, stock INTEGER NOT NULL,
                updated_at TEXT NOT NULL, ad_spend_rub INTEGER DEFAULT 30,
                private_note TEXT DEFAULT 'private', PRIMARY KEY(day, sku)
            )""")
            db.execute("CREATE TABLE other (value TEXT)")
            db.execute("INSERT INTO other VALUES ('private')")
            db.executemany(
                """INSERT INTO daily (day, sku, sold, revenue_rub, purchased, purchase_rub,
                   stock, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    ("2026-01-01", "A", 2, 400, 1, 90, 6, "2026-01-01T23:59:00+03:00"),
                    ("2026-01-02", "A", 3, 600, 0, 0, 3, "2026-01-02T23:59:00+03:00"),
                ],
            )

    def test_catalog_and_analytics(self):
        catalog = analytics.catalog()
        self.assertEqual(catalog["columns"], [*analytics.REQUIRED_COLUMNS, "ad_spend_rub"])
        self.assertEqual(catalog["sku_examples"], ["A"])
        self.assertEqual(catalog["day_min"], "2026-01-01")
        result = analytics.run_query("""
            WITH ranked AS (
                SELECT sku, stock, sold, ad_spend_rub, ROW_NUMBER() OVER
                    (PARTITION BY sku ORDER BY day DESC) AS position
                FROM daily
            )
            SELECT sku, MAX(CASE WHEN position = 1 THEN stock END) AS latest_stock,
                   SUM(sold) AS units, SUM(ad_spend_rub) AS ad_spend
            FROM ranked GROUP BY sku
        """)
        self.assertEqual(result["rows"], [["A", 3, 5, 60]])
        self.assertEqual(result["source"], "daily")
        self.assertEqual(result["updated_at"], "2026-01-02T23:59:00+03:00")
        self.assertFalse(result["truncated"])

    def test_rejects_queries_outside_daily_and_excess_work(self):
        queries = (
            "SELECT 1",
            "SELECT * FROM sqlite_schema",
            "SELECT private_note FROM daily",
            "SELECT value FROM other, daily",
            "WITH daily AS (SELECT value FROM other) SELECT value FROM daily, main.daily",
            "SELECT randomblob(5000000) FROM daily",
            "SELECT 1 FROM daily; SELECT 2 FROM daily",
            "WITH x AS (SELECT sku FROM daily) DELETE FROM daily",
            "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM x) SELECT n FROM x, daily",
        )
        for query in queries:
            with self.subTest(query=query), self.assertRaises(QuestionError):
                analytics.run_query(query)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                """INSERT INTO daily (day, sku, sold, revenue_rub, purchased, purchase_rub, stock, updated_at)
                SELECT day, sku || '%', sold, revenue_rub, purchased, purchase_rub, stock, updated_at
                FROM daily WHERE sku = 'A'"""
            )
        with self.assertRaises(QuestionError):
            analytics.run_query("SELECT SUM(a.sold) FROM " + ", ".join(
                f"daily AS {alias}" for alias in "abcdefghijkl"
            ))

    def test_bounds_output(self):
        with patch.object(analytics, "MAX_ROWS", 1):
            result = analytics.run_query("SELECT sku FROM daily ORDER BY day")
        self.assertEqual(result["rows"], [["A"]])
        self.assertTrue(result["truncated"])
        with patch.object(analytics, "MAX_RESULT_BYTES", 20):
            result = analytics.run_query("SELECT printf('%50s', sku) AS label FROM daily")
        self.assertEqual(result["rows"], [])
        self.assertTrue(result["truncated"])

    def test_daily_view_and_legacy_columns(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("ALTER TABLE daily RENAME TO raw")
            db.execute("CREATE VIEW daily AS SELECT " + ", ".join(analytics.REQUIRED_COLUMNS) + " FROM raw")
        self.assertEqual(analytics.catalog()["columns"], list(analytics.REQUIRED_COLUMNS))
        self.assertEqual(analytics.run_query("SELECT COUNT(*) FROM daily")["rows"], [[2]])
        with self.assertRaises(QuestionError):
            analytics.run_query("WITH daily AS (SELECT value FROM other) SELECT value FROM daily, temp.daily")
        with self.assertRaises(QuestionError):
            analytics.run_query("SELECT raw.private_note FROM raw, daily")


if __name__ == "__main__":
    unittest.main()

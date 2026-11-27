import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import sheets_sync


HEADERS = list(sheets_sync.REQUIRED)
ROW = ["2026-11-25", "001", 2, 200, 1, 40, 8, "2026-11-25T23:59:00+03:00"]


class SheetsSyncTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "data.sqlite3"
        environment = patch.dict("os.environ", {"SELLER_DB": str(self.path), "SELLER_SHEET_ID": "sheet",
                                                "SELLER_SHEET_RANGE": "daily!A:Z", "SELLER_SHEET_MAX_AGE_SECONDS": "300"})
        environment.start()
        self.addCleanup(environment.stop)
        fetch = patch("sheets_sync._fetch", return_value=[HEADERS, ROW])
        self.fetch = fetch.start()
        self.addCleanup(fetch.stop)

    def read(self, query):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(query).fetchall()

    def test_fresh_snapshot_reuses_cached_data_and_force_refreshes(self):
        sheets_sync.ensure_fresh()
        sheets_sync.ensure_fresh()
        self.fetch.assert_called_once_with("sheet", "daily!A:Z")
        self.assertEqual(self.read("SELECT sku, revenue_rub, updated_at FROM daily"),
                         [("001", 200, "2026-11-25T20:59:00.000000+00:00")])
        self.fetch.return_value = [HEADERS + ["orders", "cogs_rub"], ROW + [3]]
        sheets_sync.ensure_fresh(force=True)
        self.assertEqual(self.read("SELECT orders, cogs_rub FROM daily"), [(3, None)])
        self.assertEqual(self.read("SELECT name FROM pragma_table_info('daily') WHERE name = 'returns'"), [])

    def test_failed_refresh_preserves_existing_snapshot_and_reports_failure(self):
        sheets_sync.ensure_fresh()
        self.fetch.return_value = [HEADERS, ROW, ROW]
        with self.assertRaisesRegex(sheets_sync.SyncError, "повторяет"):
            sheets_sync.ensure_fresh(force=True)
        self.assertEqual(self.read("SELECT sold FROM daily"), [(2,)])

    def test_expired_snapshot_refreshes_and_reports_fetch_error(self):
        with patch("sheets_sync.time.time", return_value=1000):
            sheets_sync.ensure_fresh()
        self.fetch.side_effect = sheets_sync.SyncError("HTTP 403")
        with patch("sheets_sync.time.time", return_value=1300):
            with self.assertRaisesRegex(sheets_sync.SyncError, "403"):
                sheets_sync.ensure_fresh()
        self.assertEqual(self.read("SELECT sold FROM daily"), [(2,)])

    def test_unrelated_database_is_not_overwritten(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE daily (sku TEXT)")
            db.execute("INSERT INTO daily VALUES ('keep')")
        with self.assertRaisesRegex(sheets_sync.SyncError, "не является снимком"):
            sheets_sync.ensure_fresh(force=True)
        self.fetch.assert_not_called()
        self.assertEqual(self.read("SELECT sku FROM daily"), [("keep",)])


if __name__ == "__main__":
    unittest.main()

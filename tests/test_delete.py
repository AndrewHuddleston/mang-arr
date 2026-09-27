"""Deleting a series with its library files asks Komga to scan, the same call as after an import, so the series
leaves Komga at once instead of at Komga's next scheduled scan. Komga is faked: nothing reaches a real one."""
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from mangarr import core, db, komga, settings
from mangarr.model import Series


class DeleteScanTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.library = os.path.join(tmp.name, "library")
        for p in (mock.patch("mangarr.config.DB_PATH", os.path.join(tmp.name, "t.db")),
                  mock.patch("mangarr.config.LIBRARY_ROOT", self.library),
                  mock.patch("mangarr.config.STAGING_ROOT", os.path.join(tmp.name, "staging")),
                  mock.patch.object(komga, "_call", self.fake_call),
                  mock.patch("mangarr.notify.send", return_value=None),
                  mock.patch("mangarr.notify.send_detailed", return_value={})):
            p.start()
            self.addCleanup(p.stop)
        settings._cache.clear()
        self.addCleanup(settings._cache.clear)
        self.calls = []
        with db.connect() as con:
            self.sid = db.upsert_series(con, Series(anilist_id=118601, english="It's Mine"))
            self.file = os.path.join(self.library, db.get_series(con, self.sid)["folder"], "Chapter 001.0.cbz")
            os.makedirs(os.path.dirname(self.file))
            open(self.file, "w").close()
            db.set_have(con, self.sid, 1.0, None, self.file)
            con.commit()

    def fake_call(self, method, path, timeout=20):
        with db.connect() as con:                    # the delete is committed before Komga is asked
            gone = db.get_series(con, self.sid) is None
        self.calls.append((method, path, gone))
        return 200, None

    def komga_on(self):
        with db.connect() as con:
            settings.set_many(con, {"komga_url": "http://komga:25600", "komga_api_key": "k",
                                    "komga_library_id": "LIB1"})

    def delete(self, files: bool):
        with db.connect() as con:
            core.delete_series(con, mock.Mock(), self.sid, delete_library=files)

    def test_scan_once_the_files_are_gone(self):
        self.komga_on()
        with self.assertLogs("mangarr.komga", "INFO") as logs:
            self.delete(True)
        self.assertFalse(os.path.exists(os.path.dirname(self.file)))
        self.assertEqual(self.calls, [("POST", "/api/v1/libraries/LIB1/scan", True)])
        self.assertIn("komga: scan requested for 1 library", logs.output[-1])

    def test_no_scan_when_the_files_stay(self):
        self.komga_on()
        self.delete(False)
        self.assertTrue(os.path.exists(self.file))
        self.assertEqual(self.calls, [])

    def test_no_scan_when_nothing_was_in_the_library(self):
        self.komga_on()
        os.remove(self.file)
        os.rmdir(os.path.dirname(self.file))
        self.delete(True)
        self.assertEqual(self.calls, [])

    def test_skipped_when_komga_is_not_configured(self):
        self.delete(True)
        self.assertFalse(os.path.exists(self.file))
        self.assertEqual(self.calls, [])

    def test_komga_failing_does_not_fail_the_delete(self):
        self.komga_on()

        def refused(method, path, timeout=20):
            raise urllib.error.HTTPError("http://komga:25600" + path, 401, "Unauthorized", {}, None)
        with mock.patch.object(komga, "_call", refused), self.assertLogs("mangarr.komga", "ERROR") as logs:
            self.delete(True)
        self.assertIn("komga scan failed: HTTP 401 Unauthorized (check the API key and URL)", logs.output[0])
        self.assertFalse(os.path.exists(self.file))
        with db.connect() as con:
            self.assertIsNone(db.get_series(con, self.sid))


if __name__ == "__main__":
    unittest.main()

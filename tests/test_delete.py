"""Deleting a series with its library files asks Komga to scan, the same call as after an import, so the series
leaves Komga at once instead of at Komga's next scheduled scan. Komga is faked: nothing reaches a real one."""
import os
import tempfile
import threading
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
        self.join_scan()

    @staticmethod
    def join_scan():
        """The scan request runs on a thread of its own; wait for it while Komga is still faked."""
        for t in threading.enumerate():
            if t.name == "mangarr-komga-scan":
                t.join(10)

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

    def test_delete_does_not_wait_for_komga(self):
        """Review: the scan ran inside the delete request, so a Komga that takes the connection and never answers
        held the delete (a web request) for the whole 20 s timeout. The delete now returns first."""
        self.komga_on()
        answer = threading.Event()

        def slow(method, path, timeout=20):
            answer.wait(10)                          # a Komga that has not answered yet
            return self.fake_call(method, path, timeout)
        with mock.patch.object(komga, "_call", slow), self.assertLogs("mangarr.komga", "INFO") as logs:
            with db.connect() as con:
                core.delete_series(con, mock.Mock(), self.sid, delete_library=True)
                self.assertIsNone(db.get_series(con, self.sid))
            self.assertEqual(self.calls, [])         # the delete returned while Komga still had not answered
            answer.set()
            self.join_scan()
        self.assertEqual(self.calls, [("POST", "/api/v1/libraries/LIB1/scan", True)])
        self.assertIn("komga: scan requested for 1 library", logs.output[-1])

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


class FakeSuwayomi:
    """Records what the delete asks Suwayomi; downloaded = {manga_id: [chapter ids]}."""
    def __init__(self, downloaded: dict, broken: bool = False):
        self.downloaded, self.broken, self.calls = downloaded, broken, []

    def downloaded_chapter_ids(self, manga_id, timeout=60):
        if self.broken:
            from mangarr.suwayomi import SuwayomiError
            raise SuwayomiError("Suwayomi down")
        return list(self.downloaded.get(manga_id, []))

    def delete_downloads(self, ids, timeout=120):
        self.calls.append(("delete_downloads", list(ids)))

    def set_in_library(self, manga_id, v, retries=3, timeout=60):
        self.calls.append(("set_in_library", manga_id, v))


class DeleteDownloadsTest(DeleteScanTest):
    """Delete with files also deletes, through Suwayomi, the chapter files it downloaded for the series' entries:
    that is where the bytes are (library files are hard links), and mang-arr mounts Suwayomi's folder read-only."""
    def add_source(self, series_id, manga_id, name):
        with db.connect() as con:
            con.execute("INSERT INTO series_source (series_id, manga_id, source_name, title, author, match_level,"
                        " author_level, chapter_count, max_chapter, is_primary, seen_at)"
                        " VALUES (?,?,?,?,?,1,1,10,10,1,'2026-10-05 00:00:00')", (series_id, manga_id, name, "It's Mine", None))
            con.commit()

    def test_downloads_deleted_with_the_files(self):
        self.add_source(self.sid, 501, "Weeb Central (EN)")
        client = FakeSuwayomi({501: [9001, 9002]})
        with db.connect() as con, self.assertLogs("mangarr.core", "INFO") as logs:
            core.delete_series(con, client, self.sid, delete_library=True)
        self.assertEqual(client.calls, [("delete_downloads", [9001, 9002]), ("set_in_library", 501, False)])
        self.assertTrue(any("2 downloaded chapter file(s) of the Weeb Central (EN) entry deleted" in m for m in logs.output))
        with db.connect() as con:
            self.assertIn("with its library files and downloads", db.events(con, 5)[0]["message"])

    def test_downloads_kept_without_the_files_option(self):
        self.add_source(self.sid, 501, "Weeb Central (EN)")
        client = FakeSuwayomi({501: [9001]})
        with db.connect() as con:
            core.delete_series(con, client, self.sid, delete_library=False)
        self.assertEqual(client.calls, [("set_in_library", 501, False)])
        self.assertTrue(os.path.exists(self.file))

    def test_entry_shared_with_another_series_keeps_its_downloads(self):
        with db.connect() as con:
            other = db.upsert_series(con, Series(anilist_id=118602, english="It's Mine Too"))
            con.commit()
        self.add_source(self.sid, 501, "Weeb Central (EN)")
        self.add_source(other, 501, "Weeb Central (EN)")
        client = FakeSuwayomi({501: [9001]})
        with db.connect() as con:
            core.delete_series(con, client, self.sid, delete_library=True)
        self.assertEqual(client.calls, [])
        self.assertFalse(os.path.exists(self.file))     # the library links still go

    def test_suwayomi_down_does_not_block_the_delete(self):
        self.add_source(self.sid, 501, "Weeb Central (EN)")
        client = FakeSuwayomi({501: [9001]}, broken=True)
        with db.connect() as con, self.assertLogs("mangarr.core", "WARNING") as logs:
            core.delete_series(con, client, self.sid, delete_library=True)
        self.assertIn("were not deleted: Suwayomi down (delete them in Suwayomi)", logs.output[0])
        self.assertFalse(os.path.exists(self.file))
        with db.connect() as con:
            self.assertIsNone(db.get_series(con, self.sid))


if __name__ == "__main__":
    unittest.main()

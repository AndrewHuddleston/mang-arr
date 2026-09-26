"""Backups: create, prune, verify, restore in place."""
import os
import tempfile
import unittest
from unittest import mock

from mangarr import backup, db
from mangarr.model import Series


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch("mangarr.config.DATA_DIR", self.tmp.name),
                        mock.patch("mangarr.config.DB_PATH", os.path.join(self.tmp.name, "live.db"))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_create_verify_restore(self):
        with db.connect() as con:
            db.upsert_series(con, Series(anilist_id=1, english="Kept"))
        p = backup.create("test")
        self.assertTrue(os.path.exists(p))
        ok, msg = backup.verify(p)
        self.assertTrue(ok, msg)
        self.assertIn("1 series", msg)
        with db.connect() as con:
            db.upsert_series(con, Series(anilist_id=2, english="Added later"))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM series").fetchone()[0], 2)
        msg = backup.restore(p)
        self.assertIn("restored", msg)
        with db.connect() as con:
            titles = [r["title"] for r in con.execute("SELECT title FROM series")]
        self.assertEqual(titles, ["Kept"])
        self.assertEqual(len(backup.listing()), 2)     # the backup + the safety copy taken before restore

    def test_verify_rejects_garbage(self):
        p = os.path.join(self.tmp.name, "junk.db")
        with open(p, "wb") as f:
            f.write(b"not a database")
        ok, msg = backup.verify(p)
        self.assertFalse(ok)

    def test_prune_and_names(self):
        with db.connect():
            pass
        for _ in range(3):
            backup.create("t")
            os.utime(backup.path_of(backup.listing()[0]["name"]), None)
        self.assertEqual(backup.prune(keep=1), 2)
        self.assertEqual(len(backup.listing()), 1)
        with self.assertRaises(ValueError):
            backup.path_of("../etc/passwd")


if __name__ == "__main__":
    unittest.main()

"""Backups: create, prune, verify, restore in place - and the ways a restore
must not hurt: losing the backup being restored, breaking the live database,
reviving old credentials, or bringing in paths, triggers and tables that do
not belong."""
import asyncio
import io
import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from mangarr import backup, core, db, downloader, library, settings
from mangarr.model import Series

try:
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError):      # web extras or httpx not installed
    TestClient = None


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.library = os.path.join(t, "library")
        self.staging = os.path.join(t, "staging")
        os.makedirs(self.library)
        os.makedirs(self.staging)
        self.patches = [mock.patch("mangarr.config.DATA_DIR", t),
                        mock.patch("mangarr.config.DB_PATH", os.path.join(t, "live.db")),
                        mock.patch("mangarr.config.LOCK_PATH", os.path.join(t, "download.lock")),
                        mock.patch("mangarr.config.LIBRARY_ROOT", self.library),
                        mock.patch("mangarr.config.STAGING_ROOT", self.staging)]
        for p in self.patches:
            p.start()
        settings._cache.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        settings._cache.clear()
        self.tmp.cleanup()

    def titles(self):
        with db.connect() as con:
            return sorted(r["title"] for r in con.execute("SELECT title FROM series"))

    def backups_dir_extras(self):
        """Anything in the backups folder that is not a kept backup."""
        return [n for n in os.listdir(backup.backup_dir()) if not backup.NAME_RE.match(n)]

    def crafted(self, name="crafted.db", version=None, sql=""):
        """A database with the current schema (or `version`), plus extra SQL."""
        p = os.path.join(self.tmp.name, name)
        with db.connect(p) as con:
            pass
        con = sqlite3.connect(p)
        if version is not None:
            con.execute(f"PRAGMA user_version = {version}")
        if sql:
            con.executescript(sql)
        con.commit()
        con.close()
        return p


class BackupTest(_Base):
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
        self.assertEqual(self.titles(), ["Kept"])
        self.assertEqual(len(backup.listing()), 2)     # the backup + the safety copy taken before restore
        self.assertEqual(self.backups_dir_extras(), [])   # no work files, no -wal/-shm left behind

    def test_verify_rejects_garbage(self):
        p = os.path.join(self.tmp.name, "junk.db")
        with open(p, "wb") as f:
            f.write(b"not a database")
        ok, msg = backup.verify(p)
        self.assertFalse(ok)
        with self.assertRaises(backup.RestoreError):
            backup.restore(p)

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

    def test_listing_orders_same_second_counters_numerically(self):
        d = backup.backup_dir()
        for n in ("mangarr-20260101-000000.db", "mangarr-20260101-000000-2.db", "mangarr-20260101-000000-10.db",
                  "mangarr-20251231-235959.db"):
            open(os.path.join(d, n), "wb").close()
        self.assertEqual([b["name"] for b in backup.listing()],
                         ["mangarr-20260101-000000-10.db", "mangarr-20260101-000000-2.db",
                          "mangarr-20260101-000000.db", "mangarr-20251231-235959.db"])

    # -- #5: restoring the oldest backup must not delete it before it is read

    def test_restoring_the_oldest_backup_when_keep_is_full(self):
        with db.connect() as con:
            db.upsert_series(con, Series(anilist_id=1, english="Oldest state"))
        with mock.patch.object(backup, "KEEP", 3):
            oldest = backup.create("t")
            with db.connect() as con:
                db.upsert_series(con, Series(anilist_id=2, english="Newer"))
            backup.create("t")
            backup.create("t")
            self.assertEqual(backup.listing()[-1]["name"], os.path.basename(oldest))
            msg = backup.restore(oldest)                    # the safety copy would push it out of KEEP
            self.assertIn("restored", msg)
            self.assertEqual(self.titles(), ["Oldest state"])
            names = [b["name"] for b in backup.listing()]
            self.assertEqual(names[0], msg.rsplit(" ", 1)[1])   # the safety copy is kept ...
            self.assertEqual(names[-1], os.path.basename(oldest))   # ... and so is the backup just restored
            self.assertEqual(len(names), 4)                     # pruned after the restore, to KEEP + that one
            backup.create("t")
            self.assertEqual(len(backup.listing()), 3)          # the next backup rotates it out

    # -- #81: a failed backup leaves nothing that looks like one

    def test_failed_backup_leaves_no_file(self):
        with db.connect():
            pass
        good = backup.create("t")

        def broken(dst):
            with open(dst, "wb") as f:
                f.write(b"SQLite format 3\x00" + b"\x00" * 100)      # truncated copy
            raise sqlite3.OperationalError("disk I/O error")
        with mock.patch.object(backup, "_snapshot", broken), self.assertRaises(sqlite3.OperationalError):
            backup.create("t")
        self.assertEqual([b["name"] for b in backup.listing()], [os.path.basename(good)])
        self.assertEqual(self.backups_dir_extras(), [])

    def test_stale_temp_files_are_cleaned(self):
        d = backup.backup_dir()
        old = os.path.join(d, ".tmp-crashed.db")
        open(old, "wb").close()
        os.utime(old, (time.time() - 3 * 86400,) * 2)
        backup._clean_stale_temp()
        self.assertFalse(os.path.exists(old))

    # -- #86: the database and backups hold secrets: owner-only

    def test_database_and_backups_are_private(self):
        with db.connect() as con:
            settings.set_many(con, {"auth_user": "a", "auth_password": "secret"})
        p = backup.create("t")
        mode = lambda f: stat.S_IMODE(os.stat(f).st_mode)      # noqa: E731
        self.assertEqual(mode(p), 0o600)
        self.assertEqual(mode(backup.backup_dir()), 0o700)
        self.assertEqual(mode(os.path.join(self.tmp.name, "live.db")), 0o600)
        # a database left 0644 by an older version is tightened on the next start
        other = os.path.join(self.tmp.name, "old.db")
        sqlite3.connect(other).close()
        os.chmod(other, 0o644)
        with db.connect(other):
            pass
        self.assertEqual(mode(other), 0o600)

    def test_verify_leaves_no_companion_files(self):
        with db.connect():
            pass
        # a backup made by an older version: a WAL-mode copy of the live database
        legacy = os.path.join(backup.backup_dir(), "mangarr-20200101-000000.db")
        src = sqlite3.connect(os.path.join(self.tmp.name, "live.db"))
        dst = sqlite3.connect(legacy)
        src.backup(dst)
        dst.close()
        src.close()
        self.assertTrue(backup.verify(legacy)[0])
        backup.restore(legacy)
        self.assertEqual(self.backups_dir_extras(), [])

    # -- #7: a file that fails to migrate never replaces the live database

    def test_unmigratable_file_leaves_live_database_working(self):
        with db.connect() as con:
            db.upsert_series(con, Series(anilist_id=1, english="Live"))
        # schema 0 with tables already there: migrating it would fail with 'table series already exists'
        zero = os.path.join(self.tmp.name, "zero.db")
        con = sqlite3.connect(zero)
        con.executescript("CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT); CREATE TABLE chapter (x);")
        con.close()
        # schema 5 whose chapter table lacks the columns schema 5 has
        mismatch = self.crafted("mismatch.db", version=5)
        con = sqlite3.connect(mismatch)
        con.executescript("DROP TABLE chapter; CREATE TABLE chapter (series_id INTEGER, number REAL);")
        con.close()
        for bad in (zero, mismatch):
            before = len(backup.listing())
            with self.assertRaises(backup.RestoreError):
                backup.restore(bad)
            self.assertEqual(self.titles(), ["Live"])          # untouched, still opens and migrates
            self.assertEqual(len(backup.listing()), before)    # refused before any safety copy
        backup.create("still works")

    def test_restore_repairs_a_live_database_that_cannot_migrate(self):
        with db.connect() as con:
            db.upsert_series(con, Series(anilist_id=1, english="Good"))
        good = backup.create("t")
        con = sqlite3.connect(os.path.join(self.tmp.name, "live.db"))     # break the live database
        con.execute("PRAGMA user_version = 0")
        con.commit()
        con.close()
        with self.assertRaises(sqlite3.OperationalError), db.connect():
            pass
        msg = backup.restore(good)                             # the safety copy does not need a migration
        self.assertIn("restored", msg)
        self.assertEqual(self.titles(), ["Good"])

    def test_older_schema_is_upgraded_in_the_copy(self):
        old = os.path.join(self.tmp.name, "v2.db")
        con = sqlite3.connect(old)
        con.row_factory = sqlite3.Row
        db.migrate(con, target=2)
        con.execute("INSERT INTO series (ref, title, added_at) VALUES ('manual:Old', 'Old', '2020-01-01')")
        con.commit()
        con.close()
        msg = backup.restore(old)
        self.assertIn("schema 2", msg)
        with db.connect() as con:
            row = con.execute("SELECT * FROM series").fetchone()
            self.assertEqual(row["folder"], "Old")             # migration 3's backfill ran
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))

    def test_newer_schema_is_refused(self):
        newer = self.crafted("newer.db", version=len(db.MIGRATIONS) + 1)
        ok, msg = backup.verify(newer)
        self.assertFalse(ok)
        self.assertIn("newer", msg)
        with self.assertRaises(backup.RestoreError):
            backup.restore(newer)

    # -- #88: triggers, views and foreign tables do not come along

    def test_triggers_views_and_extra_tables_are_dropped(self):
        with db.connect():
            pass
        p = self.crafted(sql="""
            INSERT INTO series (ref, title, added_at, folder) VALUES ('manual:A', 'A', '2020-01-01', 'A');
            CREATE TRIGGER t AFTER INSERT ON event BEGIN
              INSERT OR REPLACE INTO setting (key, value) VALUES ('discord_webhook', '"https://attacker/hook"');
            END;
            CREATE VIEW v AS SELECT * FROM series;
            CREATE TABLE loot (x);
        """)
        msg = backup.restore(p)
        self.assertIn("ignored 3 object(s)", msg)
        with db.connect() as con:
            kinds = {(r[0], r[1]) for r in con.execute("SELECT type, name FROM sqlite_master")}
            self.assertNotIn(("trigger", "t"), kinds)
            self.assertNotIn(("view", "v"), kinds)
            self.assertNotIn(("table", "loot"), kinds)
            db.event(con, "added", "x")
            con.commit()
            self.assertIsNone(con.execute("SELECT value FROM setting WHERE key='discord_webhook'").fetchone())
        self.assertEqual(self.titles(), ["A"])

    # -- #116: the current login is kept

    def test_restore_keeps_current_credentials(self):
        with db.connect() as con:
            settings.set_many(con, {"auth_user": "andy", "auth_password": "OldLeakedPw", "api_key": "oldkey",
                                    "auth_method": "basic", "komga_url": "http://old"})
        old = backup.create("t")
        with db.connect() as con:
            settings.set_many(con, {"auth_password": "NewPw", "api_key": "freshkey", "auth_method": "forms",
                                    "komga_url": "http://new"})
        msg = backup.restore(old)
        self.assertIn("login and API key kept", msg)
        v = settings.all_values()                              # refreshed right away, no TTL wait
        self.assertEqual((v["auth_user"], v["api_key"], v["auth_method"]), ("andy", "freshkey", "forms"))
        self.assertTrue(settings.verify_password(v["auth_password"], "NewPw"))      # stored hashed
        self.assertFalse(settings.verify_password(v["auth_password"], "OldLeakedPw"))
        self.assertEqual(v["komga_url"], "http://old")         # everything else is the backup's

    def test_restore_does_not_revive_revoked_sessions(self):
        with db.connect() as con:
            settings.set_many(con, {"revoked_sessions": []}, internal=True)
        old = backup.create("t")
        with db.connect() as con:
            settings.set_many(con, {"revoked_sessions": ["abc:9999999999"]}, internal=True)
        backup.restore(old)
        self.assertEqual(settings.all_values()["revoked_sessions"], ["abc:9999999999"])

    def test_restore_of_a_backup_with_login_does_not_add_one(self):
        with db.connect() as con:
            settings.set_many(con, {"auth_user": "eve", "auth_password": "x"})
        with_login = backup.create("t")
        with db.connect() as con:
            con.execute("DELETE FROM setting WHERE key IN ('auth_user', 'auth_password')")
        backup.restore(with_login)
        self.assertEqual(settings.all_values()["auth_user"], "")

    # -- round 2, #68: what mang-arr left in Suwayomi's queue is about the live Suwayomi, not the data

    def leftovers(self, ids=None):
        with db.connect() as con:
            if ids is not None:
                settings.set_many(con, {"leftover_queue_ids": ids}, internal=True)
            return settings.all_values(con)["leftover_queue_ids"]

    def test_restore_keeps_the_live_leftover_queue_ids(self):
        self.assertIn(downloader.LEFTOVER_KEY, backup.LIVE_SETTINGS)
        self.assertTrue(set(backup.LIVE_SETTINGS) <= set(settings.DEFAULTS))
        self.leftovers([5001])
        old = backup.create("t")
        self.leftovers([7001])                                 # really left in Suwayomi's queue now
        backup.restore(old, keep_auth=False)                   # kept even when the login is not
        self.assertEqual(self.leftovers(), [7001])
        self.leftovers([])
        backup.restore(old)
        self.assertEqual(self.leftovers(), [])                 # 5001 may be the user's own entry by now

    def test_unreadable_live_leftovers_are_not_taken_from_the_backup(self):
        self.leftovers([5001])
        old = backup.create("t")
        real = backup._current_settings
        with mock.patch.object(backup, "_current_settings",
                               lambda keys=backup.PRESERVED_SETTINGS: None if keys == backup.LIVE_SETTINGS else
                               real(keys)):
            backup.restore(old)
        self.assertEqual(self.leftovers(), [])

    # -- #3 / #6 / #38: stored paths from a restored file stay inside their roots

    def test_restored_paths_outside_the_roots_are_cleared(self):
        victim = os.path.join(self.tmp.name, "victim.txt")      # outside library and staging
        with open(victim, "w") as f:
            f.write("keep me")
        own = os.path.join(self.library, "B", "Chapter 001.0.cbz")
        os.makedirs(os.path.dirname(own))
        open(own, "w").close()
        with db.connect():
            pass
        p = self.crafted(sql=f"""
            INSERT INTO series (id, ref, title, added_at, folder) VALUES (1, 'manual:A', 'A', '2020', '{self.tmp.name}');
            INSERT INTO series (id, ref, title, added_at, folder) VALUES (2, 'manual:B', 'B', '2020', 'B');
            INSERT INTO series (id, ref, title, added_at, folder) VALUES (3, 'manual:C', 'C', '2020', '../escape');
            INSERT INTO chapter (series_id, number, status, library_path, staging_path, updated_at)
              VALUES (1, 1, 'have', '{victim}', '{victim}', '2020');
            INSERT INTO chapter (series_id, number, status, library_path, updated_at)
              VALUES (2, 1, 'have', '{own}', '2020');
            INSERT INTO chapter (series_id, number, status, library_path, updated_at)
              VALUES (2, 2, 'have', '{self.library}/B/../../victim.txt', '2020');
            INSERT INTO series_source (series_id, manga_id, source_name, title, match_level, author_level,
              chapter_count, max_chapter, folder, seen_at) VALUES (1, 9, 'S', 'A', 0, 0, 1, 1, '/etc', '2020');
            INSERT INTO series_source (series_id, manga_id, source_name, title, match_level, author_level,
              chapter_count, max_chapter, seen_at) VALUES (2, 8, '../..', 'B', 0, 0, 1, 1, '2020');
        """)
        msg = backup.restore(p)
        self.assertIn("2 series folder name(s) replaced", msg)
        self.assertIn("5 stored path(s)", msg)
        with db.connect() as con:
            folders = {r["id"]: r["folder"] for r in con.execute("SELECT id, folder FROM series")}
            self.assertEqual(folders, {1: "A", 2: "B", 3: "C"})
            paths = {(r["series_id"], r["number"]): (r["library_path"], r["staging_path"])
                     for r in con.execute("SELECT * FROM chapter")}
            self.assertEqual(paths, {(1, 1.0): (None, None), (2, 1.0): (own, None), (2, 2.0): (None, None)})
            self.assertEqual([tuple(r) for r in con.execute("SELECT manga_id, folder FROM series_source")],
                             [(9, None)])                 # folder cleared; the '../..' source entry dropped

    def test_delete_series_only_removes_files_inside_its_folder(self):
        victim = os.path.join(self.tmp.name, "victim.txt")
        with open(victim, "w") as f:
            f.write("keep me")
        other = os.path.join(self.library, "Other", "Chapter 001.0.cbz")
        own = os.path.join(self.library, "B", "Chapter 001.0.cbz")
        link = os.path.join(self.library, "B", "Chapter 002.0.cbz")
        for f in (other, own):
            os.makedirs(os.path.dirname(f), exist_ok=True)
            open(f, "w").close()
        os.symlink(victim, link)                              # resolves outside the library
        client = mock.Mock()
        with db.connect() as con:
            con.execute("INSERT INTO series (id, ref, title, added_at, folder) VALUES (1, 'manual:B', 'B', '2020', 'B')")
            for n, p in ((1, own), (2, link), (3, victim), (4, other)):
                con.execute("INSERT INTO chapter (series_id, number, status, library_path, updated_at)"
                            " VALUES (1, ?, 'have', ?, '2020')", (n, p))
            # and a folder that escapes the library (as a pre-fix restore could have left it)
            con.execute("INSERT INTO series (id, ref, title, added_at, folder) VALUES (2, 'manual:X', 'X', '2020', ?)",
                        (self.tmp.name,))
            con.execute("INSERT INTO chapter (series_id, number, status, library_path, updated_at)"
                        " VALUES (2, 1, 'have', ?, '2020')", (victim,))
            con.commit()
            with self.assertLogs("mangarr.core", "WARNING") as logs:
                core.delete_series(con, client, 1, delete_library=True)
                core.delete_series(con, client, 2, delete_library=True)
        self.assertFalse(os.path.exists(own))                   # its own file is gone
        self.assertTrue(os.path.exists(victim))                 # nothing outside was touched
        self.assertTrue(os.path.exists(other))                  # nor another series' folder
        self.assertTrue(any("outside the series' library folder" in m for m in logs.output))
        self.assertTrue(any("is not a folder inside" in m for m in logs.output))

    def test_series_whose_folder_ends_in_a_space_is_deleted_and_restored_as_is(self):
        # a folder mang-arr made itself (here from 'Foo ...') survives restore as it is and is deleted cleanly
        with db.connect() as con:
            sid = db.upsert_series(con, Series(anilist_id=7, english="Foo ..."))
            folder = con.execute("SELECT folder FROM series WHERE id=?", (sid,)).fetchone()[0]
        self.assertEqual(folder, library.safe_title("Foo ..."))
        p = backup.create("t")
        msg = backup.restore(p)
        self.assertNotIn("folder name(s) replaced", msg)
        own = os.path.join(self.library, folder, "Chapter 001.0.cbz")
        os.makedirs(os.path.dirname(own))
        open(own, "w").close()
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT folder FROM series WHERE id=?", (sid,)).fetchone()[0], folder)
            db.set_have(con, sid, 1.0, None, own)
            core.delete_series(con, mock.Mock(), sid, delete_library=True)
        self.assertFalse(os.path.exists(own))
        self.assertFalse(os.path.exists(os.path.dirname(own)))

    def test_restore_keeps_the_id_counters(self):
        # a deleted series' id must not be handed out again: its events keep that series_id
        with db.connect() as con:
            for i in (1, 2, 3):
                db.upsert_series(con, Series(anilist_id=i, english=f"S{i}"))
            db.event(con, "downloaded", "chapter 1 of the old series 3", 3)
            db.delete_series(con, 3)
        p = backup.create("t")
        backup.restore(p)
        with db.connect() as con:
            sid = db.upsert_series(con, Series(anilist_id=9, english="New"))
        self.assertEqual(sid, 4)

    def test_restore_ignores_an_implausible_id_counter(self):
        p = self.crafted(sql="INSERT INTO series (id, ref, title, added_at, folder) VALUES (1, 'manual:A', 'A', '2020', 'A');"
                             "UPDATE sqlite_sequence SET seq = 9223372036854775807 WHERE name = 'series';")
        with self.assertLogs("mangarr.backup", "WARNING") as logs:
            backup.restore(p)
        self.assertTrue(any("id counter for series" in m for m in logs.output))
        with db.connect() as con:
            self.assertEqual(db.upsert_series(con, Series(anilist_id=9, english="New")), 2)

    # -- uploads (file objects)

    def test_restore_from_an_upload_stream_with_limit(self):
        with db.connect() as con:
            db.upsert_series(con, Series(anilist_id=1, english="Uploaded"))
        p = backup.create("t")
        with db.connect() as con:
            con.execute("DELETE FROM series")
        with open(p, "rb") as f:
            data = f.read()
        with mock.patch.object(backup, "UPLOAD_MAX_MB", 0), self.assertRaises(backup.RestoreError):
            backup.restore(io.BytesIO(data + b"\0" * (3 * os.path.getsize(os.path.join(self.tmp.name, "live.db")))))
        self.assertEqual(self.titles(), [])
        backup.restore(io.BytesIO(data))
        self.assertEqual(self.titles(), ["Uploaded"])
        self.assertEqual(self.backups_dir_extras(), [])

    def test_upload_spool_is_on_disk_as_it_arrives(self):
        # Python 3.14: the spool's 128 KB file buffer held the first chunks back from the disk (and from the
        # free-space check on the next write)
        spool = backup.UploadSpool()
        try:
            for n in (70000, 10, 200000):
                spool.write(b"x" * n)
                self.assertEqual(os.path.getsize(spool.path), spool.size)
        finally:
            spool.discard()
        self.assertEqual(self.backups_dir_extras(), [])

    def test_restore_refused_while_a_download_run_holds_the_lock(self):
        import fcntl
        with db.connect() as con:
            db.upsert_series(con, Series(anilist_id=1, english="Live"))
        p = backup.create("t")
        with open(os.path.join(self.tmp.name, "download.lock"), "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            with self.assertRaises(backup.RestoreError):
                backup.restore(p)
        self.assertEqual(len(backup.listing()), 1)             # no safety copy was made either

    # -- #67 / #90: the event history is bounded

    def test_prune_history(self):
        with db.connect() as con:
            for i in range(30):
                db.event(con, "resolved", f"e{i}")
            con.execute("UPDATE event SET at='2000-01-01 00:00:00' WHERE id <= 5")
        self.assertEqual(backup.prune_history(keep_days=90, keep_rows=20), 10)
        with db.connect() as con:
            self.assertEqual([r[0] for r in con.execute("SELECT message FROM event ORDER BY id")],
                             [f"e{i}" for i in range(10, 30)])

    def test_prune_history_drops_routine_events_before_chapter_history(self):
        with db.connect() as con:
            for i in range(10):
                db.event(con, "downloaded", f"d{i}", 1)
                db.event(con, "resolved", f"r{i}", 1)
        with self.assertLogs("mangarr.db", "INFO") as logs:
            self.assertEqual(backup.prune_history(keep_days=90, keep_rows=12), 8)
        with db.connect() as con:
            left = [r[0] for r in con.execute("SELECT message FROM event ORDER BY id")]
        self.assertEqual(left, [f"d{i}" for i in range(9)] + ["r8", "d9", "r9"])
        self.assertFalse(any("oldest other" in m for m in logs.output))
        # when routine events alone cannot get under the cap, the oldest of any kind go, and the log says so
        with self.assertLogs("mangarr.db", "INFO") as logs:
            self.assertEqual(backup.prune_history(keep_days=90, keep_rows=5), 7)
        with db.connect() as con:
            left = [r[0] for r in con.execute("SELECT message FROM event ORDER BY id")]
        self.assertEqual(left, ["d5", "d6", "d7", "d8", "d9"])
        self.assertTrue(any("MANGARR_EVENTS_KEEP_ROWS" in m for m in logs.output))


@unittest.skipIf(TestClient is None, "web extras not installed")
class BackupRoutesTest(_Base):
    def setUp(self):
        super().setUp()
        from mangarr import jobs
        from mangarr.web import app as web
        self.web = web
        self.runner = jobs.Runner()
        self.runner.start()
        self.patches.append(mock.patch.object(web, "runner", self.runner))
        self.patches[-1].start()
        with db.connect() as con:
            db.upsert_series(con, Series(anilist_id=1, english="Live"))
        self.client = TestClient(web.app)

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def test_get_download_does_not_create_or_prune_backups(self):
        with mock.patch.object(backup, "KEEP", 2):
            kept = [backup.create("t"), backup.create("t")]
            for _ in range(3):
                r = self.client.get("/system/backup")
                self.assertEqual(r.status_code, 200)
                self.assertTrue(r.content.startswith(b"SQLite format 3"))
                self.assertIn("attachment", r.headers["content-disposition"])
            self.assertEqual(sorted(b["name"] for b in backup.listing()), sorted(os.path.basename(p) for p in kept))
        self.assertEqual(self.backups_dir_extras(), [])       # the temporary copies were deleted after sending

    def test_upload_restores_on_the_job_worker(self):
        p = backup.create("t")
        with open(p, "rb") as f:
            data = f.read()
        with db.connect() as con:
            con.execute("DELETE FROM series")
        r = self.client.post("/system/backups/upload", files={"file": ("b.db", data)}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("restored", r.headers["location"])
        self.assertEqual(self.titles(), ["Live"])
        self.assertEqual([j.kind for j in self.runner.jobs()], ["restore"])
        self.assertEqual(self.backups_dir_extras(), [])

    def test_upload_over_the_limit_is_refused(self):
        with mock.patch.object(backup, "UPLOAD_MAX_MB", 0):
            limit = backup.upload_limit()
            r = self.client.post("/system/backups/upload", files={"file": ("b.db", b"x" * (limit + 10))},
                                 follow_redirects=False)
            self.assertIn("upload%20limit", r.headers["location"])

            def chunked():                                   # no Content-Length: the stream is counted
                yield b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"b.db\"\r\n\r\n"
                for _ in range(4):
                    yield b"x" * limit
                yield b"\r\n--b--\r\n"
            r = self.client.post("/system/backups/upload", content=chunked(), follow_redirects=False,
                                 headers={"content-type": "multipart/form-data; boundary=b"})
            self.assertIn("upload%20limit", r.headers["location"])
        self.assertEqual(self.titles(), ["Live"])

    # -- round 2, #24 / #87: uploads cannot fill the disk (one at a time, idle cut-off, free space, data volume)

    HEAD = b'--b\r\nContent-Disposition: form-data; name="file"; filename="b.db"\r\n\r\n'
    TAIL = b"\r\n--b--\r\n"

    async def _asgi_upload(self, chunks, stall=False, headers=()):
        """POST /system/backups/upload straight through the ASGI app, one
        receive() message per bytes chunk (a float in the list: wait that
        many seconds first); with stall=True the client then stops sending
        without going away. Returns (status, headers, flash text)."""
        chunks = list(chunks)
        self.consumed = 0                              # body bytes the server read

        async def receive():
            while chunks and isinstance(chunks[0], float):
                await asyncio.sleep(chunks.pop(0))     # a slow client
            if chunks:
                self.consumed += len(chunks[0])
                return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks) or stall}
            await asyncio.sleep(3600)                  # stalled: only the server's idle cut-off ends this
        sent = []

        async def send(message):
            sent.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": "/system/backups/upload", "raw_path": b"/system/backups/upload",
                 "query_string": b"", "root_path": "", "client": ("127.0.0.1", 50000), "server": ("localhost", 80),
                 "headers": [(b"host", b"localhost"), (b"content-type", b"multipart/form-data; boundary=b"),
                             *headers]}
        await asyncio.wait_for(self.web.app(scope, receive, send), 20)
        start = next(m for m in sent if m["type"] == "http.response.start")
        h = {k.decode().lower(): v.decode() for k, v in start["headers"]}
        return start["status"], h, urllib.parse.unquote(h.get("location", ""))

    def _spooled(self) -> list[str]:
        return [n for n in os.listdir(backup.backup_dir()) if n.startswith(".tmp-upload-")]

    def test_one_upload_at_a_time_and_a_stalled_one_is_cut_off(self):
        # the review's repro: parallel uploads that sent a few MB and stalled each held a spool file in /tmp
        from mangarr.web import uploads
        p = backup.create("t")
        with open(p, "rb") as f:
            data = f.read()

        async def scenario():
            first = asyncio.ensure_future(self._asgi_upload([self.HEAD, b"x" * 70000], stall=True))
            for _ in range(500):                                 # until the first one is being written
                spooled = self._spooled()
                if spooled and os.path.getsize(os.path.join(backup.backup_dir(), spooled[0])) >= 70000:
                    break
                await asyncio.sleep(0.01)
            t0 = time.monotonic()
            second = await self._asgi_upload([self.HEAD, data, self.TAIL])
            took = time.monotonic() - t0
            return spooled, took, second, await first
        with mock.patch.object(uploads, "IDLE_SECS", 0.5):
            spooled, took, second, first = asyncio.run(scenario())
        self.assertEqual(len(spooled), 1)                        # in the backups folder, not /tmp
        self.assertEqual((second[0], second[1].get("connection")), (303, None))   # small: read, then answered
        self.assertIn("another backup upload is in progress", second[2])
        self.assertLess(took, 0.5)                               # refused at once, not queued
        self.assertEqual(first[0], 303)
        self.assertIn("nothing arrived for 0.5 seconds", first[2])
        self.assertEqual(self.backups_dir_extras(), [])          # the partial upload was removed
        self.assertEqual(self.titles(), ["Live"])
        with db.connect() as con:
            con.execute("DELETE FROM series")
        r = self.client.post("/system/backups/upload", files={"file": ("b.db", data)}, follow_redirects=False)
        self.assertIn("restored", r.headers["location"])        # the slot is free again
        self.assertEqual(self.titles(), ["Live"])

    def test_upload_is_written_to_the_data_volume_and_moved_not_copied(self):
        p = backup.create("t")
        with open(p, "rb") as f:
            data = f.read()
        seen = []
        real = backup.restore

        def spy(source, **kw):
            seen.append((type(source).__name__, os.path.dirname(source.path), source.size))
            return real(source, **kw)
        with mock.patch.object(backup, "restore", spy), \
                mock.patch.object(backup.shutil, "copyfile", side_effect=AssertionError("copied")):
            r = self.client.post("/system/backups/upload", files={"file": ("b.db", data)}, follow_redirects=False)
        self.assertIn("restored", r.headers["location"])
        self.assertEqual(seen, [("UploadSpool", backup.backup_dir(), len(data))])
        self.assertEqual(self.backups_dir_extras(), [])

    def test_a_dripping_upload_is_cut_off_and_frees_the_slot(self):
        # round 2: a client sending a byte now and then (under the idle cut-off) kept the only upload slot and
        # its spool for as long as it liked, and every real "Restore from file" was refused meanwhile
        from mangarr.web import uploads
        p = backup.create("t")
        with open(p, "rb") as f:
            data = f.read()
        drip = [self.HEAD, b"x" * 65536] + [0.1, b"x"] * 50       # 5 s if nothing stops it

        async def scenario():
            t0 = time.monotonic()
            first = asyncio.ensure_future(self._asgi_upload(drip))
            await asyncio.sleep(0.3)
            busy = await self._asgi_upload([self.HEAD, data, self.TAIL])
            spooled = self._spooled()
            return busy, spooled, await first, time.monotonic() - t0
        with mock.patch.object(uploads, "IDLE_SECS", 0.5), mock.patch.object(uploads, "MIN_RATE", 256 << 10):
            busy, spooled, first, took = asyncio.run(scenario())
        self.assertIn("another backup upload is in progress", busy[2])    # it did hold the slot ...
        self.assertEqual(len(spooled), 1)
        self.assertEqual(first[0], 303)
        self.assertIn("the upload is too slow", first[2])                 # ... until it fell behind the rate
        self.assertIn("at least 256 KB a second", first[2])
        self.assertIn("copy the file, keeping its mangarr-<date>-<time>.db name, into the backups folder "
                      f"({backup.backup_dir()}) and restore it from the list", first[2])   # round 3: around a slow link
        self.assertLess(took, 2.5)                                        # 0.5 s + 64 KB at 256 KB/s, not 5 s
        self.assertEqual(self.backups_dir_extras(), [])
        with db.connect() as con:
            con.execute("DELETE FROM series")
        r = self.client.post("/system/backups/upload", files={"file": ("b.db", data)}, follow_redirects=False)
        self.assertIn("restored", r.headers["location"])                  # the slot is free again
        self.assertEqual(self.titles(), ["Live"])

    def test_a_steady_upload_with_pauses_is_not_cut_off(self):
        from mangarr.web import uploads
        p = backup.create("t")
        with open(p, "rb") as f:
            data = f.read()
        with db.connect() as con:
            con.execute("DELETE FROM series")
        steady = [self.HEAD]
        for i in range(0, len(data), 16384):                    # ~800 KB/s with a pause after every 16 KB
            steady += [0.02, data[i:i + 16384]]
        steady += [0.3, self.TAIL]                              # and a pause shorter than the idle cut-off
        with mock.patch.object(uploads, "IDLE_SECS", 0.5), mock.patch.object(uploads, "MIN_RATE", 256 << 10):
            status, _, msg = asyncio.run(self._asgi_upload(steady))
        self.assertEqual(status, 303)
        self.assertIn("restored", msg)
        self.assertEqual(self.titles(), ["Live"])

    def test_free_space_floor_grows_with_the_upload(self):
        # round 2: a flat 256 MB floor refused even a KB-sized backup on a volume with less than that free
        mb = 1 << 20
        cases = [(200 * mb, 8192, True),           # a small backup on a nearly full disk
                 (12288, 8192, False),             # ... but not when it would leave less than its own size
                 (300 * mb, 150 * mb, True),       # as much again must stay free ...
                 (299 * mb, 150 * mb, False),
                 (556 * mb, 300 * mb, True),       # ... up to 256 MB
                 (555 * mb, 300 * mb, False)]
        for free, total, ok in cases:
            with self.subTest(free=free, total=total), mock.patch.object(backup, "_free_bytes", return_value=free):
                if ok:
                    backup._check_room(backup.backup_dir(), total, total)
                else:
                    with self.assertRaises(backup.RestoreError):
                        backup._check_room(backup.backup_dir(), total, total)

    def test_small_upload_restores_on_a_nearly_full_disk(self):
        p = backup.create("t")
        with open(p, "rb") as f:
            data = f.read()
        with db.connect() as con:
            con.execute("DELETE FROM series")
        with mock.patch.object(backup, "_free_bytes", return_value=200 << 20):    # below the 256 MB of before
            r = self.client.post("/system/backups/upload", files={"file": ("b.db", data)}, follow_redirects=False)
        self.assertIn("restored", r.headers["location"])
        self.assertEqual(self.titles(), ["Live"])

    def test_upload_refused_up_front_when_the_disk_is_nearly_full(self):
        with mock.patch.object(backup, "_free_bytes", return_value=12288) as free:
            r = self.client.post("/system/backups/upload", files={"file": ("b.db", b"x" * 8192)},
                                 follow_redirects=False)
        msg = urllib.parse.unquote(r.headers["location"])
        self.assertIn(f"not enough free disk space in {backup.backup_dir()} (12 KB free; an upload of 9 KB "
                      "must leave 9 KB free)", msg)
        self.assertIsNone(r.headers.get("connection"))          # the small body was read before answering
        free.assert_called_once_with(backup.backup_dir())       # before the first byte was stored
        self.assertEqual(self.backups_dir_extras(), [])
        self.assertEqual(self.titles(), ["Live"])

    def test_upload_stops_when_the_disk_fills_up_meanwhile(self):
        # round 2: free space was checked again only every 8 MB, so a slowly dripping upload never was
        from mangarr.web import uploads
        calls = []

        def free(path):                                          # plenty at first, then someone else fills the disk
            calls.append(path)
            return (1 << 40) if len(calls) <= 2 else 0
        drip = [self.HEAD, b"x" * 65536] + [0.05, b"x"] * 20     # chunked: no Content-Length to check first
        with mock.patch.object(backup, "_free_bytes", free), mock.patch.object(uploads, "IDLE_SECS", 0.5):
            status, _, msg = asyncio.run(self._asgi_upload(drip))
        self.assertEqual(status, 303)
        self.assertIn("not enough free disk space", msg)
        self.assertEqual(calls, [backup.backup_dir()] * 3)      # before, then with each write: the first drip
        self.assertEqual(self.backups_dir_extras(), [])
        self.assertEqual(self.titles(), ["Live"])

    def test_malformed_uploads_are_refused_and_leave_nothing(self):
        ctype = {"content-type": "multipart/form-data; boundary=b"}
        cases = {
            "choose a .db file": b'--b\r\nContent-Disposition: form-data; name="file"; filename=""\r\n\r\n'
                                 + self.TAIL,
            "ended before the file was complete": self.HEAD + b"x" * 5000,
            "one backup file": self.HEAD + b"x" + b'\r\n--b\r\nContent-Disposition: form-data; name="file"; '
                                                 b'filename="c.db"\r\n\r\ny' + self.TAIL,
            "field named 'file'": self.HEAD.replace(b'name="file"', b'name="other"') + b"x" + self.TAIL,
        }
        for want, body in cases.items():
            with self.subTest(want=want):
                r = self.client.post("/system/backups/upload", content=body, headers=ctype, follow_redirects=False)
                self.assertEqual(r.status_code, 303)
                self.assertIn(want, urllib.parse.unquote(r.headers["location"]))
                self.assertEqual(self.backups_dir_extras(), [])
        r = self.client.post("/system/backups/upload", data={"x": "1"}, follow_redirects=False)   # not multipart
        self.assertIn("choose a .db file", urllib.parse.unquote(r.headers["location"]))
        self.assertEqual(self.titles(), ["Live"])

    def test_bad_upload_is_reported_not_500(self):
        r = self.client.post("/system/backups/upload", files={"file": ("b.db", b"garbage" * 1000)},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("restore%20refused", r.headers["location"])
        r = self.client.post("/system/backups/mangarr-20200101-000000.db/restore", follow_redirects=False)
        self.assertIn("restore%20refused", r.headers["location"])

    # -- round 3: bytes besides the file are capped and not waited for, small refused bodies are read before
    # the answer, and the rate limits can be set

    def _backup_bytes(self) -> bytes:
        with open(backup.create("t"), "rb") as f:
            return f.read()

    def test_bytes_after_the_closing_boundary_do_not_hold_the_slot(self):
        """The verifier's repro: bytes after the closing boundary did not count against the upload limit, so a
        client could keep the only upload slot for MANGARR_MAX_UPLOAD_MB at MIN_RATE (hours). Reading stops at
        the closing boundary: the restore runs and the slot is free while the junk still drips."""
        from mangarr.web import uploads
        data = self._backup_bytes()
        with db.connect() as con:
            con.execute("DELETE FROM series")

        async def scenario():
            first = asyncio.ensure_future(self._asgi_upload([self.HEAD, data, self.TAIL] + [0.1, b"y"] * 100))
            ended = []
            first.add_done_callback(lambda _: ended.append(time.monotonic()))
            for _ in range(500):                                 # restored while the junk still drips
                # the slot too: the route frees it only once it is back from the restore's thread, which can be
                # a moment after the restore job itself has finished
                if self.titles() == ["Live"] and not self.web._restore_guard() and not self.web._upload_slot.locked():
                    break
                await asyncio.sleep(0.01)
            restored = time.monotonic()
            with db.connect() as con:
                con.execute("DELETE FROM series")
            second = await self._asgi_upload([self.HEAD, data, self.TAIL])
            return second, await first, ended[0] - restored
        with mock.patch.object(uploads, "IDLE_SECS", 0.5), mock.patch.object(uploads, "MIN_RATE", 256 << 10):
            second, first, dripped = asyncio.run(scenario())
        self.assertIn("restored", second[2])                     # not "another backup upload is in progress"
        self.assertEqual((first[0], first[1].get("connection")), (303, "close"))
        self.assertIn("restored", first[2])
        # the drip is cut off, not read for its 10 s (timed from the restore: two restores on a busy machine
        # took more than 5 s by themselves)
        self.assertLess(dripped, 5)
        self.assertEqual(self.titles(), ["Live"])
        self.assertEqual(self.backups_dir_extras(), [])

    def test_bytes_besides_the_file_are_capped(self):
        """The same repro before the first boundary: python-multipart skips any number of line breaks there.
        At most MAX_OTHER_BYTES of anything but the file's data is read."""
        from mangarr.web import uploads
        with mock.patch.object(uploads, "IDLE_SECS", 0.5):
            t0 = time.monotonic()
            status, _, msg = asyncio.run(self._asgi_upload([b"\r\n" * 2048] * 10, stall=True))
        self.assertEqual(status, 303)
        self.assertIn("the form holds more than 8 KB besides the file", msg)
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(self.backups_dir_extras(), [])
        body = self.HEAD + b"x" * 5000 + b'\r\n--b\r\nContent-Disposition: form-data; name="note"\r\n\r\n' \
            + b"n" * 4000 + self.TAIL                            # a field within its limits still passes the cap
        status, _, msg = asyncio.run(self._asgi_upload([body]))
        self.assertNotIn("besides the file", msg)
        self.assertIn("restore refused", msg)                    # (not a database: refused by restore itself)

    def test_small_refused_bodies_are_read_before_the_answer(self):
        """The verifier's repro: a refusal sent before the body was read closed the connection with data
        unread, and the kernel's reset can cost a browser (typically on Windows) the answer: most likely the
        job guard during a scheduled refresh. Up to DRAIN_MAX the body is now read first and the connection
        kept; a bigger one is not read at all, and the connection is closed after the answer."""
        from mangarr.web import uploads
        mb = b"x" * (1 << 20)
        for size_mb, drained in ((5, True), ((uploads.DRAIN_MAX >> 20) + 1, False)):
            chunks = [self.HEAD] + [mb] * size_mb + [self.TAIL]
            length = sum(len(c) for c in chunks)
            with self.subTest(size_mb=size_mb), \
                    mock.patch.object(self.web, "_restore_guard", return_value="cannot restore while a job is queued"):
                status, h, msg = asyncio.run(self._asgi_upload(chunks, headers=[(b"content-length",
                                                                                 str(length).encode())]))
                self.assertEqual(status, 303)
                self.assertIn("cannot restore while a job is queued", msg)
                if drained:
                    self.assertEqual((self.consumed, h.get("connection")), (length, None))
                else:
                    self.assertEqual((self.consumed, h.get("connection")), (0, "close"))
        # without a Content-Length the body is read up to DRAIN_MAX, then given up on
        chunks = [self.HEAD] + [mb] * ((uploads.DRAIN_MAX >> 20) + 2) + [self.TAIL]
        with mock.patch.object(self.web, "_restore_guard", return_value="cannot restore while a job is queued"):
            status, h, msg = asyncio.run(self._asgi_upload(chunks))
        self.assertEqual(h.get("connection"), "close")
        self.assertLess(self.consumed, sum(len(c) for c in chunks))

    def test_upload_rate_limits_from_the_environment(self):
        """Round 3: a restore over a slow link (64 KB/s: a phone over Tailscale) was cut off after about 60 s
        with no way to allow it. MANGARR_UPLOAD_MIN_KBPS / MANGARR_UPLOAD_IDLE_SECS set the limits, through
        the same safe parser as every numeric variable."""
        import importlib

        from mangarr.web import uploads
        self.addCleanup(importlib.reload, uploads)
        with mock.patch.dict(os.environ, {"MANGARR_UPLOAD_MIN_KBPS": "32", "MANGARR_UPLOAD_IDLE_SECS": "120"}):
            importlib.reload(uploads)
        self.assertEqual((uploads.MIN_RATE, uploads.IDLE_SECS), (32 << 10, 120.0))
        with mock.patch.dict(os.environ, {"MANGARR_UPLOAD_MIN_KBPS": "1M", "MANGARR_UPLOAD_IDLE_SECS": "0"}), \
                self.assertLogs("mangarr.config", "WARNING") as logs:
            importlib.reload(uploads)
        self.assertEqual((uploads.MIN_RATE, uploads.IDLE_SECS), (128 << 10, 5.0))
        self.assertIn("MANGARR_UPLOAD_MIN_KBPS='1M' is not a number; using the default 128", "\n".join(logs.output))
        self.assertIn("MANGARR_UPLOAD_IDLE_SECS='0' is outside 5..3600; using 5", "\n".join(logs.output))

    # -- #93: no job runs alongside a restore

    def test_restore_waits_for_no_job_and_holds_later_ones(self):
        p = backup.create("t")
        order = []
        release = threading.Event()

        def blocker(job):
            release.wait(5)
        self.runner.submit("refresh", "slow", blocker)
        while self.runner.current is None:
            time.sleep(0.01)
        # the guard sees it and refuses
        r = self.client.post(f"/system/backups/{os.path.basename(p)}/restore", follow_redirects=False)
        self.assertIn("cannot%20restore", r.headers["location"])
        # a job that slips in after the guard: the restore gives up instead of running beside it
        with mock.patch.object(self.web, "_restore_guard", lambda: None), \
                mock.patch.object(self.web, "RESTORE_START_WAIT", 0.2), \
                mock.patch.object(backup, "restore", lambda src: order.append("restore") or "restored"):
            with self.assertRaises(backup.RestoreError):
                self.web._restore_exclusive("x", lambda: backup.restore(p))
            release.set()
            time.sleep(0.3)
            self.assertEqual(order, [])                        # the abandoned restore never ran later either

        # a job submitted while the restore runs starts only after it
        def restoring():
            self.runner.submit("refresh", "later", lambda job: order.append("job"))
            time.sleep(0.2)
            order.append("restore")
            return "ok"
        self.assertEqual(self.web._restore_exclusive("y", restoring), "ok")
        deadline = time.time() + 5
        while len(order) < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(order, ["restore", "job"])


class EventSettingsJsonTest(unittest.TestCase):
    def test_preserved_settings_are_known_or_reserved(self):
        known = set(settings.DEFAULTS)
        self.assertTrue({"auth_user", "auth_password", "auth_method", "api_key"} <= known)
        self.assertTrue(set(backup.PRESERVED_SETTINGS) >= {"auth_user", "auth_password", "auth_method", "api_key"})
        json.dumps(backup.PRESERVED_SETTINGS)


if __name__ == "__main__":
    unittest.main()

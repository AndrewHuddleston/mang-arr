"""Database behaviour that has bitten before: folder uniqueness, stale
wanted rows, migrations on an existing file."""
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from mangarr import db, library
from mangarr.model import Series
from mangarr.resolver import Plan, SourceMatch
from mangarr.suwayomi import Chapter, Source


def plan_with(numbers, manga_id=1):
    src = Source("1", "Src", "en")
    m = SourceMatch(src, manga_id, "T", None, 0, "T", 1,
                    [Chapter(manga_id * 1000 + int(n * 10), float(n), None, None, False) for n in numbers])
    return Plan(Series(english="T"), [m], [], [], {float(n): m for n in numbers},
                candidates={float(n): [m] for n in numbers})


class DbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_chapters_due(self):
        soon = "2999-01-01 00:00:00"
        with db.connect(self.path) as con:
            sids = [db.upsert_series(con, Series(anilist_id=i, english=f"S{i}")) for i in range(1, 5)]
            rows = [(sids[0], 1, "wanted", None), (sids[0], 2, "failed", "2000-01-01 00:00:00"),
                    (sids[1], 1, "wanted", None), (sids[1], 2, "failed", soon), (sids[1], 3, "wanted", None),
                    (sids[2], 1, "have", None), (sids[2], 2, "failed", None),
                    (sids[3], 1, "failed", soon), (sids[3], 2, "wanted", None)]
            con.executemany("INSERT INTO chapter (series_id, number, status, next_try, updated_at) VALUES (?, ?, ?, ?,"
                            " '2000-01-01 00:00:00')", rows)
            # in order, the chapters after a failed one that waits for its next attempt wait for it
            self.assertEqual(db.chapters_due(con, in_order=True), {sids[0]: 2, sids[1]: 1, sids[2]: 1})
            self.assertEqual(db.chapters_due(con), {sids[0]: 2, sids[1]: 2, sids[2]: 1, sids[3]: 1})

    def test_same_title_gets_distinct_folders(self):
        with db.connect(self.path) as con:
            a = db.upsert_series(con, Series(anilist_id=1, english="Wind Breaker"))
            b = db.upsert_series(con, Series(anilist_id=2, english="Wind Breaker"))
            fa, fb = db.get_series(con, a)["folder"], db.get_series(con, b)["folder"]
        self.assertEqual(fa, "Wind Breaker")
        self.assertEqual(fb, "Wind Breaker (anilist_2)")
        self.assertNotEqual(fa, fb)

    def test_stale_wanted_becomes_unavailable_and_returns(self):
        with db.connect(self.path) as con:
            sid = db.upsert_series(con, Series(anilist_id=3, english="S"))
            db.save_plan(con, sid, plan_with([1, 2, 3]), 1)
            self.assertEqual(db.wanted(con, sid), [1.0, 2.0, 3.0])
            db.save_plan(con, sid, plan_with([1, 2]), 1)         # 3 vanished from every source
            self.assertEqual(db.wanted(con, sid), [1.0, 2.0])
            status = {c["number"]: c["status"] for c in db.chapters(con, sid)}
            self.assertEqual(status[3.0], "unavailable")
            db.save_plan(con, sid, plan_with([1, 2, 3]), 1)      # and it is back
            self.assertEqual(db.wanted(con, sid), [1.0, 2.0, 3.0])

    def test_have_and_ignored_survive_replanning(self):
        with db.connect(self.path) as con:
            sid = db.upsert_series(con, Series(anilist_id=4, english="S"))
            db.save_plan(con, sid, plan_with([1, 2]), 1)
            db.set_have(con, sid, 1.0, "/staging/1.cbz", "/lib/1.cbz")
            db.set_status(con, sid, 2.0, "ignored")
            db.save_plan(con, sid, plan_with([1, 2]), 1)
            status = {c["number"]: c["status"] for c in db.chapters(con, sid)}
            self.assertEqual(status, {1.0: "have", 2.0: "ignored"})
            self.assertEqual(db.wanted(con, sid), [])

    def test_manual_ref_keeps_case(self):
        s = Series(english="Let's Play")
        self.assertEqual(s.ref, "manual:Let's Play")



class RetryScheduleTest(unittest.TestCase):
    def test_failed_chapters_back_off(self):
        with tempfile.TemporaryDirectory() as tmp, db.connect(os.path.join(tmp, "r.db")) as con:
            sid = db.upsert_series(con, Series(anilist_id=9, english="R"))
            db.save_plan(con, sid, plan_with([1, 2]), 1)
            db.set_status(con, sid, 1.0, "failed", "boom")           # 1st failure: retry next pass
            self.assertEqual(db.wanted(con, sid), [1.0, 2.0])
            db.set_status(con, sid, 1.0, "failed", "boom")           # 2nd: a day later
            self.assertEqual(db.wanted(con, sid), [2.0])
            row = next(c for c in db.chapters(con, sid) if c["number"] == 1.0)
            self.assertEqual(row["tries"], 2)
            self.assertIn("next attempt after", row["reason"])
            db.save_plan(con, sid, plan_with([1, 2]), 1)              # re-resolve keeps the schedule
            self.assertEqual(next(c for c in db.chapters(con, sid) if c["number"] == 1.0)["status"], "failed")
            db.set_status(con, sid, 1.0, "have")                       # success resets
            self.assertEqual(next(c for c in db.chapters(con, sid) if c["number"] == 1.0)["tries"], 0)



class ReliabilityTest(unittest.TestCase):
    def test_reliability_smoothing(self):
        with tempfile.TemporaryDirectory() as tmp, db.connect(os.path.join(tmp, "s.db")) as con:
            self.assertEqual(db.reliability(con), {})
            for _ in range(8):
                db.record_source_result(con, "Good", "ok")
            db.record_source_result(con, "Good", "failed")
            for _ in range(5):
                db.record_source_result(con, "Bad", "failed")
            db.record_source_result(con, "Bad", "corrupt")
            r = db.reliability(con)
            self.assertGreater(r["Good"], 0.75)
            self.assertLess(r["Bad"], 0.25)
            self.assertEqual(db.source_stats(con)["Bad"]["corrupt"], 1)


class AutoThrottleTest(unittest.TestCase):
    def test_detected_rate_limit_expires(self):
        with tempfile.TemporaryDirectory() as tmp, db.connect(os.path.join(tmp, "t.db")) as con:
            self.assertEqual(db.auto_throttled(con), set())
            db.record_throttle(con, "Manganato (EN)")
            db.record_throttle(con, "Manganato (EN)")
            self.assertEqual(db.auto_throttled(con), {"manganato (en)"})
            self.assertEqual(db.source_stats(con)["Manganato (EN)"]["throttled"], 2)
            con.execute("UPDATE source_stats SET last_throttled='2000-01-01 00:00:00'")
            self.assertEqual(db.auto_throttled(con), set())


class MigrationTest(unittest.TestCase):
    """Each migration is one transaction with its version bump (#32, #74)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "m.db")

    def tearDown(self):
        self.tmp.cleanup()

    def version(self):
        con = sqlite3.connect(self.path)
        try:
            return con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()

    def test_failed_migration_is_rolled_back_and_retried_cleanly(self):
        with db.connect(self.path):
            pass
        n = len(db.MIGRATIONS)
        broken = [*db.MIGRATIONS, "ALTER TABLE series ADD COLUMN extra1 TEXT;\nALTER TABLE nope ADD COLUMN x TEXT;\n"]
        with mock.patch.object(db, "MIGRATIONS", broken), self.assertRaises(sqlite3.OperationalError):
            with db.connect(self.path):
                pass
        self.assertEqual(self.version(), n)                    # the version did not move ...
        con = sqlite3.connect(self.path)
        cols = [r[1] for r in con.execute("PRAGMA table_info(series)")]
        con.close()
        self.assertNotIn("extra1", cols)                       # ... and neither did the first ALTER
        fixed = [*db.MIGRATIONS, "ALTER TABLE series ADD COLUMN extra1 TEXT;\nALTER TABLE series ADD COLUMN extra2 TEXT;\n"]
        with mock.patch.object(db, "MIGRATIONS", fixed), db.connect(self.path) as con:   # a corrected release works
            cols = [r[1] for r in con.execute("PRAGMA table_info(series)")]
        self.assertIn("extra2", cols)
        self.assertEqual(self.version(), n + 1)

    def test_concurrent_first_connections_migrate_once(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        db.migrate(con, target=2)
        con.close()
        errors = []

        def open_it():
            try:
                with db.connect(self.path) as c:
                    c.execute("SELECT COUNT(*) FROM series").fetchone()
            except Exception as e:                            # noqa: BLE001 - collected for the assertion
                errors.append(e)
        threads = [threading.Thread(target=open_it) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.version(), len(db.MIGRATIONS))

    def test_every_migration_splits_into_statements(self):
        for sql in db.MIGRATIONS:
            stmts = db._statements(sql)
            self.assertTrue(stmts)
            for st in stmts:
                self.assertTrue(sqlite3.complete_statement(st))


class EventHistoryTest(unittest.TestCase):
    """The event table is indexed per series and bounded (#67, #90)."""

    def test_index_long_messages_and_pruning(self):
        with tempfile.TemporaryDirectory() as tmp, db.connect(os.path.join(tmp, "e.db")) as con:
            plan = " ".join(r[3] for r in con.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM event WHERE series_id=? ORDER BY id DESC LIMIT 15", (1,)))
            self.assertIn("event_series", plan)
            db.event(con, "resolved", "x" * 50000, 1)
            self.assertLessEqual(len(con.execute("SELECT message FROM event").fetchone()[0]), db.EVENT_MESSAGE_MAX)
            for i in range(10):
                db.event(con, "resolved", f"m{i}", 1)
            con.execute("UPDATE event SET at='2001-01-01 00:00:00' WHERE message='m0'")
            self.assertEqual(db.prune_events(con, keep_days=30, keep_rows=5), 6)   # m0 by age, 5 more by count
            self.assertEqual([r[0] for r in con.execute("SELECT message FROM event ORDER BY id")],
                             [f"m{i}" for i in range(5, 10)])


class PathSafetyTest(unittest.TestCase):
    def test_series_size_only_counts_library_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = os.path.join(tmp, "lib")
            os.makedirs(os.path.join(lib, "S"))
            inside, outside = os.path.join(lib, "S", "c.cbz"), os.path.join(tmp, "secret")
            for p in (inside, outside):
                with open(p, "wb") as f:
                    f.write(b"x" * 10)
            with mock.patch("mangarr.config.LIBRARY_ROOT", lib), db.connect(os.path.join(tmp, "p.db")) as con:
                sid = db.upsert_series(con, Series(anilist_id=5, english="S"))
                db.set_have(con, sid, 1.0, None, inside)
                db.set_have(con, sid, 2.0, None, outside)
                self.assertEqual(db.series_size(con, sid), (10, 1))

    def test_valid_folder(self):
        for ok in ("Wind Breaker", "Wind Breaker (anilist_2)", "untitled"):
            self.assertTrue(db.valid_folder(ok), ok)
        for bad in ("", ".", "..", "/etc", "../x", "a/b", "a\\b", "a\0b", None, 3):
            self.assertFalse(db.valid_folder(bad), bad)

    def test_valid_folder_accepts_every_folder_mang_arr_makes(self):
        # safe_title is not idempotent ('Foo ...' -> 'Foo '): its output must still count as valid
        for title in ("Foo ...", "Why Me .", " x ", "a:b", "..", "...", "/", "Wind Breaker"):
            folder = library.unique_folder(title, set(), "anilist:1")
            self.assertTrue(db.valid_folder(folder), (title, folder))


if __name__ == "__main__":
    unittest.main()

"""Database behaviour that has bitten before: folder uniqueness, stale
wanted rows, migrations on an existing file."""
import os
import tempfile
import unittest

from mangarr import db
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


if __name__ == "__main__":
    unittest.main()

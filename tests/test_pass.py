"""Refresh pass ordering: missing chapters first, complete finished series skipped until due."""
import time
import unittest
from unittest import mock

try:
    from mangarr.web import app as web
except ImportError:                      # web extras not installed
    web = None


def row(title, status="RELEASING", wanted=0, monitored=1, last=None, sid=None, have=10, error=None, listed=None):
    return {"id": sid, "title": title, "status": status, "wanted": wanted, "monitored": monitored,
            "last_resolved": last, "have": have, "last_error": error,
            "listed": have + wanted if listed is None else listed}


def ago(days):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))


@unittest.skipIf(web is None, "web extras not installed")
class PassTest(unittest.TestCase):
    def test_order_and_skip(self):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        rows = [row("B ongoing"), row("A finished complete", "FINISHED", 0, last=now),
                row("C finished missing", "FINISHED", 3, last=now), row("D unmonitored", wanted=5, monitored=0),
                row("E finished stale", "FINISHED", 0, last=ago(30)), row("F ongoing missing", wanted=2)]
        with mock.patch("mangarr.web.app.settings.get", lambda k: 7.0):
            keep, skippable = web.plan_pass(rows)
        self.assertEqual([r["title"] for r in keep],
                         ["C finished missing", "F ongoing missing", "B ongoing", "E finished stale"])
        self.assertEqual([(r["title"], left) for r, left in skippable], [("A finished complete", 7)])

    def test_zero_days_means_always(self):
        rows = [row("A", "FINISHED", 0, last=time.strftime("%Y-%m-%d %H:%M:%S"))]
        with mock.patch("mangarr.web.app.settings.get", lambda k: 0):
            keep, skippable = web.plan_pass(rows)
        self.assertEqual(len(keep), 1)
        self.assertEqual(skippable, [])

    def test_due_first_then_continuing_then_the_rest_in_the_order_given(self):
        rows = [row("Z hiatus", "HIATUS", sid=1), row("Y failed later", "FINISHED", 2, last=ago(1), sid=2),
                row("X ongoing", sid=3), row("W failed now", "RELEASING", 1, sid=4),
                row("V finished done long ago", "FINISHED", 0, last=ago(8), sid=5), row("U ongoing", sid=6),
                row("T wanted", "FINISHED", 4, last=ago(1), sid=7), row("S unknown", None, sid=8),
                row("R cancelled complete", "CANCELLED", 0, last=ago(2.5), sid=9),
                row("Q hiatus complete", "HIATUS", 0, last=ago(1), sid=10)]
        due = {4: 1, 7: 4}                         # Y's failed chapters wait for their next attempt
        with mock.patch("mangarr.web.app.settings.get", lambda k: 7.0), \
                self.assertLogs("mangarr.web.app", "DEBUG") as cm:
            keep, skippable = web.plan_pass(rows, due)
        self.assertEqual([r["title"] for r in keep],
                         ["W failed now", "T wanted", "X ongoing", "U ongoing", "Z hiatus", "Y failed later",
                          "V finished done long ago", "S unknown", "Q hiatus complete"])
        # never skipped: failed chapters (their retry schedule), hiatus, continuing
        self.assertEqual([(r["title"], left) for r, left in skippable], [("R cancelled complete", 5)])
        self.assertIn("pass order: W failed now in group 1 (1 chapter(s) due)", "\n".join(cm.output))
        self.assertTrue(all(line.startswith("DEBUG:") for line in cm.output))

    def test_skip_text(self):
        self.assertEqual(web._skip_text(row("A", "FINISHED"), 3),
                         "skipped: complete and finished; next check in about 3 day(s)")
        self.assertEqual(web._skip_text(row("A", "CANCELLED"), 1),
                         "skipped: complete and cancelled; next check in about 1 day(s)")
        self.assertEqual(web._skip_text(row("A", "FINISHED", have=38, listed=40), 2),      # 2 unavailable
                         "skipped: finished, nothing to download (2 chapter(s) no source lists); next check in "
                         "about 2 day(s)")

    def test_a_series_with_nothing_on_disk_is_never_complete(self):
        now = ago(0.1)
        rows = [row("A no source matched", "CANCELLED", last=now, sid=1, have=0, listed=0,
                    error="no usable source has this series"),
                row("B every chapter unavailable", "FINISHED", last=now, sid=2, have=0, listed=40),
                row("C matched before, not this time", "FINISHED", last=now, sid=3, error="no usable source has "
                                                                                           "this series"),
                row("D complete", "FINISHED", last=now, sid=4)]
        with mock.patch("mangarr.web.app.settings.get", lambda k: 7.0):
            keep, skippable = web.plan_pass(rows, {})
        self.assertEqual([r["title"] for r in keep], [r["title"] for r in rows[:3]])
        self.assertEqual([r["title"] for r, _ in skippable], ["D complete"])


@unittest.skipIf(web is None, "web extras not installed")
class RunPassTest(unittest.TestCase):
    def test_items_track_each_series(self):
        from mangarr import core, jobs
        rows = [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}, {"id": 3, "title": "C"}]

        class Result:
            downloaded = imported = 0

        def fake_refresh(con, client, sid, **kw):
            if sid == 2:
                raise RuntimeError("Suwayomi unreachable")
            kw["progress"]("downloading")
            return Result()
        outcomes = {1: ("done", "complete: nothing missing"), 3: ("nomatch", "no match: rejected titles: X")}
        job = jobs.Job(1, "refresh-all", "all")
        with mock.patch.object(core, "refresh_series", fake_refresh), \
             mock.patch.object(core, "describe_outcome", lambda con, sid, o: outcomes[sid]), \
             mock.patch.object(core, "downloads_due", lambda con, o: []), \
             mock.patch.object(web, "_record_error", lambda sid, e: None), \
             mock.patch.object(web.db, "connect", mock.MagicMock()):
            done, dl, imp, errors = web._run_pass(job, rows, "test")
        self.assertEqual([i["state"] for i in job.items], ["done", "error", "nomatch"])
        self.assertIn("unreachable", job.items[1]["result"])
        self.assertEqual(job.items[2]["result"], "no match: rejected titles: X")
        self.assertEqual((done, errors), (3, 1))          # done = processed, errors included

    def test_complete_finished_series_a_stop_did_not_get_to_are_skipped_not_cut(self):
        from mangarr import core, jobs, lanes
        from mangarr.suwayomi import SuwayomiUnreachable
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        rows = [row("A", sid=1), row("B", sid=2)]
        later = [(dict(row(f"Done {k}", "FINISHED", last=now, sid=10 + k), ref=f"manual:{k}", expected=10), 3)
                 for k in range(5)]

        def down(con, client, sid, **kw):
            raise SuwayomiUnreachable("connection refused")
        job = jobs.Job(1, "refresh-all", "all")
        with mock.patch.object(core, "refresh_series", down), mock.patch.object(lanes, "HOLD_SECS", 0.01), \
                mock.patch.object(web, "_record_error", lambda sid, e: None), \
                mock.patch.object(web.db, "connect", mock.MagicMock()), \
                mock.patch.object(web.metadata, "statuses", side_effect=AssertionError("not asked")), \
                self.assertLogs("mangarr.web.app", "WARNING"), self.assertRaises(web.PassStopped) as cm:
            web._run_pass(job, rows, "test", later)
        self.assertEqual([i["state"] for i in job.items], ["error", "error"] + ["skipped"] * 5)
        self.assertEqual(job.items[2]["result"], "skipped: complete and finished; next check in about 3 day(s)")
        self.assertEqual(str(cm.exception), "pass stopped after 2 of 7 series, 5 complete finished series skipped: "
                                            "Suwayomi is not answering (connection refused)")
        self.assertEqual(cm.exception.counts, (2, 0, 0, 2))

    def test_the_stop_text_counts_only_what_the_stop_cut(self):
        items = [{"state": "cancelled", "result": "series was deleted"},
                 {"state": "cancelled", "result": "pass stopped: Suwayomi is not answering after 2 chapter(s) "
                                                  "downloaded"},
                 {"state": "error", "result": "x"},
                 {"state": "cancelled", "result": "pass stopped: Suwayomi is not answering"},
                 {"state": "done", "result": "1 downloaded"},
                 {"state": "cancelled", "result": "pass stopped: Suwayomi is not answering"}]
        self.assertEqual(web._pass_stopped_text(items, 5, "why"),
                         "pass stopped after 5 of 6 series, 1 not checked, 1 not downloaded, 1 downloaded in part: "
                         "Suwayomi is not answering (why)")
        self.assertEqual(web._pass_stopped_text(items[:1], 1, "why"),
                         "pass stopped after 1 of 1 series: Suwayomi is not answering (why)")
        skipped = {"state": "skipped", "result": "skipped: complete and finished; next check in about 3 day(s)"}
        self.assertEqual(web._pass_stopped_text(items[:1] + [dict(skipped) for _ in range(5)], 1, "why"),
                         "pass stopped after 1 of 6 series, 5 complete finished series skipped: Suwayomi is not "
                         "answering (why)")


if __name__ == "__main__":
    unittest.main()

"""Refresh pass ordering: missing chapters first, complete finished series skipped until due."""
import time
import unittest
from unittest import mock

try:
    from mangarr.web import app as web
except ImportError:                      # web extras not installed
    web = None


def row(title, status="RELEASING", wanted=0, monitored=1, last=None):
    return {"title": title, "status": status, "wanted": wanted, "monitored": monitored, "last_resolved": last}


@unittest.skipIf(web is None, "web extras not installed")
class PassTest(unittest.TestCase):
    def test_order_and_skip(self):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 30 * 86400))
        rows = [row("B ongoing"), row("A finished complete", "FINISHED", 0, last=now),
                row("C finished missing", "FINISHED", 3, last=now), row("D unmonitored", wanted=5, monitored=0),
                row("E finished stale", "FINISHED", 0, last=old), row("F ongoing missing", wanted=2)]
        with mock.patch("mangarr.web.app.settings.get", lambda k: 7.0):
            keep, skipped = web.plan_pass(rows)
        self.assertEqual([r["title"] for r in keep],
                         ["C finished missing", "F ongoing missing", "B ongoing", "E finished stale"])
        self.assertEqual(skipped, 1)

    def test_zero_days_means_always(self):
        rows = [row("A", "FINISHED", 0, last=time.strftime("%Y-%m-%d %H:%M:%S"))]
        with mock.patch("mangarr.web.app.settings.get", lambda k: 0):
            keep, skipped = web.plan_pass(rows)
        self.assertEqual(len(keep), 1)
        self.assertEqual(skipped, 0)


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


if __name__ == "__main__":
    unittest.main()

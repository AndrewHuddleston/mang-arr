"""Refresh pass ordering: missing chapters first, complete finished series skipped until due."""
import time
import unittest
from unittest import mock

from mangarr.web import app as web


def row(title, status="RELEASING", wanted=0, monitored=1, last=None):
    return {"title": title, "status": status, "wanted": wanted, "monitored": monitored, "last_resolved": last}


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


if __name__ == "__main__":
    unittest.main()

"""downloader.download with a fake Suwayomi: chapters fall back to the next
source when one fails, dead sources are dropped, nothing sleeps."""
import tempfile
import unittest
from unittest import mock

from mangarr import downloader
from mangarr.model import Series
from mangarr.resolver import Plan, SourceMatch
from mangarr.suwayomi import Chapter, Source


def match(name, manga_id, numbers, throttled=False):
    src = Source(str(manga_id), name, "en")
    chapters = [Chapter(manga_id * 1000 + int(n * 10), float(n), f"Chapter {n}", None, False) for n in numbers]
    return SourceMatch(src, manga_id, name, None, 0, name, 1, chapters)


class FakeClient:
    """Downloads every enqueued chapter except those in `broken`."""

    def __init__(self, broken=()):
        self.broken = set(broken)          # chapter ids that always fail
        self.have: set[int] = set()
        self.queued: list[int] = []
        self.calls: list[str] = []

    def enqueue(self, ids): self.queued = list(ids)
    def dequeue(self, ids): self.queued = [c for c in self.queued if c not in ids]
    def start(self):
        self.calls.append(f"start {sorted(self.queued)}")
        for cid in self.queued:
            if cid not in self.broken:
                self.have.add(cid)
        self.queued = [c for c in self.queued if c in self.broken]     # finished ones leave the queue
    def stop(self): pass
    def queue(self):
        return [{"id": cid, "state": "ERROR", "tries": 3, "progress": 0.0} for cid in self.queued]
    def downloaded_ids(self, manga_id):
        return {cid for cid in self.have if cid // 1000 == manga_id}


def plan_for(matches):
    cands = {}
    for m in matches:
        for n in m.numbers:
            cands.setdefault(n, []).append(m)
    return Plan(Series(english="T"), matches, [], [], {n: c[0] for n, c in cands.items()}, candidates=cands)


class FallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = self.tmp.name + "/lock"
        self.patches = [mock.patch("mangarr.downloader.time.sleep", lambda s: None),
                        mock.patch("mangarr.config.DB_PATH", self.tmp.name + "/test.db")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def run_download(self, client, plan, only):
        with mock.patch.object(downloader.config, "LOCK_PATH", self.lock):
            return downloader.download(client, plan, only=only)

    def test_all_ok_first_source(self):
        a, b = match("A", 1, [1, 2, 3]), match("B", 2, [1, 2, 3])
        client = FakeClient()
        res = self.run_download(client, plan_for([a, b]), {1.0, 2.0, 3.0})
        self.assertEqual(res, {1.0: "ok", 2.0: "ok", 3.0: "ok"})
        self.assertTrue(all("start" in c and "[1010" in c or "[10" in c for c in client.calls))

    def test_falls_back_per_chapter(self):
        a, b = match("A", 1, [1, 2, 3]), match("B", 2, [1, 2, 3])
        client = FakeClient(broken={1020})            # A's chapter 2 is broken
        res = self.run_download(client, plan_for([a, b]), {1.0, 2.0, 3.0})
        self.assertEqual(res, {1.0: "ok", 2.0: "ok", 3.0: "ok"})
        self.assertIn(2020, client.have)              # came from B

    def test_fails_when_every_source_fails(self):
        a, b = match("A", 1, [5]), match("B", 2, [5])
        client = FakeClient(broken={1050, 2050})
        reasons = {}
        with mock.patch.object(downloader.config, "LOCK_PATH", self.lock):
            res = downloader.download(client, plan_for([a, b]), only={5.0}, reasons=reasons)
        self.assertEqual(res, {5.0: "failed"})
        self.assertIn("A:", reasons[5.0])
        self.assertIn("B:", reasons[5.0])

    def test_single_source_failure_says_so(self):
        a = match("A", 1, [7])
        client = FakeClient(broken={1070})
        reasons = {}
        with mock.patch.object(downloader.config, "LOCK_PATH", self.lock):
            res = downloader.download(client, plan_for([a]), only={7.0}, reasons=reasons)
        self.assertEqual(res, {7.0: "failed"})
        self.assertIn("no other source has this chapter", reasons[7.0])

    def test_dead_source_not_retried(self):
        a, b = match("A", 1, [1, 2]), match("B", 2, [1, 2, 3])
        client = FakeClient(broken={1010, 1020})      # A delivers nothing at all
        res = self.run_download(client, plan_for([a, b]), {1.0, 2.0, 3.0})
        self.assertEqual(res, {1.0: "ok", 2.0: "ok", 3.0: "ok"})
        starts_on_a = [c for c in client.calls if "[1010" in c or "[1020" in c]
        self.assertLessEqual(len(starts_on_a), 2)     # short backoff since B has them, then dropped

    def test_no_source_at_all(self):
        a = match("A", 1, [1])
        res = self.run_download(FakeClient(), plan_for([a]), {1.0, 9.0})
        self.assertEqual(res[9.0], "failed")
        self.assertEqual(res[1.0], "ok")



class DownloadOneTest(unittest.TestCase):
    def test_download_one(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("mangarr.downloader.time.sleep", lambda s: None), \
             mock.patch("mangarr.config.DB_PATH", tmp + "/test.db"), \
             mock.patch("mangarr.config.LOCK_PATH", tmp + "/lock"):
            a = match("A", 1, [3, 4])
            ok, failed, why = downloader.download_one(FakeClient(), 1, a.chapters[0], "T", "A")
            self.assertTrue(ok)
            ok, failed, why = downloader.download_one(FakeClient(broken={1040}), 1, a.chapters[1], "T", "A")
            self.assertFalse(ok)
            self.assertEqual(failed, [4.0])
            self.assertIn(4.0, why)


class InOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch("mangarr.downloader.time.sleep", lambda s: None),
                        mock.patch("mangarr.config.DB_PATH", self.tmp.name + "/test.db"),
                        mock.patch("mangarr.config.LOCK_PATH", self.tmp.name + "/lock")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def run_in_order(self, client, plan, only):
        reasons = {}
        res = downloader.download(client, plan, only=only, reasons=reasons, in_order=True)
        return res, reasons

    def test_stops_at_a_chapter_no_source_has(self):
        a = match("A", 1, [1, 2, 3, 4, 5])
        client = FakeClient(broken={1030})                      # ch 3 is dead on the only source
        res, reasons = self.run_in_order(client, plan_for([a]), {1.0, 2.0, 3.0, 4.0, 5.0})
        self.assertEqual(res, {1.0: "ok", 2.0: "ok", 3.0: "failed"})
        self.assertNotIn(4.0, res)                              # never downloaded past the gap
        self.assertIn("waiting for chapter 3", reasons[4.0])
        self.assertIn("waiting for chapter 3", reasons[5.0])
        self.assertNotIn(1040, client.have)

    def test_fallback_keeps_the_order(self):
        a, b = match("A", 1, [1, 2, 3, 4]), match("B", 2, [1, 2, 3, 4])
        client = FakeClient(broken={1030})                      # ch 3 dead on A, fine on B
        res, _ = self.run_in_order(client, plan_for([a, b]), {1.0, 2.0, 3.0, 4.0})
        self.assertEqual(res, {1.0: "ok", 2.0: "ok", 3.0: "ok", 4.0: "ok"})
        self.assertIn(2030, client.have)

    def test_off_downloads_past_the_gap(self):
        a = match("A", 1, [1, 2, 3, 4])
        client = FakeClient(broken={1030})
        res = downloader.download(client, plan_for([a]), only={1.0, 2.0, 3.0, 4.0}, in_order=False)
        self.assertEqual(res[4.0], "ok")
        self.assertEqual(res[3.0], "failed")


if __name__ == "__main__":
    unittest.main()

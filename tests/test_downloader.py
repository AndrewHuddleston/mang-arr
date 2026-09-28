"""downloader.download with a fake Suwayomi: chapters fall back to the next
source when one fails, dead sources are dropped, nothing sleeps. The steps
of one series (SeriesSteps), the wait on Suwayomi's queue on a fake clock,
the rate-limit backoff and the split download lock."""
import fcntl
import os
import random
import tempfile
import threading
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
    def dequeue(self, ids, timeout=30): self.queued = [c for c in self.queued if c not in ids]
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
        attempts = []
        with mock.patch.object(downloader.config, "LOCK_PATH", self.lock):
            res = downloader.download(client, plan_for([a, b]), only={1.0, 2.0, 3.0}, attempts=attempts)
        self.assertEqual(res, {1.0: "ok", 2.0: "ok", 3.0: "ok"})
        self.assertIn(2020, client.have)              # came from B
        self.assertEqual(sorted(attempts), [("A", 1.0, "ok"), ("A", 2.0, "failed"), ("A", 3.0, "ok"),
                                            ("B", 2.0, "ok")])      # who did it, not the first choice

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
        self.assertLessEqual(len(starts_on_a), 2)     # short backoff since B has them, then asked last

    def test_a_dead_source_still_gets_a_chapter_the_others_fail(self):
        # A's ch 1 is broken, so A delivered nothing in its first run; B's ch 2 is broken: ch 2 comes from A
        a, b = match("A", 1, [1, 2]), match("B", 2, [1, 2])
        client = FakeClient(broken={1010, 2020})
        res = self.run_download(client, plan_for([a, b]), {1.0, 2.0})
        self.assertEqual(res, {1.0: "ok", 2.0: "ok"})
        self.assertEqual(client.have, {2010, 1020})

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


def steps_for(matches, wanted, in_order=True):
    reasons = {}
    return downloader.SeriesSteps(plan_for(matches), set(map(float, wanted)), in_order, "T", reasons), reasons


def drive(steps, broken, choose, tiers=None):
    """Run a series' steps to the end the way _download_source would: in
    order a run stops at its first broken chapter ((source name, number) in
    `broken`). choose(wants, alternatives) picks the site of each run.
    Returns [(source name, chapters asked)]. With `tiers` (a TestCase), check
    that a run on an alternative is never of a worse tier than the chapter's
    next source, nor on a source that delivered nothing."""
    runs = []
    while keys := steps.wants(set()):
        alts = steps.alternatives(set())
        key = choose(keys, alts)
        if tiers is not None and key in alts:
            n = steps.order[steps.idx] if steps.in_order else None
            first = steps._next_source(n) if n is not None else None
            run = steps.take(key, set())
            tiers.assertNotIn(run.match.manga_id, steps.dead)
            if first is not None:
                tiers.assertLessEqual(run.match.source.tier, first.source.tier)
        else:
            run = steps.take(key, set())
        if run is None:
            continue
        ok, failed = [], []
        for c in run.todo:
            if (run.match.source.name, c.number) in broken:
                failed.append(c.number)
                if run.in_order:
                    break
            else:
                ok.append(c.number)
        steps.record(run, ok, failed, {n: "broken" for n in failed})
        runs.append((run.match.source.name, [c.number for c in run.todo]))
        if len(runs) > 100:
            raise AssertionError(f"no end in sight: {runs}")
    return runs


def switch_once():
    """choose() for drive: the first time there is an alternative, take it (the next site is busy)."""
    done = []

    def choose(keys, alts):
        if alts and not done:
            done.append(alts[0])
            return alts[0]
        return keys[0]
    return choose


class SeriesStepsTest(unittest.TestCase):
    def test_in_order_wants_only_the_next_chapters_site(self):
        steps, _ = steps_for([match("A", 1, [1, 2]), match("B (EN)", 2, [3])], [1, 2, 3])
        self.assertEqual(steps.wants(set()), ["a"])
        self.assertIsNone(steps.take("b", set()))
        run = steps.take("a", set())
        self.assertEqual([c.number for c in run.todo], [1.0, 2.0])
        self.assertEqual(run.batch, 1)
        steps.record(run, [1.0, 2.0], [], {})
        self.assertEqual(steps.wants(set()), ["b"])
        steps.record(steps.take("b", set()), [3.0], [], {})
        self.assertEqual(steps.wants(set()), [])
        self.assertEqual(steps.results, {1.0: "ok", 2.0: "ok", 3.0: "ok"})

    def test_a_failure_moves_on_to_the_next_candidate(self):
        steps, _ = steps_for([match("A", 1, [1, 2]), match("B", 2, [1, 2])], [1, 2])
        steps.record(steps.take("a", set()), [1.0], [2.0], {2.0: "broken"})
        self.assertEqual(steps.wants(set()), ["b"])
        run = steps.take("b", set())
        self.assertEqual([c.number for c in run.todo], [2.0])
        self.assertTrue(run.patient)                       # B is the last source for ch 2

    def test_a_dead_chapter_stops_the_series_with_waiting_reasons(self):
        steps, reasons = steps_for([match("A", 1, [1, 2, 3])], [1, 2, 3])
        steps.record(steps.take("a", set()), [1.0], [2.0], {2.0: "broken"})
        with self.assertLogs("mangarr.downloader", "WARNING"):
            self.assertEqual(steps.wants(set()), [])
        self.assertEqual(steps.results, {1.0: "ok", 2.0: "failed"})
        self.assertIn("A: broken", reasons[2.0])
        self.assertIn("waiting for chapter 2", reasons[3.0])

    def test_a_chapter_no_copy_of_which_is_known_to_be_it_is_not_attempted(self):
        # a fractional chapter whose copies' pages are not counted this time (plan.uncounted: Suwayomi did not
        # answer) is left for the next pass, not failed; in order the later chapters wait for it
        for in_order, results in ((True, {1.0: "ok"}), (False, {1.0: "ok", 2.0: "ok"})):
            plan = plan_for([match("A", 1, [1, 1.5, 2])])
            plan.candidates[1.5], plan.uncounted = [], {1.5: ["A"]}
            reasons = {}
            steps = downloader.SeriesSteps(plan, {1.0, 1.5, 2.0}, in_order, "T", reasons)
            with self.assertLogs("mangarr.downloader", "WARNING"):
                runs = drive(steps, set(), lambda keys, alts: keys[0])
            self.assertEqual((steps.results, [n for _, nums in runs for n in nums]), (results, sorted(results)))
            self.assertEqual(reasons[1.5], downloader.uncounted_reason(1.5, ["A"]))
            self.assertEqual(reasons.get(2.0), downloader.uncounted_reason(1.5, ["A"]) if in_order else None)

    def test_take_is_none_when_the_chapter_was_ignored_meanwhile(self):
        steps, _ = steps_for([match("A", 1, [1]), match("B", 2, [2])], [1, 2])
        self.assertEqual(steps.wants(set()), ["a"])
        self.assertIsNone(steps.take("a", {1.0}))
        self.assertEqual(steps.wants({1.0}), ["b"])

    def test_batch_mode_keeps_the_call_order(self):
        # recorded from the downloader before SeriesSteps (the same plan and failures)
        a, b, c = match("A", 1, [1, 2, 3, 4, 5, 6]), match("B", 2, [1, 2, 3, 4, 5, 6]), match("C", 3, [3, 4, 5, 6, 7, 8])
        client = FakeClient(broken={1020, 1050, 2050, 3070, 1060, 2060})
        reasons = {}
        with tempfile.TemporaryDirectory() as tmp, mock.patch("mangarr.downloader.time.sleep", lambda s: None), \
             mock.patch("mangarr.config.DB_PATH", tmp + "/t.db"), mock.patch("mangarr.config.LOCK_PATH", tmp + "/lock"):
            res = downloader.download(client, plan_for([a, b, c]), only={float(n) for n in range(1, 10)},
                                      reasons=reasons, in_order=False)
        self.assertEqual(client.calls, ["start [1010, 1020, 1030, 1040]", "start [1050, 1060]", "start [3070, 3080]",
                                        "start [2020, 2050, 2060]", "start [3050, 3060]"])
        self.assertEqual(res, {**{float(n): "ok" for n in (1, 2, 3, 4, 5, 6, 8)}, 7.0: "failed", 9.0: "failed"})
        self.assertIn("no other source has this chapter", reasons[7.0])

    def test_batch_rounds_by_site(self):
        steps, _ = steps_for([match("A", 1, [1, 2]), match("B", 2, [3])], [1, 2, 3], in_order=False)
        self.assertEqual(steps.wants(set()), ["a", "b"])
        run = steps.take("b", set())                       # any site of the round, not only the first
        self.assertEqual([c.number for c in run.todo], [3.0])
        self.assertEqual(steps.wants(set()), ["a"])
        steps.record(run, [3.0], [], {})
        steps.record(steps.take("a", set()), [1.0, 2.0], [], {})
        self.assertEqual(steps.wants(set()), [])

    def test_stop_marks_the_chapters_not_reached(self):
        for in_order in (True, False):
            steps, reasons = steps_for([match("A", 1, [1, 2, 3])], [1, 2, 3], in_order)
            run = steps.take("a", set())
            steps.record(run, [1.0], [], {})
            steps.stop("not now")
            self.assertEqual(steps.wants(set()), [])
            self.assertEqual((reasons.get(2.0), reasons.get(3.0), reasons.get(1.0)), ("not now", "not now", None))

    def test_a_run_cut_short_goes_on_from_the_first_chapter_that_did_not_arrive(self):
        # a cancel ends a run with neither failures nor all of it: the series is not done
        steps, _ = steps_for([match("A", 1, [1, 2, 3])], [1, 2, 3])
        steps.record(steps.take("a", set()), [1.0], [], {})
        self.assertEqual(steps.wants(set()), ["a"])
        self.assertEqual([c.number for c in steps.take("a", set()).todo], [2.0, 3.0])

    def test_a_page_by_page_source_takes_one_chapter_at_a_time(self):
        m = match("Comick (Unoriginal) (EN)", 1, [1, 2, 3])
        m.source = Source("1", m.source.name, "en", page_warm=True)
        steps, _ = steps_for([m], [1, 2, 3], in_order=False)
        run = steps.take("comick (unoriginal)", set())
        self.assertEqual((run.batch, len(run.todo)), (1, 3))

    def test_in_order_alternatives_are_other_sites_of_no_worse_tier(self):
        a, b, d = match("A (EN)", 1, [1, 2, 3]), match("B", 2, [1, 2]), match("A (ALL)", 4, [1, 2, 3])
        c = match("C", 3, [1, 2])
        c.source = Source("3", "C", "en", page_warm=True)
        steps, reasons = steps_for([a, d, b, c], [1, 2, 3])
        self.assertEqual(steps.wants(set()), ["a"])
        self.assertEqual(steps.alternatives(set()), ["b"])   # not A (ALL): the same site; not C: page by page
        self.assertIsNone(steps.take("c", set()))
        run = steps.take("b", set())                        # A is busy: B lists ch 1 and 2 as well
        self.assertEqual((run.match.source.name, [x.number for x in run.todo], run.patient), ("B", [1.0, 2.0], False))
        steps.record(run, [1.0], [2.0], {2.0: "broken"})
        self.assertEqual((steps.wants(set()), steps.alternatives(set())), (["a"], []))
        run = steps.take("a", set())
        self.assertEqual((run.match.source.name, [x.number for x in run.todo]), ("A (EN)", [2.0, 3.0]))
        steps.record(run, [2.0, 3.0], [], {})
        self.assertEqual(steps.wants(set()), [])
        self.assertEqual(steps.results, {1.0: "ok", 2.0: "ok", 3.0: "ok"})

    def test_every_source_is_tried_before_a_chapter_fails(self):
        for in_order in (True, False):
            with self.subTest(in_order=in_order):
                steps, reasons = steps_for([match("A", 1, [1]), match("B", 2, [1]), match("C", 3, [1])], [1],
                                           in_order)
                seen = []
                while keys := steps.wants(set()):
                    alts = steps.alternatives(set())
                    run = steps.take(alts[0] if alts else keys[0], set())   # A stays busy while there is another
                    seen.append(run.match.source.name)
                    steps.record(run, [], [1.0], {1.0: "broken"})
                self.assertEqual(seen, ["B", "C", "A"])
                self.assertEqual(steps.results, {1.0: "failed"})
                self.assertIn("B: broken; C: broken; A: broken", reasons[1.0])

    def test_out_of_order_a_free_site_takes_chapters_of_a_busy_one(self):
        a, b = match("A", 1, [1, 2, 3, 4]), match("B", 2, [3, 4, 5, 6])
        steps, _ = steps_for([a, b], [1, 2, 3, 4, 5, 6], in_order=False)
        self.assertEqual((steps.wants(set()), steps.alternatives(set())), (["a", "b"], []))
        run_b = steps.take("b", set())
        self.assertEqual([x.number for x in run_b.todo], [5.0, 6.0])
        self.assertEqual((steps.wants(set()), steps.alternatives(set())), (["a"], ["b"]))
        steps.record(run_b, [5.0, 6.0], [], {})
        run = steps.take("b", set())                        # A is busy: B takes the chapters it lists too
        self.assertEqual(([x.number for x in run.todo], run.batch, run.patient), ([3.0, 4.0], 4, False))
        steps.record(run, [3.0], [4.0], {4.0: "broken"})    # 4 still gets its turn on A
        run = steps.take("a", set())
        self.assertEqual([x.number for x in run.todo], [1.0, 2.0])
        steps.record(run, [1.0, 2.0], [], {})
        run = steps.take("a", set())
        self.assertEqual([x.number for x in run.todo], [4.0])
        steps.record(run, [4.0], [], {})
        self.assertEqual(steps.wants(set()), [])
        self.assertEqual(set(steps.results.values()), {"ok"})

    def test_a_switch_never_fails_a_chapter_another_source_has(self):
        # a run taken from a free site is often one chapter: one broken chapter there must not keep
        # that source from the chapters it has, nor fail one the busy source was never asked for
        a, b = match("A", 1, [1, 2]), match("B", 2, [1, 2])
        steps, reasons = steps_for([a, b], [1, 2])
        runs = drive(steps, {("A", 2.0), ("B", 1.0)}, switch_once())
        self.assertEqual(runs, [("B", [1.0, 2.0]), ("A", [1.0, 2.0]), ("B", [2.0])])
        self.assertEqual(steps.results, {1.0: "ok", 2.0: "ok"})
        b, c = match("B", 2, [1, 2]), match("C", 3, [1])
        steps, reasons = steps_for([b, c], [1, 2], in_order=False)
        runs = drive(steps, {("B", 2.0), ("C", 1.0)}, switch_once())
        self.assertEqual(runs, [("C", [1.0]), ("B", [2.0]), ("B", [1.0])])
        self.assertEqual(steps.results, {1.0: "ok", 2.0: "failed"})
        self.assertEqual(reasons[2.0], "B: broken (no other source has this chapter)")

    def test_a_source_that_delivered_nothing_is_asked_last(self):
        a, b = match("A", 1, [1, 2]), match("B", 2, [1, 2])
        steps, reasons = steps_for([a, b], [1, 2])
        with self.assertLogs("mangarr.downloader", "WARNING") as cm:
            runs = drive(steps, {("A", 1.0), ("B", 2.0)}, lambda keys, alts: keys[0])
        self.assertEqual(runs, [("A", [1.0, 2.0]), ("B", [1.0, 2.0]), ("A", [2.0])])   # A: ch 1 was broken
        self.assertEqual(steps.results, {1.0: "ok", 2.0: "ok"})
        self.assertIn("A delivered nothing this run; asking the other sources first from now on", cm.output[0])
        steps, _ = steps_for([match("A", 1, [1, 2, 3]), match("B", 2, [1, 2, 3]), match("C", 3, [2, 3])], [1, 2, 3])
        run = steps.take("a", set())
        steps.record(run, [], [1.0], {1.0: "broken"})               # A is dead
        self.assertEqual((steps.wants(set()), steps.alternatives(set())), (["b"], []))
        steps.record(steps.take("b", set()), [1.0], [2.0], {2.0: "broken"})
        self.assertEqual((steps.wants(set()), steps.alternatives(set())), (["c"], []))   # never A instead
        run = steps.take("c", set())
        self.assertTrue(run.patient)                                # only A is left besides it
        self.assertEqual(steps.attempts, [("A", 1.0, "failed"), ("B", 1.0, "ok"), ("B", 2.0, "failed")])

    def test_every_chapter_gets_every_source_whatever_is_switched(self):
        # random series, broken copies and switching choices: a chapter fails only once every source
        # that lists it was asked, in order the series stops only at such a chapter, and a switch
        # never goes to a worse tier or to a source that delivered nothing
        rnd = random.Random(7)
        for case in range(400):
            names = ["A", "B", "C", "D"][:rnd.randint(1, 4)]
            nums = [float(n) for n in range(1, rnd.randint(2, 6))]
            ms = []
            for k, name in enumerate(names, 1):
                listed = [n for n in nums if rnd.random() < 0.7] or [rnd.choice(nums)]
                ms.append(match(name, k, listed))
                ms[-1].source = Source(str(k), name, "en", throttled=rnd.random() < 0.25)
            wanted = sorted({n for m in ms for n in m.numbers})
            broken = {(m.source.name, n) for m in ms for n in m.numbers if rnd.random() < 0.35}
            in_order = case % 2 == 0
            steps, reasons = steps_for(ms, wanted, in_order)
            with self.subTest(case=case):
                with mock.patch.object(downloader.log, "warning"):
                    drive(steps, broken, lambda keys, alts: rnd.choice(keys + alts), tiers=self)
                good = {n: any((m.source.name, n) not in broken for m in ms if n in m.numbers) for n in wanted}
                expect = {}
                for n in wanted:
                    expect[n] = "ok" if good[n] else "failed"
                    if in_order and not good[n]:
                        break
                self.assertEqual(steps.results, expect, (ms, broken))
                for n, r in expect.items():
                    if r == "failed":
                        for m in ms:
                            if n in m.numbers:
                                self.assertIn(f"{m.source.name}: broken", reasons[n])

    def test_a_worse_tier_is_never_an_alternative(self):
        for flag in ("throttled", "page_warm"):
            for in_order in (True, False):
                a, c = match("A", 1, [1]), match("C", 2, [1])
                c.source = Source("2", "C", "en", **{flag: True})
                steps, _ = steps_for([a, c], [1], in_order)
                self.assertEqual((steps.wants(set()), steps.alternatives(set())), (["a"], []), (flag, in_order))
        a, c = match("A", 1, [1]), match("C", 2, [1])
        a.source, c.source = Source("1", "A", "en", throttled=True), Source("2", "C", "en", throttled=True)
        self.assertEqual(steps_for([a, c], [1])[0].alternatives(set()), ["c"])      # the same tier is fine

    def test_en_and_all_variants_are_one_site(self):
        self.assertEqual(downloader.lanes_key("Comick (Unoriginal) (EN)"), downloader.lanes_key("Comick (Unoriginal) (ALL)"))
        self.assertEqual(downloader.lanes_key(" Weeb Central "), "weeb central")
        self.assertNotEqual(downloader.lanes_key("Comick (Unoriginal) (EN)"), downloader.lanes_key("Comick (EN)"))


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def advance(self, secs):
        self.t += secs


class QueueScript:
    """downloadStatus as a function of the fake time: state(id, t) -> (state, tries, progress) or None (gone)."""

    def __init__(self, clock, state):
        self.clock, self.state = clock, state

    def queue(self):
        out = []
        for cid in (1, 2, 3, 4):
            st = self.state(cid, self.clock.t)
            if st:
                out.append({"id": cid, "state": st[0], "tries": st[1], "progress": st[2]})
        return out


class WaitTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for p in (mock.patch("mangarr.downloader.time.monotonic", self.clock.now),
                  mock.patch("mangarr.downloader.time.sleep", self.clock.advance)):
            p.start()
            self.addCleanup(p.stop)

    def wait(self, state, ids=(1,)):
        return downloader._wait(QueueScript(self.clock, state), list(ids), lambda: False, every=2)

    def test_not_started_is_no_stall_until_the_cap(self):
        with self.assertLogs("mangarr.downloader", "WARNING") as cm:
            self.assertEqual(self.wait(lambda cid, t: ("QUEUED", 0, 0.0)), "unstarted")
        self.assertGreater(self.clock.t, downloader.QUEUED_CAP_SECS)
        self.assertLess(self.clock.t, downloader.QUEUED_CAP_SECS + 10)
        self.assertIn("not started by Suwayomi", "\n".join(cm.output))

    def test_downloading_without_progress_still_times_out(self):
        with self.assertLogs("mangarr.downloader", "WARNING"):
            self.assertEqual(self.wait(lambda cid, t: ("DOWNLOADING", 0, 0.1)), "timeout")
        self.assertLess(self.clock.t, downloader.STALL_SECS + 10)

    def test_the_stall_clock_starts_when_it_starts(self):
        started = downloader.STALL_SECS + 500             # queued longer than a stall, then stuck downloading
        with self.assertLogs("mangarr.downloader", "WARNING"):
            out = self.wait(lambda cid, t: ("QUEUED", 0, 0.0) if t < started else ("DOWNLOADING", 0, 0.2))
        self.assertEqual(out, "timeout")
        self.assertGreater(self.clock.t, started + downloader.STALL_SECS)

    def test_one_downloading_and_three_queued_is_judged_as_before(self):
        def state(cid, t):
            return ("DOWNLOADING", 0, 0.3) if cid == 1 else ("QUEUED", 0, 0.0)
        with self.assertLogs("mangarr.downloader", "WARNING"):
            self.assertEqual(self.wait(state, ids=(1, 2, 3, 4)), "timeout")
        self.assertLess(self.clock.t, downloader.STALL_SECS + 10)

    def test_started_after_a_while_finishes(self):
        self.assertEqual(self.wait(lambda cid, t: ("QUEUED", 0, 0.0) if t < 900 else None), "done")


class StuckClient:
    """Every enqueued chapter sits in Suwayomi's queue in `state` and never arrives."""

    def __init__(self, state=("DOWNLOADING", 0, 0.1)):
        self.state, self.queued, self.enqueued, self.dequeued = state, [], [], []

    def enqueue(self, ids):
        self.queued += ids
        self.enqueued.append(list(ids))

    def dequeue(self, ids, timeout=30):
        self.dequeued.append(list(ids))
        self.queued = [c for c in self.queued if c not in ids]

    def start(self): pass
    def queue(self):
        return [{"id": c, "state": self.state[0], "tries": self.state[1], "progress": self.state[2]} for c in self.queued]
    def downloaded_ids(self, manga_id): return set()


class RateLimitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.paused = []

        def pause(secs, should_cancel=None, step=1.0):
            self.paused.append(secs)
            self.clock.advance(secs)
            return False
        for p in (mock.patch("mangarr.downloader.time.monotonic", self.clock.now),
                  mock.patch("mangarr.downloader.time.sleep", self.clock.advance),
                  mock.patch.object(downloader.limits, "pause", pause),
                  mock.patch("mangarr.config.DB_PATH", self.tmp.name + "/t.db"),
                  mock.patch("mangarr.config.LOCK_PATH", self.tmp.name + "/lock")):
            p.start()
            self.addCleanup(p.stop)

    def run_source(self, client, patient, memo, numbers=(1, 2)):
        a = match("A", 1, numbers)
        with self.assertLogs("mangarr.downloader", "WARNING") as cm:
            out = downloader._download_source(client, 1, a.chapters, 1, "T", "A", patient, lambda: False,
                                              lambda m: None, memo)
        return out, cm.output

    def test_no_progress_backs_off_then_gives_up(self):
        memo = downloader.RunMemo()
        (ok, failed, why), _ = self.run_source(StuckClient(), True, memo)
        self.assertEqual([s for s in self.paused if s >= 60], [60, 120, 240, 300])
        self.assertEqual((ok, failed), ([], [1.0, 2.0]))
        self.assertEqual(why[2.0], "download made no progress, gave up after 300s of backoff")
        self.assertEqual((memo.throttle, memo.gave_up, memo.unstarted), ({"A"}, {"A"}, []))

    def test_with_a_fallback_the_backoff_stops_at_60(self):
        memo = downloader.RunMemo(shared=downloader.PassShared())
        (ok, failed, why), _ = self.run_source(StuckClient(), False, memo)
        self.assertEqual([s for s in self.paused if s >= 60], [60])
        self.assertEqual(failed, [1.0, 2.0])
        self.assertTrue(memo.shared.seen("A"))             # the rest of the pass knows too

    def test_not_started_stops_the_series_without_blaming_the_source(self):
        client = StuckClient(("QUEUED", 0, 0.0))
        throttled, reasons = set(), {}
        a = match("A", 1, [1, 2, 3])
        with self.assertLogs("mangarr.downloader", "WARNING") as cm:
            res = downloader.download(client, plan_for([a]), only={1.0, 2.0, 3.0}, reasons=reasons,
                                      throttled=throttled, in_order=True)
        self.assertEqual(res, {})
        self.assertEqual(throttled, set())                 # no verdict on the source
        self.assertEqual(client.enqueued, [[1010]])        # nothing else was queued behind it
        self.assertEqual(client.dequeued, [[1010]])        # and ours was taken back out
        self.assertEqual({reasons[n] for n in (1.0, 2.0, 3.0)}, {downloader.UNSTARTED_REASON})
        self.assertIn("it and the later chapters wait for the next pass", "\n".join(cm.output))

    def test_not_started_goes_on_to_the_next_source(self):
        # Suwayomi downloads one chapter per source at a time: a queue busy for A need not be for B
        for in_order in (False, True):
            with self.subTest(in_order=in_order):
                client, throttled, reasons = BusyForOneSource(), set(), {}
                plan = plan_for([match("A", 1, [1, 2]), match("B", 2, [1, 2])])
                with self.assertLogs("mangarr.downloader", "WARNING"):
                    res = downloader.download(client, plan, only={1.0, 2.0}, reasons=reasons, throttled=throttled,
                                              in_order=in_order)
                self.assertEqual(res, {1.0: "ok", 2.0: "ok"})
                self.assertEqual(throttled, set())
                self.assertEqual(client.enqueued[0], [1010] if in_order else [1010, 1020])
                self.assertEqual(client.dequeued, [client.enqueued[0]])     # taken back out of A's queue
                self.assertEqual({c // 1000 for ids in client.enqueued[1:] for c in ids}, {2})  # A not asked again

    def test_a_chapter_only_the_busy_source_has_waits_for_the_next_pass(self):
        client, reasons = BusyForOneSource(), {}
        plan = plan_for([match("A", 1, [1, 2, 3]), match("B", 2, [1, 2])])
        with self.assertLogs("mangarr.downloader", "WARNING"):
            res = downloader.download(client, plan, only={1.0, 2.0, 3.0}, reasons=reasons, in_order=False)
        self.assertEqual(res, {1.0: "ok", 2.0: "ok"})                  # 3 is not failed: it was never attempted
        self.assertEqual(reasons[3.0], downloader.UNSTARTED_REASON)
        self.assertEqual(client.enqueued, [[1010, 1020, 1030], [2010, 2020]])

    def test_the_busy_source_is_left_alone_for_a_while_by_the_whole_pass(self):
        shared, client = downloader.PassShared(), BusyForOneSource()
        a = match("A", 1, [1, 2, 3])

        def run(chapter):
            memo = downloader.RunMemo(shared=shared)                   # each series of the pass has its own
            downloader._download_source(client, 1, [chapter], 1, "T", "A", True, lambda: False, lambda m: None, memo)
            return memo.unstarted, memo.unqueued
        with self.assertLogs("mangarr.downloader", "WARNING"):
            self.assertEqual(run(a.chapters[0]), ([1.0], []))          # queued, not started
        with self.assertLogs("mangarr.downloader", "INFO") as cm:
            self.assertEqual(run(a.chapters[1]), ([], [2.0]))          # not even queued
        self.assertIn("not queueing ch 2 there now", "\n".join(cm.output))
        self.assertEqual(client.enqueued, [[1010]])
        self.clock.advance(downloader.QUEUED_BUSY_SECS)
        with self.assertLogs("mangarr.downloader", "WARNING"):
            self.assertEqual(run(a.chapters[2]), ([3.0], []))          # tried again after a while
        self.assertEqual(client.enqueued, [[1010], [1030]])

    def test_a_chapter_never_queued_says_so(self):
        # the second series of a pass does not queue on the busy source at all: its reason must not
        # claim Suwayomi was given the chapter and did not start it
        shared, client = downloader.PassShared(), BusyForOneSource()
        shared.note_unstarted("A", self.clock.now())
        for in_order in (True, False):
            with self.subTest(in_order=in_order):
                reasons = {}
                plan = plan_for([match("A", 1, [1, 2, 3]), match("B", 2, [1, 2])])
                steps = downloader.SeriesSteps(plan, {1.0, 2.0, 3.0}, in_order, "T", reasons)
                memo = downloader.RunMemo(shared=shared)
                run = steps.take("a", set())
                with self.assertLogs("mangarr.downloader", "INFO"):
                    ok, failed, why = downloader._download_source(client, 1, run.todo, run.batch, "T", "A",
                                                                  run.patient, lambda: False, lambda m: None, memo)
                self.assertEqual((ok, failed, memo.unstarted, memo.unqueued), ([], [], [], [1.0, 2.0, 3.0]))
                steps.record(run, ok, failed, why)
                steps.not_started(run, memo.unqueued, queued=False)
                while keys := steps.wants(set()):
                    run = steps.take(keys[0], set())
                    self.assertEqual(run.match.source.name, "B")
                    steps.record(run, [c.number for c in run.todo], [], {})
                self.assertEqual(steps.results, {1.0: "ok", 2.0: "ok"})
                self.assertEqual(reasons[3.0], "not attempted: A is busy with other downloads; tried again next pass")
                self.assertEqual(downloader.busy_source(reasons[3.0]), "A")
                self.assertEqual(steps.tried[3.0], ["A: not queued: its download queue was busy with other downloads"])
        self.assertEqual(client.enqueued, [])
        self.assertIsNone(downloader.busy_source(downloader.UNSTARTED_REASON))

    def test_download_one_says_it_was_not_started(self):
        a = match("A", 1, [4])
        with self.assertLogs("mangarr.downloader", "WARNING"):
            ok, failed, why = downloader.download_one(StuckClient(("QUEUED", 0, 0.0)), 1, a.chapters[0], "T", "A")
        self.assertEqual((ok, failed, why), (False, [4.0], {4.0: downloader.UNSTARTED_REASON}))


class BusyForOneSource(StuckClient):
    """Suwayomi never gets to manga 1's chapters (its source is busy with
    someone else's download); every other chapter arrives at once."""

    def __init__(self):
        super().__init__(("QUEUED", 0, 0.0))
        self.have: set[int] = set()

    def start(self):
        for c in [c for c in self.queued if c // 1000 != 1]:
            self.have.add(c)
            self.queued.remove(c)

    def downloaded_ids(self, manga_id):
        return {c for c in self.have if c // 1000 == manga_id}


class LockSplitTest(unittest.TestCase):
    def test_taken_in_one_thread_given_back_in_another(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/lock"
            box = {}
            t = threading.Thread(target=lambda: box.setdefault("fd", downloader.acquire_download_lock(path)))
            t.start()
            t.join(10)
            with open(path) as f:
                self.assertIn(f"pid {os.getpid()}", f.read())
            other = os.open(path, os.O_RDWR)
            try:
                with self.assertRaises(OSError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                t = threading.Thread(target=downloader.release_download_lock, args=(box["fd"],))
                t.start()
                t.join(10)
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)       # free now
            finally:
                os.close(other)

    def test_a_cancelled_wait_leaves_no_descriptor_open(self):
        with tempfile.TemporaryDirectory() as tmp, downloader.download_lock(tmp + "/lock"):
            before = len(os.listdir("/proc/self/fd"))
            with mock.patch("mangarr.downloader.time.sleep", lambda s: None), self.assertRaises(downloader.Cancelled):
                downloader.acquire_download_lock(tmp + "/lock", should_cancel=lambda: True)
            self.assertEqual(len(os.listdir("/proc/self/fd")), before)


if __name__ == "__main__":
    unittest.main()

"""Download lanes (lanes.py): outages counted per episode, the lane count
against Suwayomi's own limit, and the pool itself against a fake Suwayomi:
one series per site, the lane cap, pass order, pacing, a crashing step or
lane, the hand-over limit, deleted series, what pending_for sees, and the
download lock held for the whole pass. Real threads, short real waits;
nothing reaches a real Suwayomi."""
import os
import random
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_suwayomi import PassBase, add_series, entry, resolver_for  # noqa: E402

from mangarr import backup, core, db, downloader, jobs, lanes, settings  # noqa: E402

X, Y, Z = "Site X (EN)", "Site Y", "Site Z"


class OutagesTest(unittest.TestCase):
    def setUp(self):
        self.t = 100.0
        self.o = lanes.Outages(clock=lambda: self.t)

    def test_first_report_holds(self):
        self.assertEqual(self.o.report(RuntimeError("down")), "hold")
        self.assertEqual(self.o.hold_left(), lanes.HOLD_SECS)
        self.t += 10
        self.assertEqual(self.o.hold_left(), lanes.HOLD_SECS - 10)

    def test_reports_during_the_hold_are_one_episode(self):
        for _ in range(5):                                  # every lane saw the same outage
            self.assertEqual(self.o.report(RuntimeError("down")), "hold")
        self.assertFalse(self.o.stopped)

    def test_a_second_episode_stops(self):
        self.o.report(RuntimeError("down"))
        self.t += lanes.HOLD_SECS
        self.assertEqual(self.o.hold_left(), 0.0)
        self.o.served()
        self.assertEqual(self.o.report(RuntimeError("still down")), "stop")
        self.assertTrue(self.o.stopped)
        self.assertEqual(self.o.why, "still down")
        self.assertEqual(self.o.report(RuntimeError("x")), "stop")

    def test_ok_resets_only_outside_a_hold(self):
        self.o.report(RuntimeError("down"))
        self.o.ok()                                         # a lane that finished just before it: no reset
        self.o.served()
        self.assertEqual(self.o.report(RuntimeError("down")), "stop")
        o = lanes.Outages(clock=lambda: self.t)
        o.report(RuntimeError("down"))
        o.served()
        o.ok()                                              # Suwayomi answered after the hold
        self.assertEqual(o.report(RuntimeError("down again")), "hold")


class _Cap:
    def __init__(self, cap):
        self.cap, self.asked = cap, 0

    def max_sources_in_parallel(self):
        self.asked += 1
        return self.cap

    def gq(self, *a, **k):
        raise AssertionError("nothing may be written to Suwayomi")

    set_max_sources_in_parallel = gq


class EffectiveLanesTest(unittest.TestCase):
    def lanes_for(self, setting, cap):
        c = _Cap(cap)
        with mock.patch.object(lanes.limits, "setting", lambda k: setting):
            return lanes.effective_lanes(c), c.asked

    def test_one_lane_asks_nothing(self):
        self.assertEqual(self.lanes_for(1, 5), ((1, None), 0))

    def test_suwayomis_cap_wins(self):
        with self.assertLogs("mangarr.lanes", "WARNING") as cm:
            self.assertEqual(self.lanes_for(3, 1), ((1, 1), 1))
        self.assertEqual(len(cm.output), 1)
        self.assertIn("using 1 download lane(s) instead of 3", cm.output[0])

    def test_a_larger_cap_leaves_the_setting(self):
        with self.assertNoLogs("mangarr.lanes", "WARNING"):
            self.assertEqual(self.lanes_for(3, 6), ((3, 6), 1))

    def test_unreadable_cap_means_one_lane(self):
        with self.assertLogs("mangarr.lanes", "WARNING") as cm:
            self.assertEqual(self.lanes_for(4, None), ((1, None), 1))
        self.assertEqual(len(cm.output), 1)
        self.assertIn("one source at a time", cm.output[0])


class PoolBase(PassBase):
    """Series tracked on the fake, handed to a LanePool the way _run_pass does."""

    def setUp(self):
        super().setUp()
        self.job = jobs.Job(1, "refresh-all", "all")

    def seed(self, fake, plans: dict) -> list[tuple]:
        """[(series id, title, plan, chapters due)] in plans' order."""
        out = []
        with mock.patch.object(core, "resolve", resolver_for(fake, plans)), db.connect() as con:
            for title in plans:
                sid = add_series(con, fake, title)
                o = core.Outcome(sid, core.resolve(fake, core.db.series_to_model(db.get_series(con, sid))))
                out.append((sid, title, o.plan, core.downloads_due(con, o)))
        return out

    def pool(self, fake, n_lanes, job=None, **kw) -> lanes.LanePool:
        self.job = job or self.job
        self.errors = []
        pool = lanes.LanePool(fake, self.job, n_lanes, "test", lanes.Outages(),
                              on_error=lambda sid, e: self.errors.append((sid, e)), **kw)
        self.addCleanup(pool.shutdown)
        return pool

    def items(self, series) -> list[dict]:
        self.job.items = [{"series_id": sid, "title": t, "state": "queued", "result": ""} for sid, t, _, _ in series]
        return self.job.items

    def run_all(self, fake, plans, n_lanes=3, order=None, before_close=None) -> list[tuple]:
        series = self.seed(fake, plans)
        items = self.items(series)
        pool = self.pool(fake, n_lanes)
        pool.start()
        for i in order or range(len(series)):
            sid, title, plan, due = series[i]
            pool.submit(i + 1, sid, title, items[i], plan, due)
        if before_close:
            before_close(pool, series)
        pool.close()
        pool.join()
        pool.shutdown()
        self.pool_ = pool
        return series

    def statuses(self, sid) -> set:
        return {st for st, _ in self.status(sid).values()}

    def watch(self, item: dict) -> list[str]:
        """Every text the item shows until the test ends (sampled)."""
        seen, stop = [], threading.Event()

        def sample():
            while not stop.wait(0.001):
                t = item.get("result")
                if not seen or seen[-1] != t:
                    seen.append(t)
        th = threading.Thread(target=sample, daemon=True)
        th.start()
        self.addCleanup(th.join, 2)
        self.addCleanup(stop.set)
        return seen


class PoolTest(PoolBase):
    def test_one_series_per_site_at_a_time(self):
        fake = self.fake(lanes=3)
        sites = ["Site A", "Site B", "Site C", "Site D"]
        plans = {f"S{k}": [entry(fake, sites[k % 4], k, f"S{k}", [1, 2])] for k in range(1, 13)}
        series = self.run_all(fake, plans)
        self.assertEqual(fake.violations, [])
        for sid, *_ in series:
            self.assertEqual(self.statuses(sid), {"have"})
        self.assertEqual(self.pool_.counts(), (24, 24, 0))
        self.assertEqual({i["state"] for i in self.job.items}, {"done"})
        self.assertEqual((self.job.lanes, self.job.active_series_ids), ([], frozenset()))

    def test_the_activity_page_sees_every_lane(self):
        # job.lanes lists every lane of the running pass, idle ones with source None (Activity: "1 of 3 busy")
        fake = self.fake(lanes=3)
        plans = {"S1": [entry(fake, "Site A", 1, "S1", [1, 2, 3, 4])]}
        seen = []

        def sample(pool, series):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not seen:
                if any(ln["source"] for ln in self.job.lanes):
                    seen.append(self.job.lanes)
                threading.Event().wait(0.001)
        series = self.run_all(fake, plans, before_close=sample)
        self.assertTrue(seen, "no lane was ever seen busy")
        self.assertEqual([ln["lane"] for ln in seen[0]], [1, 2, 3])
        busy = [ln for ln in seen[0] if ln["source"]]
        self.assertEqual([(ln["source"], ln["series_id"], ln["title"]) for ln in busy], [("Site A", series[0][0], "S1")])
        self.assertIsInstance(busy[0]["since"], float)
        self.assertEqual([ln for ln in seen[0] if not ln["source"]],
                         [{"lane": k, "source": None, "series_id": None, "title": None, "text": "", "since": None}
                          for k in (1, 2, 3) if k != busy[0]["lane"]])
        self.assertEqual(self.job.lanes, [])                                 # the pass ended

    def test_the_lane_count_caps_the_sites_at_once(self):
        fake = self.fake(lanes=2)
        plans = {f"S{k}": [entry(fake, f"Site {k}", k, f"S{k}", [1, 2, 3])] for k in range(1, 6)}
        series = self.run_all(fake, plans, n_lanes=2)
        self.assertEqual(fake.violations, [])
        for sid, *_ in series:
            self.assertEqual(self.statuses(sid), {"have"})

    def test_an_in_order_series_waits_for_its_site_and_keeps_its_order(self):
        fake = self.fake()
        fake.hold_until_other(X, "nowhere", timeout=0.3)    # A keeps Site X busy a while
        plans = {"A": [entry(fake, X, 1, "A", [1])],
                 "B": [entry(fake, Y, 2, "B", [1, 3]), entry(fake, X, 3, "B", [2])]}
        seed = self.seed(fake, plans)
        items = self.items(seed)
        texts = self.watch(items[1])
        pool = self.pool(fake, 3)
        pool.start()
        for i, (sid, title, plan, due) in enumerate(seed):
            pool.submit(i + 1, sid, title, items[i], plan, due)
        pool.close()
        pool.join()
        self.assertIn(f"waiting for {X}: busy with A", texts)
        b = [e[4] % 1000 / 10 for e in fake.kinds("enqueue") if e[3] in (2, 3)]
        self.assertEqual(b, [1.0, 2.0, 3.0])
        self.assertEqual(self.statuses(seed[1][0]), {"have"})

    def test_an_earlier_series_goes_first(self):
        fake = self.fake()
        fake.hold_until_other(X, "nowhere", timeout=0.3)
        plans = {f"S{k}": [entry(fake, X, k, f"S{k}", [1])] for k in range(1, 4)}
        self.run_all(fake, plans, order=[0, 2, 1])        # handed over 1, 3, 2: pass order still wins
        self.assertEqual([e[3] for e in fake.kinds("enqueue")], [1, 2, 3])

    def test_a_paced_site_rests_between_series(self):
        with db.connect() as con:
            settings.set_many(con, {"throttled_delay_seconds": 0.3})
        settings._cache.clear()
        fake = self.fake()
        plans = {"A": [entry(fake, X, 1, "A", [1], throttled=True)],
                 "B": [entry(fake, X, 2, "B", [1], throttled=True)]}
        seed = self.seed(fake, plans)
        items = self.items(seed)
        texts = self.watch(items[1])
        pool = self.pool(fake, 3)
        pool.start()
        for i, (sid, title, plan, due) in enumerate(seed):
            pool.submit(i + 1, sid, title, items[i], plan, due)
        pool.close()
        pool.join()
        a_done = fake.kinds("finish")[0][0]
        b_start = [e[0] for e in fake.kinds("enqueue") if e[3] == 2][0]
        self.assertGreaterEqual(b_start - a_done, 0.25)
        self.assertTrue(any(t.startswith(f"waiting for {X}: paced, next chapter in") for t in texts if t), texts)

    def test_a_crashing_step_releases_its_site(self):
        fake = self.fake()
        plans = {"A": [entry(fake, X, 1, "A", [1])], "B": [entry(fake, X, 2, "B", [1, 2])]}
        real = downloader._download_source

        def crash_for_a(client, manga_id, *a, **k):
            if manga_id == 1:
                raise KeyError("boom")
            return real(client, manga_id, *a, **k)
        with mock.patch.object(downloader, "_download_source", crash_for_a), \
             self.assertLogs("mangarr.lanes", "ERROR") as cm:
            series = self.run_all(fake, plans)
        self.assertIn("A: a download step failed", "\n".join(cm.output))
        self.assertEqual(self.job.items[0]["state"], "error")
        self.assertIn("KeyError", self.job.items[0]["result"])
        self.assertEqual([sid for sid, _ in self.errors], [series[0][0]])
        self.assertEqual(self.job.items[1]["state"], "done")
        self.assertEqual(self.statuses(series[1][0]), {"have"})
        self.assertEqual(self.pool_.counts(), (2, 2, 1))

    def test_every_lane_dying_does_not_hang_the_pass(self):
        fake = self.fake()
        plans = {"A": [entry(fake, X, 1, "A", [1])], "B": [entry(fake, Y, 2, "B", [1])],
                 "C": [entry(fake, Z, 3, "C", [1])]}
        seed = self.seed(fake, plans)
        items = self.items(seed)
        pool = self.pool(fake, 2)

        def pick():                                         # both lanes die once A and B wait for them
            if len(pool._waiting) < 2:
                return None, None, 0.0
            raise RuntimeError("bug")
        with mock.patch.object(pool, "_pick", pick), self.assertLogs("mangarr.lanes", "ERROR") as cm:
            pool.start()
            for i, (sid, title, plan, due) in enumerate(seed[:2]):
                pool.submit(i + 1, sid, title, items[i], plan, due)
            for t in pool._threads:
                t.join(2)
            with self.assertRaises(lanes.PoolStopped):     # no lane left: the resolve step stops too
                pool.submit(3, *seed[2][:2], items[2], *seed[2][2:])
            pool.close()
            t0 = time.perf_counter()
            pool.join()
            self.assertLess(time.perf_counter() - t0, 2)
            pool.shutdown()
        self.assertIn("download lane 1 stopped unexpectedly", "\n".join(cm.output))
        self.assertEqual(pool.stop_why, lanes.LANE_DIED)
        self.assertEqual([(i["state"], i["result"]) for i in items[:2]], [("error", lanes.LANE_DIED)] * 2)
        self.assertEqual(pool.counts(), (0, 0, 2))
        self.assertEqual(fake.kinds("enqueue"), [])
        self.assertTrue(self.lock_free())

    def test_one_dead_lane_leaves_the_work_to_the_others(self):
        fake = self.fake(lanes=2)
        plans = {f"S{k}": [entry(fake, f"Site {k}", k, f"S{k}", [1, 2, 3])] for k in range(1, 7)}
        real, died = lanes.LanePool._pick, []

        def pick(pool):
            if threading.current_thread().name == "mangarr-lane-1" and not died:
                died.append(1)
                raise RuntimeError("bug")
            return real(pool)
        with mock.patch.object(lanes.LanePool, "_pick", pick), self.assertLogs("mangarr.lanes", "ERROR") as cm:
            series = self.run_all(fake, plans, n_lanes=2)
        self.assertIn("download lane 1 stopped unexpectedly", "\n".join(cm.output))
        for sid, *_ in series:
            self.assertEqual(self.statuses(sid), {"have"})
        self.assertEqual({i["state"] for i in self.job.items}, {"done"})
        self.assertEqual(self.pool_.counts(), (18, 18, 0))
        self.assertEqual(fake.violations, [])

    def test_a_lane_dying_mid_step_gives_its_site_back(self):
        fake = self.fake()
        plans = {"A": [entry(fake, X, 1, "A", [1])], "B": [entry(fake, X, 2, "B", [1])],
                 "C": [entry(fake, Y, 3, "C", [1])]}
        real, died = lanes.LanePool._release, []

        def release(pool, lane, task, *a):
            if task.title == "A" and not died:
                died.append(lane)
                raise RuntimeError("bug")
            return real(pool, lane, task, *a)
        with mock.patch.object(lanes.LanePool, "_release", release), self.assertLogs("mangarr.lanes", "ERROR"):
            series = self.run_all(fake, plans)
        items = self.job.items
        self.assertEqual((items[0]["state"], items[0]["result"]), ("error", f"RuntimeError: {lanes.LANE_DIED}"))
        self.assertEqual(self.statuses(series[0][0]), {"have"})         # what it downloaded is still written
        self.assertEqual([sid for sid, _ in self.errors], [series[0][0]])
        self.assertEqual([i["state"] for i in items[1:]], ["done", "done"])
        self.assertEqual(self.statuses(series[1][0]), {"have"})         # B still got Site X
        self.assertEqual(self.pool_.counts(), (3, 3, 1))

    def test_waiting_texts_follow_the_sites(self):
        # C waits for Site X while A has it; once A moves on to Site Z, X is free and C
        # waits only for a lane (all three are busy), which its text must say
        fake = self.fake(secs=0.15, max_parallel=4)
        fake.hold_until_other(X, "nowhere", timeout=0.5)
        plans = {"A": [entry(fake, X, 1, "A", [1]), entry(fake, Z, 2, "A", range(2, 11))],
                 "B": [entry(fake, Y, 3, "B", range(1, 16))],
                 "C": [entry(fake, X, 4, "C", [1])],
                 "D": [entry(fake, "Site W", 5, "D", range(1, 16))]}
        seed = self.seed(fake, plans)
        items = self.items(seed)
        pool = self.pool(fake, 3)
        pool.start()

        def until(cond, what):
            deadline = time.perf_counter() + 10
            while time.perf_counter() < deadline:
                with pool._cv:
                    if cond():
                        return
                threading.Event().wait(0.002)
            self.fail(f"never saw {what}: {items[2]['result']!r}, busy {sorted(pool._busy)}")
        for i in range(3):
            pool.submit(i + 1, *seed[i][:2], items[i], *seed[i][2:])
        until(lambda: items[2]["result"] == f"waiting for {X}: busy with A", "C waiting for A")
        pool.submit(4, *seed[3][:2], items[3], *seed[3][2:])
        until(lambda: set(pool._busy) == {"site y", "site w", "site z"}, "A on Site Z")
        with pool._cv:
            self.assertEqual((items[2]["state"], items[2]["result"]),
                             ("waiting", "resolved: 1 chapter(s) due; waiting for a download lane"))
        self.job.cancel = True
        pool.close()
        pool.join()
        pool.shutdown()

    def test_the_system_refusing_a_lane_thread(self):
        fake = self.fake()
        plans = {"A": [entry(fake, X, 1, "A", [1])], "B": [entry(fake, Y, 2, "B", [1])]}
        real = threading.Thread.start

        def start(t):
            if t.name == "mangarr-lane-2":
                raise RuntimeError("can't start new thread")
            return real(t)
        with mock.patch.object(threading.Thread, "start", start), \
             self.assertLogs("mangarr.lanes", "WARNING") as cm:
            series = self.run_all(fake, plans)
        self.assertIn("could start only 1 of 3 download lane(s)", "\n".join(cm.output))
        self.assertEqual(self.pool_.lanes, 1)
        for sid, *_ in series:
            self.assertEqual(self.statuses(sid), {"have"})
        self.assertTrue(self.lock_free())
        pool = self.pool(fake, 2)
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("can't start new thread")), \
             self.assertRaises(RuntimeError):
            pool.start()
        self.assertEqual(pool._running, 0)

    def test_submit_waits_while_too_many_series_wait_and_a_cancel_frees_it(self):
        fake = self.fake()
        fake.hold_until_other(X, "nowhere", timeout=10)     # the one lane stays on A
        plans = {f"S{k}": [entry(fake, X, k, f"S{k}", [1])] for k in range(1, 5)}
        seed = self.seed(fake, plans)
        items = self.items(seed)
        pool = self.pool(fake, 1)
        pool.start()
        with mock.patch.object(lanes, "PIPELINE_MAX_WAITING", 2):
            for i in range(3):
                sid, title, plan, due = seed[i]
                pool.submit(i + 1, sid, title, items[i], plan, due)
            raised = []

            def fourth():
                sid, title, plan, due = seed[3]
                try:
                    pool.submit(4, sid, title, items[3], plan, due)
                except lanes.PoolStopped as e:
                    raised.append(e)
            th = threading.Thread(target=fourth, daemon=True)
            th.start()
            th.join(0.3)
            self.assertTrue(th.is_alive())                  # S2 and S3 wait: the hand-over waits too
            self.assertIn("waiting: 2 series queued for download lanes", self.job.progress)
            self.job.cancel = True
            th.join(3)
        self.assertFalse(th.is_alive())
        self.assertEqual(len(raised), 1)
        pool.close()
        pool.join()
        pool.shutdown()
        self.assertEqual([i["state"] for i in items[1:3]], ["cancelled", "cancelled"])
        self.assertEqual(items[1]["result"], "pass cancelled")
        self.assertEqual(fake.items, [])

    def test_a_series_deleted_while_it_waits(self):
        fake = self.fake()
        fake.hold_until_other(X, "nowhere", timeout=0.3)
        plans = {"A": [entry(fake, X, 1, "A", [1])], "B": [entry(fake, Y, 2, "B", [1, 2])]}

        def delete_b(pool, series):
            with db.connect() as con:
                db.delete_series(con, series[1][0])
        series = self.seed(fake, plans)
        items = self.items(series)
        pool = self.pool(fake, 1)
        pool.start()
        pool.submit(1, *series[0][:2], items[0], *series[0][2:])
        pool.submit(2, *series[1][:2], items[1], *series[1][2:])
        self.assertEqual(items[1]["state"], "waiting")
        delete_b(pool, series)
        pool.close()
        pool.join()
        self.assertEqual((items[1]["state"], items[1]["result"]), ("cancelled", "series was deleted"))
        self.assertEqual([e for e in fake.kinds("enqueue") if e[3] == 2], [])
        with db.connect() as con:
            self.assertEqual(db.chapters(con, series[1][0]), [])
        self.assertEqual(items[0]["state"], "done")

    def test_pending_for_sees_resolving_and_downloading_series_not_waiting_ones(self):
        runner = jobs.Runner()
        fake = self.fake()
        fake.hold_until_other(X, "nowhere", timeout=10)
        plans = {"A": [entry(fake, X, 1, "A", [1])], "B": [entry(fake, Y, 2, "B", [1])]}
        series = self.seed(fake, plans)
        job = runner.submit("refresh-all", "all", lambda j: None)
        job.status = "running"
        self.job = job
        items = self.items(series)
        job.items = items
        pool = self.pool(fake, 1, job=job)
        pool.start()
        pool.submit(1, *series[0][:2], items[0], *series[0][2:])
        pool.submit(2, *series[1][:2], items[1], *series[1][2:])
        job.active_series_id = 99                           # being resolved
        deadline = time.perf_counter() + 3
        while items[0]["state"] != "running" and time.perf_counter() < deadline:
            time.sleep(0.01)
        self.assertTrue(runner.pending_for(series[0][0]))  # downloading
        self.assertFalse(runner.pending_for(series[1][0]))  # waiting: it may be deleted, the pool copes
        self.assertTrue(runner.pending_for(99))
        job.cancel = True
        pool.close()
        pool.join()
        pool.shutdown()
        self.assertFalse(runner.pending_for(series[0][0]))

    def test_the_download_lock_is_held_for_the_whole_pass(self):
        fake = self.fake()
        fake.hold_until_other(X, "nowhere", timeout=0.3)
        plans = {"A": [entry(fake, X, 1, "A", [1])], "B": [entry(fake, Y, 2, "B", [1])]}
        refused = []

        def try_restore(pool, series):
            try:
                with backup._no_download_run():
                    pass
            except backup.RestoreError as e:
                refused.append(str(e))
        with mock.patch.object(downloader, "acquire_download_lock", wraps=downloader.acquire_download_lock) as acq:
            self.run_all(fake, plans, before_close=try_restore)
        self.assertEqual(acq.call_count, 1)                 # once for the pass, not per series
        self.assertEqual(refused, ["a download run is in progress (another mang-arr process); try again later"])
        self.assertTrue(self.lock_free())

    def test_many_passes_with_random_timing(self):
        rnd = random.Random(7)
        for run in range(20):
            with self.subTest(run=run):
                fake = self.fake(secs=rnd.uniform(0.001, 0.005), lanes=3)
                sites = ["Site A", "Site B", "Site C", "Site A (ALL)"]
                plans = {}
                for k in range(1, 5):
                    mid = run * 100 + k
                    title = f"R{run}S{k}"
                    plans[title] = [entry(fake, sites[rnd.randrange(4)], mid, title, [1, 2]),
                                    entry(fake, sites[rnd.randrange(3)], mid + 50, title, [1, 2])]
                fake.broken = {rnd.choice([m.chapters[0].id for p in plans.values() for m in p]) for _ in range(3)}
                series = self.run_all(fake, plans)
                self.assertEqual(fake.violations, [])
                self.assertEqual(fake.items, [])
                for sid, *_ in series:
                    for n, (st, why) in self.status(sid).items():
                        # in order, a chapter every source failed holds the ones after it
                        self.assertTrue(st in ("have", "failed") or why.startswith("waiting for chapter"),
                                        (n, st, why))
                self.assertEqual({i["state"] for i in self.job.items} - {"done", "failed"}, set())
                self.assertTrue(self.lock_free())


if __name__ == "__main__":
    unittest.main()

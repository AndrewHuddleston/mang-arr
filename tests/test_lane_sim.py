"""How busy the download lanes keep a whole refresh pass, simulated: the
real web._run_pass, resolver.resolve (searching every source through the
fake), download lanes, downloads and imports, against fake_suwayomi with
sources of different speeds. Time is scaled: one simulated second takes
SCALE real seconds, and every search, search gap and chapter download is
given in simulated seconds.

12 series, 12 sources, 3 lanes, chapters in order. Every series is on
Alpha, which lists all its 8 chapters and so is its first choice, and on
one of Bravo, Charlie or Delta (the same kind of source) with all of them
or, for every other series, only the first six (7 and 8 only on Alpha);
every fourth one has a ninth chapter that only the slow Echo lists. Seven
sources have nothing and cost three searches each. A search takes 0.5-3.0
s, a chapter 6-12 s.

Searching one site after the other and waiting for Alpha (search_parallel
1, no free-site switching: 0.2.1) keeps about one lane busy: about 790 s
here, 0.9 lanes busy on average. Searching five sites at once and taking
the same chapters from a free site keeps two or three busy: about 320 s,
at least two lanes busy 85 % of the time, 2.5 on average.

    python -m unittest tests.test_lane_sim -v      (MANGARR_SIM_REPORT=1 prints the numbers)"""
import os
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_suwayomi import PassBase, entry  # noqa: E402

from mangarr import config, core, db, jobs, lanes, limits, resolver, settings  # noqa: E402
from mangarr.model import Series  # noqa: E402
from mangarr.suwayomi import Source  # noqa: E402

try:
    from mangarr.web import app as web
except (ImportError, RuntimeError):      # web extras not installed
    web = None

SCALE = 0.015                            # real seconds per simulated second
# site: (secs a search takes, secs a chapter takes), simulated
SITES = {"Alpha": (0.5, 6.0), "Bravo": (0.5, 6.0), "Charlie": (1.0, 8.0), "Delta": (1.0, 8.0), "Echo": (3.0, 12.0),
         **{f"Nothing {k}": (1.0 + 0.25 * k, None) for k in range(1, 8)}}
PARTNERS = ("Bravo", "Charlie", "Delta")
SERIES = 12


class Sim:
    """What one simulated pass did: its length (simulated s), and from the
    first lane busy to the end, the share of the time at least 2 lanes were
    busy and the mean number of busy lanes."""

    def __init__(self, took: float, samples: list[tuple[float, int]], items: list[dict]):
        self.took, self.items = took, items
        start = next((t for t, busy in samples if busy), samples[-1][0] if samples else 0.0)
        span = [(t, b) for t, b in samples if t >= start]
        total = two = weighted = 0.0
        for (t, b), (t_next, _) in zip(span, span[1:], strict=False):
            total += t_next - t
            two += (t_next - t) if b >= 2 else 0.0
            weighted += (t_next - t) * b
        self.first_busy = start
        self.share_two = two / total if total else 0.0
        self.mean_busy = weighted / total if total else 0.0

    def __str__(self) -> str:
        return (f"pass {self.took:.0f} s, first lane busy at {self.first_busy:.0f} s, then >= 2 lanes busy "
                f"{self.share_two:.0%} of the time, {self.mean_busy:.2f} lanes busy on average")


def simulate(test: PassBase, parallel: int, switch: bool) -> Sim:
    """One refresh pass over the scenario above in a data dir of its own."""
    tmp = tempfile.mkdtemp(dir=test.tmp)
    for d in ("staging", "library"):
        os.makedirs(os.path.join(tmp, d))
    fake = test.fake(max_parallel=3, lanes=3, secs=3.0 * SCALE,
                     source_secs={n: c * SCALE for n, (_, c) in SITES.items() if c},
                     search_secs={n: s * SCALE for n, (s, _) in SITES.items()})
    sources = [Source(n, n, "en") for n in SITES]
    for k in range(1, SERIES + 1):
        title = f"Series {k:02d}"
        entry(fake, "Alpha", 100 + k, title, range(1, 9))
        entry(fake, PARTNERS[k % 3], 200 + k, title, range(1, 9) if k % 2 else range(1, 7))
        if k % 4 == 0:
            entry(fake, "Echo", 300 + k, title, [9])
    samples: list[tuple[float, int]] = []
    lock = threading.Lock()
    t0 = time.perf_counter()

    def record_lanes(n, busy, waiting):
        with lock:
            samples.append(((time.perf_counter() - t0) / SCALE, busy))

    def resolve(client, series, **kw):                 # the real one, with the fake's sources
        return resolver.resolve(client, series, sources=sources, **kw)

    def pause(secs, should_cancel=None):               # real waits (the tests cut time.sleep short)
        if secs > 0:
            threading.Event().wait(secs)
        return bool(should_cancel and should_cancel())
    with mock.patch.object(config, "DB_PATH", os.path.join(tmp, "t.db")), \
            mock.patch.object(config, "LOCK_PATH", os.path.join(tmp, "lock")), \
            mock.patch.object(config, "STAGING_ROOT", os.path.join(tmp, "staging")), \
            mock.patch.object(config, "LIBRARY_ROOT", os.path.join(tmp, "library")), \
            mock.patch.object(web, "client", fake), mock.patch.object(core, "resolve", resolve), \
            mock.patch.object(resolver, "SEARCHES", limits.Spacer(pause=pause)), \
            mock.patch.object(resolver, "SEARCH_GAP_SECS", resolver.SEARCH_GAP_SECS * SCALE), \
            mock.patch.object(resolver, "GENTLE_SEARCH_GAP_SECS", resolver.GENTLE_SEARCH_GAP_SECS * SCALE), \
            mock.patch.object(lanes, "TAKE_FREE_SITE", switch), \
            mock.patch.object(lanes.metrics, "record_lanes", record_lanes):
        settings._cache.clear()
        try:
            with db.connect() as con:
                settings.set_many(con, {"download_lanes": 3, "search_parallel": parallel, "download_in_order": True,
                                        "throttled_delay_seconds": 0.0})
                for k in range(1, SERIES + 1):
                    db.upsert_series(con, Series(english=f"Series {k:02d}",
                                                 synonyms=[f"Series {k:02d} Alt", f"Series {k:02d} Other"]))
                rows = [dict(r) for r in db.series_rows(con)]
            settings._cache.clear()
            job = jobs.Job(1, "refresh-all", "all")
            t0 = time.perf_counter()
            out = web._run_pass(job, rows, "sim")
            took = (time.perf_counter() - t0) / SCALE
            with db.connect() as con:
                states = {r["id"]: {c["status"] for c in db.chapters(con, r["id"])} for r in rows}
                stats = {name: (r["ok"], r["failed"]) for name, r in db.source_stats(con).items()}
        finally:
            settings._cache.clear()
    test.assertEqual(fake.violations, [])
    test.assertEqual(out[1:], (99, 99, 0))                  # 12 x 8 chapters + 3 only Echo has, all arrived
    test.assertEqual(set().union(*states.values()), {"have"})
    delivered = Counter(e[2] for e in fake.kinds("finish"))
    test.assertEqual(stats, {name: (n, 0) for name, n in delivered.items()})   # each source's own downloads
    return Sim(took, samples, job.items)


@unittest.skipIf(web is None, "web extras not installed")
class LaneUseTest(PassBase):
    def test_the_lanes_stay_busy(self):
        before = simulate(self, parallel=1, switch=False)          # 0.2.1
        after = simulate(self, parallel=5, switch=True)
        if os.environ.get("MANGARR_SIM_REPORT"):
            print(f"\nsearch_parallel 1, no switching: {before}\nsearch_parallel 5, switching:   {after}",
                  file=sys.stderr)
        self.assertGreater(after.share_two, 0.5, str(after))       # 2 or 3 lanes busy most of the pass
        self.assertLess(after.took * 2, before.took, f"{after} / {before}")
        self.assertLess(before.mean_busy, after.mean_busy)
        self.assertEqual({i["state"] for i in after.items}, {"done"})


if __name__ == "__main__":
    unittest.main()

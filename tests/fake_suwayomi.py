"""A fake Suwayomi for the download lane and end-to-end pass tests: its
download queue and downloader, page images, and a switch that makes it stop
answering; plus the plans a resolve would make for it, and a test base with
a temporary data dir. Nothing here opens a socket: gq() refuses, and the
base blocks urlopen and outgoing connections.

Import it after sys.path.insert(0, <this folder>): tests/ is no package."""
import faulthandler
import os
import re
import tempfile
import threading
import time
import unittest
import zipfile
from collections.abc import Callable
from unittest import mock

from mangarr import config, core, downloader, lanes, library, limits, pagewarm, resolver, settings
from mangarr.model import Series
from mangarr.resolver import Plan, SourceMatch
from mangarr.suwayomi import Chapter, PageFetch, Source, SuwayomiUnreachable

_real_sleep = time.sleep            # saved at import: tests patch time.sleep globally
PAGE_RE = re.compile(r"/api/v1/manga/(\d+)/chapter/(\d+)/page/(\d+)")


def chapter_id(manga_id: int, n: float) -> int:
    return manga_id * 1000 + int(round(n * 10))


def number_of(cid: int) -> float:
    return (cid % 1000) / 10


class FakeSuwayomi:
    """Suwayomi's download queue and downloader. Each source downloads one
    chapter at a time, and at most max_parallel sources at once (its 'max
    sources in parallel'); a chapter takes `secs` (real time: Event waits,
    so a patched time.sleep does not change it). A chapter id in `broken`
    ends ERROR after 3 tries; so does a chapter of a source in `page_warm`
    (the image server refuses the burst) unless every one of its pages was
    fetched through fetch_page since the last evict(). A finished chapter
    is a real CBZ in the staging folder Suwayomi would use, and counts as
    downloaded. `foreign` items (chapter id -> source name) were queued by
    someone else: they keep their source busy for good.

    For a real resolve (resolver.resolve with sources given), search()
    finds an entry by its title in `catalog` after search_secs, and manga()
    lists its chapters; source_secs gives a source a chapter time of its own.

    `down` makes every call raise SuwayomiUnreachable. `events` records
    (t, kind, source, manga id, chapter id) for enqueue, start, finish,
    error, dequeue and page; `violations` whatever breaks the lane rules:
    our chapters of two series (manga ids) of one site queued at once, or
    ours on more than `lanes` sites at once."""

    def __init__(self, sources: dict | None = None, max_parallel: int = 3, secs: float = 0.03,
                 page_warm=(), pages: int = 4, busy_pages: dict | None = None, broken=(), foreign: dict | None = None,
                 staging_titles: dict | None = None, lanes: int | None = None, page_secs: float = 0.0,
                 source_secs: dict | None = None, search_secs: dict | None = None):
        self.sources = dict(sources or {})              # manga id -> source name
        self.max_parallel, self.secs, self.pages, self.page_secs = max_parallel, secs, pages, page_secs
        self.source_secs = dict(source_secs or {})      # source name -> secs a chapter takes there (else secs)
        self.search_secs = dict(search_secs or {})      # source name -> secs a search takes there
        self.catalog: dict[str, dict[str, int]] = {}    # source name -> {entry title: manga id} (search)
        self.searches: list[tuple[float, str, str]] = []    # (t, source name, query)
        self.page_warm = set(page_warm)
        self.busy_pages = dict(busy_pages or {})        # (chapter id, page) -> busy answers left
        self.broken = set(broken)
        self.foreign = dict(foreign or {})
        self.staging_titles = dict(staging_titles or {})    # manga id -> the entry's title
        self.listing: dict[int, list[float]] = {}       # manga id -> chapter numbers
        self.lanes = lanes
        self.bad_urls: set[int] = set()                 # chapter ids whose page list has a foreign URL
        self.down = False
        self.cap_unreadable = False
        self.have: set[int] = set()
        self.items: list[dict] = []                     # ours: {id, manga, source, state, tries, progress, t0}
        self.fetched: dict[int, set[int]] = {}          # chapter id -> pages fetched since the last evict
        self.page_log: list[tuple[int, int, str]] = []  # (chapter id, page, status)
        self.events: list[tuple] = []
        self.violations: list[str] = []
        self.in_library: dict[int, bool] = {}
        self.on_finish = None                           # on_finish(fake, chapter id), after a chapter arrived
        self.overlap_seen = False
        self._hold: tuple | None = None
        self._held: int | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.t0 = time.perf_counter()

    # -- the client's side ---------------------------------------------------------------------

    def _check(self) -> None:
        if self.down:
            raise SuwayomiUnreachable("Suwayomi at http://fake unreachable: connection refused")

    def check_up(self) -> None:
        self._check()

    def gq(self, query, *a, **k):
        raise AssertionError(f"FakeSuwayomi got a query it does not know: {str(query)[:80]!r}")

    def enqueue(self, ids) -> None:
        with self._lock:                                # checked under the lock: set_down() is exact
            self._check()
            for cid in ids:
                if any(x["id"] == cid for x in self.items):
                    continue
                mid = cid // 1000
                x = {"id": cid, "manga": mid, "source": self.sources[mid], "state": "QUEUED", "tries": 0,
                     "progress": 0.0, "t0": 0.0}
                self.items.append(x)
                self._event("enqueue", x)
            self._check_sites()

    def dequeue(self, ids, timeout: int = 30) -> None:
        self._check()
        with self._lock:
            for x in [x for x in self.items if x["id"] in ids]:
                self.items.remove(x)
                self._event("dequeue", x)

    def start(self) -> None:
        """startDownloader: the downloader runs from the first call on."""
        self._check()
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="fake-suwayomi", daemon=True)
                self._thread.start()

    def queue(self) -> list[dict]:
        self._check()
        with self._lock:
            out = [{"id": x["id"], "state": x["state"], "tries": x["tries"], "progress": x["progress"]}
                   for x in self.items]
            return out + [{"id": cid, "state": "DOWNLOADING", "tries": 0, "progress": 0.5} for cid in self.foreign]

    def downloaded_ids(self, manga_id: int) -> set[int]:
        self._check()
        with self._lock:
            return {c for c in self.have if c // 1000 == manga_id}

    def chapters(self, manga_id: int) -> list[Chapter]:
        self._check()
        with self._lock:
            return [Chapter(chapter_id(manga_id, n), n, f"Chapter {n:g}", None, chapter_id(manga_id, n) in self.have)
                    for n in self.listing.get(manga_id, [])]

    def chapter_url(self, cid: int) -> str | None:
        """The chapter's page on its (made-up) site, as Suwayomi's realUrl."""
        self._check()
        return f"https://site{cid // 1000}.example/chapter/{cid}" if cid // 1000 in self.sources else None

    def set_in_library(self, manga_id: int, in_library: bool, retries: int = 3, timeout: int = 60) -> None:
        self._check()
        self.in_library[manga_id] = in_library

    def search(self, src: Source, query: str) -> list[dict]:
        """What a source search finds: its entry whose title is the query,
        after search_secs (real time)."""
        self._check()
        with self._lock:
            self.searches.append((time.perf_counter() - self.t0, src.name, query))
        if self.search_secs.get(src.name):
            threading.Event().wait(self.search_secs[src.name])
        with self._lock:
            mid = self.catalog.get(src.name, {}).get(query)
            return [] if mid is None else [{"id": mid, "title": query, "author": None, "status": "ONGOING"}]

    def manga(self, manga_id: int) -> tuple[dict, list[Chapter]]:
        return {"id": manga_id, "title": self.staging_titles[manga_id], "author": None}, self.chapters(manga_id)

    def max_sources_in_parallel(self) -> int | None:
        return None if self.down or self.cap_unreadable else self.max_parallel

    def page_urls(self, cid: int) -> list[str]:
        self._check()
        mid, idx = cid // 1000, cid % 1000
        urls = [f"/api/v1/manga/{mid}/chapter/{idx}/page/{k}" for k in range(self.pages)]
        if cid in self.bad_urls:
            urls[1] = "http://images.example/page/1.jpg"
        return urls

    def fetch_page(self, path: str, timeout: int = 60) -> PageFetch:
        self._check()
        m = PAGE_RE.fullmatch(path)
        if not m:
            raise AssertionError(f"fetch_page got {path!r}")
        cid, k = int(m[1]) * 1000 + int(m[2]), int(m[3])
        if self.page_secs:
            threading.Event().wait(self.page_secs)
        with self._lock:
            x = {"id": cid, "manga": cid // 1000, "source": self.sources[cid // 1000]}
            self._event("page", x)
            if self.busy_pages.get((cid, k)):
                self.busy_pages[(cid, k)] -= 1
                self.page_log.append((cid, k, "busy"))
                return PageFetch("busy", 503, 0, 0.0)
            self.fetched.setdefault(cid, set()).add(k)
            self.page_log.append((cid, k, "ok"))
            return PageFetch("ok", 200, 1000, 0.0)

    # -- test controls -------------------------------------------------------------------------

    def set_down(self, down: bool = True) -> float:
        """Stop (or start again) answering; returns when, on the events' clock.
        No enqueue that lands after that time was accepted while down."""
        with self._lock:
            self.down = down
            return time.perf_counter() - self.t0

    def evict(self) -> None:
        """Suwayomi's page cache is cleared."""
        with self._lock:
            self.fetched.clear()

    def hold_until_other(self, source_a: str, source_b: str, timeout: float = 5.0) -> None:
        """The first chapter of source_a does not finish before a chapter of
        source_b downloads at the same time (overlap_seen), or `timeout` s."""
        self._hold = (source_a, source_b, timeout)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)

    def kinds(self, kind: str, source: str | None = None) -> list[tuple]:
        return [e for e in self.events if e[1] == kind and (source is None or e[2] == source)]

    # -- the downloader ------------------------------------------------------------------------

    def _event(self, kind: str, x: dict) -> None:
        self.events.append((time.perf_counter() - self.t0, kind, x["source"], x["manga"], x["id"]))

    def _check_sites(self) -> None:
        sites: dict[str, set] = {}
        for x in self.items:
            sites.setdefault(downloader.lanes_key(x["source"]), set()).add(x["manga"])
        for site, mids in sites.items():
            if len(mids) > 1:
                self.violations.append(f"{site}: chapters of manga {sorted(mids)} queued at once")
        if self.lanes is not None and len(sites) > self.lanes:
            self.violations.append(f"our chapters on {len(sites)} sites at once: {sorted(sites)}")

    def _run(self) -> None:
        while not self._stop.wait(0.002):
            arrived = []
            with self._lock:
                now = time.perf_counter()
                for x in [x for x in self.items if x["state"] == "DOWNLOADING"]:
                    if self._holding(x, now):
                        continue
                    secs = self.source_secs.get(x["source"], self.secs)
                    if now - x["t0"] >= secs:
                        if self._finish(x):
                            arrived.append(x["id"])
                    else:
                        x["progress"] = min(0.99, (now - x["t0"]) / secs)
                busy = {x["source"] for x in self.items if x["state"] == "DOWNLOADING"} | set(self.foreign.values())
                for x in self.items:
                    if x["state"] == "QUEUED" and x["source"] not in busy and len(busy) < self.max_parallel:
                        x["state"], x["t0"] = "DOWNLOADING", now
                        busy.add(x["source"])
                        self._event("start", x)
                if self._hold:
                    a, b, _ = self._hold
                    if a in busy and b in busy and a not in self.foreign.values():
                        self.overlap_seen = True
            for cid in arrived:
                if self.on_finish is not None:
                    self.on_finish(self, cid)

    def _holding(self, x: dict, now: float) -> bool:
        if not self._hold or x["source"] != self._hold[0] or self._held not in (None, x["id"]):
            return False
        self._held = x["id"]
        return not self.overlap_seen and now - x["t0"] < self._hold[2]

    def _finish(self, x: dict) -> bool:
        cid = x["id"]
        cold = x["source"] in self.page_warm and not set(range(self.pages)) <= self.fetched.get(cid, set())
        if cid in self.broken or cold:
            x["state"], x["tries"], x["progress"] = "ERROR", 3, 0.0
            self._event("error", x)
            return False
        folder = os.path.join(config.STAGING_ROOT, library.safe_title(x["source"]),
                              library.safe_title(self.staging_titles.get(x["manga"], f"T{x['manga']}")))
        os.makedirs(folder, exist_ok=True)
        with zipfile.ZipFile(os.path.join(folder, f"Chapter {number_of(cid):g}.cbz"), "w") as z:
            z.writestr("001.jpg", os.urandom(2000))
        self.have.add(cid)
        self.items.remove(x)
        self._event("finish", x)
        return True


def entry(fake: FakeSuwayomi, source_name: str, manga_id: int, title: str, numbers, throttled: bool = False,
          page_warm: bool | None = None) -> SourceMatch:
    """The source entry a resolve would find on the fake: its chapters, and
    the fake told where they live and what the entry is called."""
    warm = source_name in fake.page_warm if page_warm is None else page_warm
    src = Source(source_name, source_name, "en", throttled=throttled, page_warm=warm)
    nums = [float(n) for n in numbers]
    fake.sources[manga_id], fake.staging_titles[manga_id], fake.listing[manga_id] = source_name, title, nums
    fake.catalog.setdefault(source_name, {})[title] = manga_id
    chapters = [Chapter(chapter_id(manga_id, n), n, f"Chapter {n:g}", None, False) for n in nums]
    return SourceMatch(src, manga_id, title, None, 0, title, 1, chapters)


def make_plan(series: Series, matches: list[SourceMatch]) -> Plan:
    """A plan as resolve() makes it: candidates ranked the same way."""
    candidates = resolver._assign(matches)
    return Plan(series, matches, [], [], {n: c[0] for n, c in candidates.items()}, candidates=candidates)


def resolver_for(fake: FakeSuwayomi, plans: dict) -> Callable:
    """A stand-in for core.resolve: {series title: [entries]} -> a fresh plan
    each time (asking the fake first, so a Suwayomi that is down fails the
    resolve like a real one)."""
    def resolve(client, series, **kw):
        fake.check_up()
        return make_plan(series, list(plans.get(series.title, [])))
    return resolve


def add_series(con, fake: FakeSuwayomi, title: str) -> int:
    """A tracked series (manual: no metadata lookup) with its plan saved, as
    a refresh without download leaves it. core.resolve must be patched."""
    out = core.add_series(con, fake, Series(english=title), download=False)
    return out.series_id


class PassBase(unittest.TestCase):
    """A temporary data dir (database, lock, staging, library), no network,
    no notifications, no Komga, a short outage hold, sleeps cut to 2 ms and
    fresh pacers. A hung test dumps every thread after 120 s."""

    lanes_setting = 3

    def setUp(self):
        faulthandler.dump_traceback_later(120, exit=False)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

        def blocked(*a, **k):
            raise OSError("network disabled in tests")

        def no_urlopen(*a, **k):
            raise AssertionError("a test tried to reach the network")
        for p in (mock.patch("mangarr.config.DB_PATH", self.tmp + "/t.db"),
                  mock.patch("mangarr.config.LOCK_PATH", self.tmp + "/lock"),
                  mock.patch("mangarr.config.STAGING_ROOT", self.tmp + "/staging"),
                  mock.patch("mangarr.config.LIBRARY_ROOT", self.tmp + "/library"),
                  mock.patch("urllib.request.urlopen", no_urlopen),
                  mock.patch("socket.create_connection", blocked),
                  mock.patch("time.sleep", lambda s: _real_sleep(0.002)),
                  mock.patch("mangarr.notify.send", return_value=None),
                  mock.patch("mangarr.notify.send_detailed", return_value={}),
                  mock.patch.object(core.komga, "scan", lambda *a, **k: False),
                  mock.patch.object(core.metadata, "by_ref", lambda ref: None),
                  mock.patch.object(lanes, "HOLD_SECS", 0.2),
                  mock.patch.object(downloader, "UNREACHABLE_GIVE_UP_SECS", 0.1),
                  mock.patch.object(downloader, "_retry_leftovers_later", lambda client, first=None: None),
                  mock.patch.object(resolver, "SEARCHES", limits.Spacer(pause=lambda s, c=None: False)),
                  mock.patch.dict(pagewarm._pacers, clear=True)):
            p.start()
            self.addCleanup(p.stop)
        os.makedirs(self.tmp + "/staging")
        os.makedirs(self.tmp + "/library")
        settings._cache.clear()
        self.addCleanup(settings._cache.clear)
        downloader._unsaved = downloader._SAVED
        from mangarr import db
        with db.connect() as con:
            settings.set_many(con, {"download_lanes": self.lanes_setting, "throttled_delay_seconds": 0.05,
                                    "page_delay_seconds": 0.5})
        settings._cache.clear()
        self.addCleanup(self.assert_no_lanes_left)

    def assert_no_lanes_left(self):
        deadline = time.perf_counter() + 5
        while time.perf_counter() < deadline:
            alive = [t.name for t in threading.enumerate() if t.name.startswith("mangarr-lane-")]
            if not alive:
                return
            _real_sleep(0.02)
        self.fail(f"download lanes still running after the test: {alive}")

    def fake(self, **kw) -> FakeSuwayomi:
        fake = FakeSuwayomi(**kw)
        self.addCleanup(fake.close)
        return fake

    def status(self, sid: int) -> dict:
        from mangarr import db
        with db.connect() as con:
            return {r["number"]: (r["status"], r["reason"]) for r in db.chapters(con, sid)}

    @staticmethod
    def lock_free() -> bool:
        import fcntl
        fd = os.open(config.LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

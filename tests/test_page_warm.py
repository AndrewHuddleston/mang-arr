"""Page-by-page fetching for sources whose image server refuses bursts:
the client's page GET (Client.fetch_page over outbound.drain), the spacing
between requests to one source (limits.Spacer, pagewarm.PagePacer), a
chapter's warm-up (pagewarm.warm_chapter) and how the downloader queues a
chapter only after it, on a fake clock. Only 127.0.0.1 servers and fakes;
nothing reaches a real Suwayomi."""
import json
import logging
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from unittest import mock

from mangarr import downloader, health, limits, outbound, pagewarm, settings, suwayomi
from mangarr.resolver import SourceMatch
from mangarr.suwayomi import Chapter, CircuitOpen, Client, PageFetch, Source, SuwayomiError, SuwayomiUnreachable

PAGE = "/api/v1/manga/1/chapter/2/page/{}"


class _PageServer:
    """A local HTTP server: `routes` maps path -> (status, body bytes, headers)."""

    def __init__(self, routes):
        self.hits = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                srv.hits.append(self.path)
                status, body, headers = routes.get(self.path.split("?")[0], (404, b"not found", {}))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _HungSuwayomi:
    """A local Suwayomi that answers the page list, then hangs on every page
    GET; a status query hangs too, unless `answers` (then only the image
    server behind it is slow)."""

    def __init__(self, answers: bool):
        self.stop = threading.Event()
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if b"fetchChapterPages" in body:
                    data = {"fetchChapterPages": {"pages": [PAGE.format(k) for k in range(4)]}}
                elif b"aboutServer" in body and answers:
                    data = {"aboutServer": {"version": "v2.0"}}
                else:
                    srv.stop.wait(30)
                    return
                out = json.dumps({"data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_GET(self):
                srv.stop.wait(30)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def _trickle_server():
    """A raw server that answers 200 with a long body, one byte every 0.2 s."""
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(5)
    stop = threading.Event()

    def serve():
        ls.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = ls.accept()
            except OSError:
                continue
            try:
                c.recv(65536)
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n")
                while not stop.is_set():
                    c.sendall(b"x")
                    stop.wait(0.2)
            except OSError:
                pass
            finally:
                c.close()
    threading.Thread(target=serve, daemon=True).start()

    def close():
        stop.set()
        ls.close()
    return f"http://127.0.0.1:{ls.getsockname()[1]}", close


def _closed_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


class ClientPageTest(unittest.TestCase):
    def setUp(self):
        suwayomi._down.clear()
        self.addCleanup(suwayomi._down.clear)
        self.big = bytes(range(256)) * 1200                  # 300 KiB
        self.srv = _PageServer({PAGE.format(0): (200, self.big, {"Content-Type": "image/jpeg"}),
                                PAGE.format(1): (500, b"upstream said 429", {}),
                                PAGE.format(2): (302, b"", {"Location": "http://elsewhere.test/x.jpg"}),
                                PAGE.format(3): (404, b"", {}),
                                PAGE.format(4): (429, b"", {})})
        self.addCleanup(self.srv.close)
        self.client = Client(self.srv.url)

    def test_a_page_is_read_and_thrown_away(self):
        r = self.client.fetch_page(PAGE.format(0) + "?updatedAt=1700000000")
        self.assertEqual((r.status, r.http, r.nbytes), ("ok", 200, len(self.big)))
        self.assertEqual(self.srv.hits, [PAGE.format(0) + "?updatedAt=1700000000"])

    def test_streamed_in_pieces_and_capped(self):
        with self.assertLogs("mangarr.suwayomi", "WARNING") as cm:
            r = self.client.fetch_page(PAGE.format(0), max_bytes=100_000)
        self.assertEqual((r.status, r.nbytes), ("ok", 100_000))
        self.assertIn("larger than 100000 bytes", cm.output[0])
        seen = {}
        with mock.patch.object(outbound, "drain", lambda url, **kw: seen.update(kw) or (200, 1, False)):
            self.client.fetch_page(PAGE.format(0))
        self.assertEqual((seen["max_bytes"], seen["timeout"]), (64 << 20, 60))

        class Body:                                          # what drain asks the response for
            status, sizes, left = 200, [], 200_000

            def __enter__(self): return self
            def __exit__(self, *a): return False

            def read(self, n):
                Body.sizes.append(n)
                out = min(n, Body.left)
                Body.left -= out
                return b"x" * out
        with mock.patch.object(outbound._OPENER, "open", lambda req, timeout: Body()):
            self.assertEqual(outbound.drain("http://127.0.0.1:9/x", max_bytes=64 << 20), (200, 200_000, False))
        self.assertEqual(max(Body.sizes), 64 << 10)

    def test_busy_answers_are_no_outage(self):
        for n, http in ((1, 500), (4, 429)):
            r = self.client.fetch_page(PAGE.format(n))
            self.assertEqual((r.status, r.http, r.nbytes), ("busy", http, 0))
        self.assertEqual(suwayomi._down, {})

    def test_gone_and_redirect(self):
        self.assertEqual(self.client.fetch_page(PAGE.format(3)).status, "gone")
        with self.assertLogs("mangarr.outbound", "WARNING"):
            r = self.client.fetch_page(PAGE.format(2))
        self.assertEqual((r.status, r.http), ("error", 302))            # never followed
        self.assertEqual(suwayomi._down, {})

    def test_a_timeout_is_no_outage(self):
        url, close = _trickle_server()
        self.addCleanup(close)
        t0 = time.monotonic()
        r = Client(url).fetch_page(PAGE.format(0), timeout=1)
        self.assertEqual(r.status, "timeout")
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(suwayomi._down, {})

    def test_a_page_timeout_asks_whether_suwayomi_itself_answers(self):
        for answers in (True, False):
            with self.subTest(answers=answers):
                srv = _HungSuwayomi(answers)
                self.addCleanup(srv.close)
                with mock.patch.object(suwayomi, "PROBE_SECS", 1):
                    if answers:                                 # only the image server is slow
                        self.assertEqual(Client(srv.url).fetch_page(PAGE.format(0), timeout=1).status, "timeout")
                    else:                                       # Suwayomi hangs: not a refusing image server
                        with self.assertRaises(SuwayomiUnreachable) as cm:
                            Client(srv.url).fetch_page(PAGE.format(0), timeout=1)
                        self.assertIn("a page request got no answer in 1 s, and neither did a status query",
                                      str(cm.exception))

    def test_refused_trips_the_shared_breaker(self):
        url = _closed_port_url()
        with self.assertLogs("mangarr.suwayomi", "ERROR"), self.assertRaises(SuwayomiUnreachable):
            Client(url).fetch_page(PAGE.format(0))
        self.assertIn(url + "/api/graphql", suwayomi._down)
        sent = []
        with mock.patch("urllib.request.urlopen", lambda req, timeout: sent.append(req)), \
             mock.patch.object(outbound, "drain", lambda *a, **k: sent.append(a)):
            with self.assertRaises(CircuitOpen):
                Client(url).chapters(1)                      # another client: fails fast, nothing sent
            with self.assertRaises(CircuitOpen):
                Client(url).fetch_page(PAGE.format(0))
        self.assertEqual(sent, [])

    def test_an_answer_closes_the_breaker_once_its_window_is_over(self):
        suwayomi._down[self.client.api] = (time.monotonic() - suwayomi.BREAKER_SECS - 1, "earlier outage")
        with self.assertLogs("mangarr.suwayomi", "INFO"):
            self.assertEqual(self.client.fetch_page(PAGE.format(0)).status, "ok")
        self.assertEqual(suwayomi._down, {})

    def test_only_page_paths_are_requested(self):
        for bad in ("http://elsewhere.test" + PAGE.format(0), PAGE.format(0) + "\n", PAGE.format(0) + "/../../x",
                    "/api/graphql", PAGE.format(0) + "?a=%2e", None, PAGE.format("0" * 6)):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.client.fetch_page(bad)
        self.assertEqual(self.srv.hits, [])

    def test_a_cancel_cuts_a_slow_page_short(self):
        url, close = _trickle_server()
        self.addCleanup(close)
        flag = []
        threading.Timer(0.2, lambda: flag.append(1)).start()
        t0 = time.monotonic()
        with self.assertRaises(limits.Cancelled):
            Client(url).cancellable(lambda: bool(flag)).fetch_page(PAGE.format(0), timeout=30)
        self.assertLess(time.monotonic() - t0, 3)

    def test_drain_keeps_its_deadline(self):
        url, close = _trickle_server()
        self.addCleanup(close)
        t0 = time.monotonic()
        with self.assertRaises(TimeoutError):
            outbound.drain(url + "/x", timeout=10, deadline=time.monotonic() + 0.5)
        self.assertLess(time.monotonic() - t0, 3)
        with self.assertRaises(ValueError):
            outbound.drain("file:///etc/passwd")
        with self.assertRaises(urllib.error.HTTPError):
            outbound.drain(self.srv.url + PAGE.format(3))


class FakeClock:
    def __init__(self):
        self.t = 100.0
        self.lock = threading.Lock()

    def now(self):
        with self.lock:
            return self.t

    def advance(self, secs):
        with self.lock:
            self.t += secs


class SpacerTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.waits = []

        def pause(secs, should_cancel=None):
            self.waits.append(round(secs, 6))
            if should_cancel and should_cancel():
                return True
            self.clock.advance(max(secs, 0.0))
            return False
        self.spacer = limits.Spacer(clock=self.clock.now, pause=pause)

    def test_start_to_start_per_key(self):
        for _ in range(3):
            self.assertFalse(self.spacer.wait("weeb", 1.0))
        self.assertEqual(self.waits, [0.0, 1.0, 1.0])
        self.clock.advance(0.4)                             # 0.4 s of work after the last start
        self.spacer.wait("weeb", 1.0)
        self.assertEqual(self.waits[-1], 0.6)               # measured from the start, not the end
        self.clock.advance(5)
        self.spacer.wait("weeb", 1.0)
        self.assertEqual(self.waits[-1], 0.0)               # long idle: no wait

    def test_keys_do_not_wait_for_each_other(self):
        self.spacer.wait("weeb", 3.0)
        self.spacer.wait("bato", 3.0)
        self.spacer.wait("comick", 3.0)
        self.assertEqual(self.waits, [0.0, 0.0, 0.0])

    def test_callers_in_other_threads_queue_up(self):
        clock, gaps = FakeClock(), []
        spacer = limits.Spacer(clock=clock.now, pause=lambda secs, c=None: gaps.append(secs) or False)
        threads = [threading.Thread(target=spacer.wait, args=("weeb", 2.0)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(sorted(gaps), [0.0, 2.0, 4.0, 6.0, 8.0])     # every one its own slot, none shared

    def test_cancel(self):
        self.spacer.wait("weeb", 10.0)
        self.assertTrue(self.spacer.wait("weeb", 10.0, should_cancel=lambda: True))

    def test_old_keys_are_pruned(self):
        self.spacer.wait("old", 1.0)
        self.clock.advance(7200)
        self.spacer.wait("new", 1.0)
        self.spacer.prune(3600)
        self.assertEqual(set(self.spacer._next), {"new"})


# -- the warm-up ---------------------------------------------------------------------------------

COMICK = "Comick (Unoriginal) (EN)"


def page_path(chapter_id, p):
    return f"/api/v1/manga/{chapter_id // 1000}/chapter/{chapter_id % 1000}/page/{p}"


def comick(numbers, name=COMICK, manga_id=1):
    src = Source(str(manga_id), name, "en", page_warm=True)
    chapters = [Chapter(manga_id * 1000 + int(n * 10), float(n), f"Chapter {n}", None, False) for n in numbers]
    return SourceMatch(src, manga_id, name, None, 0, name, 1, chapters)


class FakePageClient:
    """Suwayomi in front of an image server that refuses bursts: a queued
    chapter is built only from its cache, i.e. when every page (all but
    `builds_missing`) was fetched one by one since the last evict();
    otherwise every try fails (ERROR, tries 3). `busy` maps a page path to
    how many busy answers it gives first (-1: always), `answers` to a fixed
    status ('gone', 'error'); `urls` replaces a chapter's page list. Each
    page request takes `fetch_secs` on the clock."""

    def __init__(self, clock, pages=4, busy=None, answers=None, urls=None, fetch_secs=0.5, builds_missing=0):
        self.clock, self.pages, self.fetch_secs, self.builds_missing = clock, pages, fetch_secs, builds_missing
        self.busy, self.answers, self.urls = dict(busy or {}), dict(answers or {}), dict(urls or {})
        self.events: list[tuple] = []       # (clock time, kind, what)
        self.cached: set[str] = set()
        self.queued: list[int] = []
        self.have: set[int] = set()
        self.on_start = None
        self.on_page = None
        self.down = False

    def kinds(self):
        return [e[1] for e in self.events]

    def page_urls(self, chapter_id):
        self.events.append((self.clock.now(), "urls", chapter_id))
        if chapter_id in self.urls:
            return list(self.urls[chapter_id])
        return [page_path(chapter_id, p) for p in range(self.pages)]

    def fetch_page(self, path, timeout=60):
        if self.down:
            raise SuwayomiUnreachable("Suwayomi at x unreachable")
        self.events.append((self.clock.now(), "page", path))
        self.clock.advance(self.fetch_secs)
        if self.on_page:
            self.on_page(self, path)
        fixed = self.answers.get(path)
        if fixed:
            return PageFetch(fixed, 404 if fixed == "gone" else 403, 0, self.fetch_secs)
        left = self.busy.get(path, 0)
        if left:
            self.busy[path] = left - 1 if left > 0 else left
            return PageFetch("busy", 429, 0, self.fetch_secs)
        self.cached.add(path)
        return PageFetch("ok", 200, 1000, self.fetch_secs)

    def evict(self):
        self.cached.clear()

    def enqueue(self, ids):
        self.events.append((self.clock.now(), "enqueue", list(ids)))
        self.queued += [i for i in ids if i not in self.queued]

    def dequeue(self, ids, timeout=30):
        self.queued = [c for c in self.queued if c not in ids]

    def start(self):
        if self.on_start:
            self.on_start(self)
        for cid in list(self.queued):
            missing = [p for p in range(self.pages) if page_path(cid, p) not in self.cached]
            if len(missing) <= self.builds_missing:
                self.have.add(cid)
                self.queued.remove(cid)

    def queue(self):
        return [{"id": cid, "state": "ERROR", "tries": 3, "progress": 0.0} for cid in self.queued]

    def downloaded_ids(self, manga_id):
        return {cid for cid in self.have if cid // 1000 == manga_id}


class WarmBase(unittest.TestCase):
    """A fake clock for pagewarm (its _now and _pause) and for the
    downloader's pauses, settings from self.values, fresh pacers."""

    def setUp(self):
        self.clock = FakeClock()
        self.page_waits: list[float] = []           # pagewarm's pauses (pacing and retry waits)
        self.dl_waits: list[float] = []             # the downloader's (backoff, between chapters)
        self.cancelled = False
        self.values = {"page_delay_seconds": 2.5, "throttled_delay_seconds": 0.0,
                       "page_warm_sources": [COMICK.lower()], downloader.LEFTOVER_KEY: []}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (mock.patch.object(pagewarm, "_now", self.clock.now),
                  mock.patch.object(pagewarm, "_pause", self.page_pause),
                  mock.patch.object(downloader.limits, "pause", self.dl_pause),
                  mock.patch("mangarr.downloader.time.sleep", lambda s: None),
                  mock.patch("mangarr.settings.get", lambda k: self.values.get(k, settings.DEFAULTS[k])),
                  mock.patch("mangarr.config.DB_PATH", tmp.name + "/t.db"),
                  mock.patch("mangarr.config.LOCK_PATH", tmp.name + "/lock"),
                  mock.patch.dict(pagewarm._pacers, clear=True),
                  mock.patch.object(pagewarm, "_bad_url_logged", False)):
            p.start()
            self.addCleanup(p.stop)
        limits._warned.clear()

    def page_pause(self, secs, should_cancel=None):
        self.page_waits.append(round(secs, 6))
        if should_cancel and should_cancel():
            return True
        self.clock.advance(max(secs, 0.0))
        return bool(should_cancel and should_cancel())

    def dl_pause(self, secs, should_cancel=None, step=1.0):
        self.dl_waits.append(secs)
        self.clock.advance(max(secs, 0.0))
        return bool(should_cancel and should_cancel())

    def cancel(self):
        return self.cancelled

    def run_source(self, client, numbers=(1,), warm=True, patient=True, memo=None, stop_on_fail=False):
        m = comick(numbers)
        self.memo = memo or downloader.RunMemo()
        self.said: list[str] = []
        return downloader._download_source(client, 1, m.chapters, 1, "T", COMICK, patient, self.cancel,
                                           self.said.append, self.memo, stop_on_fail=stop_on_fail, warm=warm)

    def warm(self, client, number=1):
        self.said = []
        return pagewarm.warm_chapter(client, comick([number]).chapters[0], COMICK, self.cancel, self.said.append)

    def starts(self, client, path=None):
        return [t for t, kind, what in client.events if kind == "page" and (path is None or what == path)]


class WarmUpTest(WarmBase):
    def test_pages_one_at_a_time_then_the_chapter_is_queued(self):
        client = FakePageClient(self.clock, pages=4)
        with self.assertLogs("mangarr.pagewarm", "INFO") as cm:
            ok, failed, why = self.run_source(client)
        self.assertEqual((ok, failed, why), ([1.0], [], {}))
        self.assertEqual(client.kinds(), ["urls", "page", "page", "page", "page", "enqueue"])
        self.assertEqual(client.events[-1][2], [1010])
        starts = self.starts(client)
        for a, b in pairwise(starts):
            self.assertGreaterEqual(b - a - client.fetch_secs, 2.5 - 1e-9)     # end of one to the next start
        self.assertIn("4/4 pages fetched one by one", "\n".join(cm.output))

    def test_the_spacing_follows_the_setting(self):
        self.values["page_delay_seconds"] = 6.0
        client = FakePageClient(self.clock, pages=3, fetch_secs=0.0)
        self.warm(client)
        starts = self.starts(client)
        self.assertEqual([round(b - a, 6) for a, b in pairwise(starts)], [6.0, 6.0])
        self.values["page_delay_seconds"] = 1e9                           # clamped to 60 at the point of use
        with self.assertLogs("mangarr.limits", "WARNING"):
            self.warm(client, 2)
        self.assertEqual(max(self.page_waits), 60.0)

    def test_a_busy_page_is_retried_after_5_then_10_s(self):
        self.values["page_delay_seconds"] = 0.5
        p = page_path(1010, 1)
        client = FakePageClient(self.clock, pages=4, busy={p: 2})
        with self.assertLogs("mangarr.pagewarm", "INFO"):
            r = self.warm(client)
        self.assertEqual((r.state, r.fetched, r.busy, r.recovered, r.failed), ("ok", 4, 2, 1, 0))
        tries = self.starts(client, p)
        self.assertEqual([round(b - a - client.fetch_secs, 6) for a, b in pairwise(tries)], [5.0, 10.0])
        self.assertIn(f"{COMICK}: chapter 1 - page 2 of 4: image server busy, retry 1 of 5 in 5 s", self.said)
        self.assertIn(f"{COMICK}: chapter 1 - page 2 of 4: image server busy, retry 2 of 5 in 10 s", self.said)

    def test_a_page_busy_on_every_try_is_left_out(self):
        p = page_path(1010, 2)
        client = FakePageClient(self.clock, pages=4, busy={p: -1}, builds_missing=1)
        with self.assertLogs("mangarr.pagewarm", "WARNING") as cm:
            ok, failed, why = self.run_source(client)
        self.assertEqual((ok, failed), ([1.0], []))                     # Suwayomi fetched that page itself
        self.assertEqual(len(self.starts(client, p)), pagewarm.PAGE_TRIES)
        self.assertEqual(client.kinds().count("enqueue"), 1)
        self.assertIn("page 3 of 4 was still busy after 6 tries", "\n".join(cm.output))
        client = FakePageClient(self.clock, pages=4, busy={page_path(1020, 2): -1})
        with self.assertLogs("mangarr.pagewarm", "WARNING"):
            r = self.warm(client, 2)
        self.assertEqual((r.state, r.fetched, r.failed, r.usable), ("partial", 3, 1, True))

    def test_an_image_server_that_refuses_even_paced_requests(self):
        busy = {page_path(1010, p): -1 for p in range(3)}              # 3 of 20 pages always busy
        client = FakePageClient(self.clock, pages=20, busy=busy)
        with self.assertLogs("mangarr", "WARNING") as cm:
            ok, failed, why = self.run_source(client)                   # no fallback: backs off 60, once
        self.assertEqual(client.kinds().count("enqueue"), 0)            # never queued
        self.assertEqual((ok, failed), ([], [1.0]))
        self.assertEqual(why[1.0], "the image server refused even paced page requests (0 of 20 pages), gave up "
                                   "after 60s of backoff")
        self.assertEqual([s for s in self.dl_waits if s >= 60], [60])  # no wait before giving up: no try follows
        self.assertEqual((self.memo.throttle, self.memo.gave_up), ({COMICK}, {COMICK}))
        self.assertIn("refused even paced page requests on ch 1 - backing off 60s", "\n".join(cm.output))
        self.assertIn("the image server answered busy 15 times in 10 min (stopped at page 3 of 20)",
                      "\n".join(cm.output))
        self.assertLess(self.clock.now() - 100.0, 2 * (600 + pagewarm.DELAY_MAX_SECS) + 60)   # two warm-ups
        self.assertTrue(any("(rate limiting): waiting 60 s before retrying chapter 1" in m for m in self.said))

    def test_refused_once_more_pages_than_allowed_failed(self):
        busy = {page_path(1010, p): -1 for p in (0, 1, 2)}
        client = FakePageClient(self.clock, pages=10, busy=busy)
        with mock.patch.object(pagewarm, "DEADLINE_MIN", 7200), self.assertLogs("mangarr.pagewarm", "WARNING"):
            r = self.warm(client)
        self.assertEqual((r.state, r.failed, r.fetched), ("refused", 3, 0))     # max(2, 10% of 10) = 2 allowed
        self.assertEqual(r.why, "3 pages stayed busy after 6 tries each")
        self.assertEqual(len(self.starts(client)), 18)                  # nothing after the third

    def test_a_chapter_that_takes_too_long(self):
        client = FakePageClient(self.clock, pages=20, fetch_secs=50.0)  # pages arrive, but slowly
        with self.assertLogs("mangarr", "WARNING"):
            ok, failed, why = self.run_source(client, patient=False)   # with a fallback: given up at once
        self.assertEqual(client.kinds().count("enqueue"), 0)
        self.assertEqual(failed, [1.0])
        self.assertRegex(why[1.0], r"^fetching pages one at a time took over 10 min \(1[12] of 20 pages\)$")
        self.assertEqual([s for s in self.dl_waits if s >= 60], [])
        client = FakePageClient(self.clock, pages=20, fetch_secs=50.0)
        with self.assertLogs("mangarr.pagewarm", "WARNING") as cm:
            r = self.warm(client)
        self.assertEqual((r.state, r.why, r.limit), ("deadline", "stopped at page 13 of 20 after 10 min", 600))
        self.assertIn("12/20 pages fetched in 6", cm.output[0])

    def test_retries_never_run_past_the_chapters_time(self):
        busy = {page_path(1010, p): -1 for p in range(3)}              # 3 of 4 pages always busy
        client = FakePageClient(self.clock, pages=4, busy=busy)
        with self.assertLogs("mangarr.pagewarm", "WARNING"):
            r = self.warm(client)
        self.assertEqual((r.state, r.limit), ("refused", 600))
        self.assertLessEqual(r.secs, r.limit + client.fetch_secs)       # at most the page in flight at the end
        self.assertLessEqual(self.starts(client)[-1], 100.0 + r.limit)  # no page asked for after it

    def test_a_refused_chapter_holds_its_lane_about_ten_minutes_a_try(self):
        busy = {page_path(1010, p): -1 for p in range(3)}
        for patient, tries in ((False, 1), (True, 2)):
            with self.subTest(patient=patient):
                self.clock = FakeClock()
                pagewarm._pacers.clear()
                client = FakePageClient(self.clock, pages=4, busy=dict(busy))
                with mock.patch.object(pagewarm, "_now", self.clock.now), self.assertLogs("mangarr", "WARNING"):
                    ok, failed, why = self.run_source(client, patient=patient)
                self.assertEqual(failed, [1.0])
                held = self.clock.now() - 100.0
                self.assertLess(held, tries * 610 + (tries - 1) * 60)     # was 11.8 and 24.3 min
                self.assertGreater(held, (tries - 1) * 600)

    def test_a_cancel_between_pages(self):
        client = FakePageClient(self.clock, pages=6)
        client.on_page = lambda c, path: setattr(self, "cancelled", len(self.starts(c)) >= 2)
        ok, failed, why = self.run_source(client)
        self.assertEqual((ok, failed, why), ([], [], {}))
        self.assertEqual(len(self.starts(client)), 2)
        self.assertNotIn("enqueue", client.kinds())
        a = comick([1])
        with self.assertRaises(downloader.Cancelled):
            downloader.download_one(FakePageClient(self.clock), 1, a.chapters[0], "T", COMICK, should_cancel=lambda: True)

    def test_a_cancel_during_a_retry_wait(self):
        p = page_path(1010, 1)
        client = FakePageClient(self.clock, pages=4, busy={p: -1})
        real = self.page_pause

        def pause(secs, should_cancel=None):
            if secs >= pagewarm.RETRY_FIRST_SECS:
                self.cancelled = True
            return real(secs, should_cancel)
        with mock.patch.object(pagewarm, "_pause", pause):
            ok, failed, why = self.run_source(client)
        self.assertEqual((ok, failed, why), ([], [], {}))               # nothing failed: it was not reached
        self.assertEqual(len(self.starts(client, p)), 1)
        self.assertNotIn("enqueue", client.kinds())
        self.assertEqual(self.memo.throttle, set())

    def test_an_unexpected_page_url_is_never_requested(self):
        evil = ["http://elsewhere.test/1.jpg", page_path(1010, 1) + "\n", "/api/graphql", None]
        for i, bad in enumerate(evil):
            client = FakePageClient(self.clock, urls={1010: [page_path(1010, 0), bad]}, builds_missing=4)
            with self.assertLogs("mangarr", "INFO") as cm:
                ok, failed, why = self.run_source(client)
            self.assertEqual(ok, [1.0], repr(bad))                       # downloaded the normal way
            self.assertEqual(client.kinds(), ["urls", "enqueue"], repr(bad))
            errors = [r for r in cm.records if r.levelno >= logging.ERROR]
            self.assertEqual(len(errors), 1 if i == 0 else 0, repr(bad))   # an error once per process
            self.assertIn("falling back to a normal download", "\n".join(cm.output))

    def test_no_page_list_or_a_gone_page_falls_back(self):
        client = FakePageClient(self.clock, pages=4, answers={page_path(1010, 1): "gone"})
        with self.assertLogs("mangarr.downloader", "INFO") as cm:
            self.run_source(client)
        self.assertEqual(client.kinds(), ["urls", "page", "page", "enqueue"])
        self.assertIn("page 2 answered HTTP 404; falling back to a normal download", "\n".join(cm.output))
        for answer in ([], [page_path(1010, 0)] * (pagewarm.MAX_PAGES + 1), SuwayomiError("no pages")):
            client = FakePageClient(self.clock, builds_missing=4)
            if isinstance(answer, Exception):
                client.page_urls = mock.Mock(side_effect=answer)
            else:
                client.urls = {1010: answer}
            with self.assertLogs("mangarr.downloader", "INFO"):
                ok, _, _ = self.run_source(client)
            self.assertEqual((ok, self.starts(client), client.kinds()[-1:]), ([1.0], [], ["enqueue"]))

    def test_a_cleared_page_cache_is_warmed_once_more(self):
        client = FakePageClient(self.clock, pages=3)
        starts = []

        def evict_first(c):
            starts.append(1)
            if len(starts) == 1:
                c.evict()                                               # Suwayomi dropped its cache meanwhile
        client.on_start = evict_first
        with self.assertLogs("mangarr.downloader", "WARNING") as cm:
            ok, failed, why = self.run_source(client)
        self.assertEqual((ok, failed), ([1.0], []))
        self.assertEqual(client.kinds(), ["urls"] + ["page"] * 3 + ["enqueue"] + ["urls"] + ["page"] * 3 + ["enqueue"])
        self.assertEqual(self.memo.rewarmed, {1010})
        self.assertIn("did not build ch 1 from its page cache", "\n".join(cm.output))
        self.assertEqual(self.memo.throttle, set())                     # not a rate limit

    def test_a_chapter_still_not_built_after_that_fails(self):
        client = FakePageClient(self.clock, pages=3)
        client.on_start = FakePageClient.evict
        with self.assertLogs("mangarr.downloader", "WARNING"):
            ok, failed, why = self.run_source(client, numbers=(1, 2), stop_on_fail=True)
        self.assertEqual((ok, failed), ([], [1.0]))
        self.assertEqual(why[1.0], downloader.WARM_BUILD_FAILED)
        self.assertEqual(client.kinds().count("enqueue"), 2)            # ch 1 twice; in order, ch 2 waits
        self.assertEqual(client.queued, [])                             # ours taken back out each time

    def test_progress_text(self):
        client = FakePageClient(self.clock, pages=2, busy={page_path(1010, 1): 1})
        with self.assertLogs("mangarr.pagewarm", "INFO"):
            self.run_source(client, numbers=(1, 2))
        self.assertEqual(self.said[:6], [
            f"{COMICK}: chapter 1 (0 of 2 done)",
            f"{COMICK}: chapter 1 - fetching page 1 of 2 one at a time (0 of 2 done)",
            f"{COMICK}: chapter 1 - fetching page 2 of 2 one at a time (0 of 2 done)",
            f"{COMICK}: chapter 1 - page 2 of 2: image server busy, retry 1 of 5 in 5 s (0 of 2 done)",
            f"{COMICK}: chapter 1 - fetching page 2 of 2 one at a time (0 of 2 done)",
            f"{COMICK}: chapter 1 - 2 of 2 pages cached; Suwayomi is building the chapter (0 of 2 done)"])
        self.assertIn(f"{COMICK}: chapter 2 - fetching page 1 of 2 one at a time (1 of 2 done)", self.said)

    def test_suwayomi_not_answering_stops_it(self):
        client = FakePageClient(self.clock, pages=4)
        client.on_page = lambda c, path: setattr(c, "down", True)
        with self.assertRaises(SuwayomiUnreachable):
            self.run_source(client)
        self.assertNotIn("enqueue", client.kinds())

    def test_a_normal_source_never_asks_for_pages(self):
        client = FakePageClient(self.clock, pages=4, builds_missing=4)
        client.page_urls = mock.Mock(side_effect=AssertionError("page list asked for"))
        ok, failed, why = self.run_source(client, numbers=(1, 2), warm=False)
        self.assertEqual(ok, [1.0, 2.0])
        client.page_urls.assert_not_called()

    def test_a_series_with_one_chapter_only_on_the_gentle_source(self):
        from mangarr.model import Series
        from mangarr.resolver import Plan

        weeb = Source("2", "Weeb Central", "en")
        weeb = SourceMatch(weeb, 2, "Weeb Central", None, 0, "T", 1,
                           [Chapter(2000 + n * 10, float(n), None, None, False) for n in (1, 3)])
        slow = comick([2])
        cands = {1.0: [weeb], 2.0: [slow], 3.0: [weeb]}
        plan = Plan(Series(english="T"), [weeb, slow], [], [], {n: c[0] for n, c in cands.items()}, candidates=cands)
        client = FakePageClient(self.clock, pages=3)
        real_start = client.start

        def start():                                                    # Weeb Central serves bursts fine
            for cid in list(client.queued):
                if cid // 1000 == 2:
                    client.have.add(cid)
                    client.queued.remove(cid)
            real_start()
        client.start = start
        with self.assertLogs("mangarr", "INFO"):
            res = downloader.download(client, plan, only={1.0, 2.0, 3.0}, in_order=True)
        self.assertEqual(res, {1.0: "ok", 2.0: "ok", 3.0: "ok"})
        order = [(kind, what) for _, kind, what in client.events if kind != "page"]
        self.assertEqual(order, [("enqueue", [2010]), ("urls", 1020), ("enqueue", [1020]), ("enqueue", [2030])])
        pages = [i for i, e in enumerate(client.events) if e[1] == "page"]
        self.assertEqual(len(pages), 3)
        self.assertLess(max(pages), client.kinds().index("enqueue", 2))  # every page before ch 2 is queued


class HungSuwayomiWarmTest(WarmBase):
    def test_a_hung_suwayomi_is_not_taken_for_a_refusing_image_server(self):
        suwayomi._down.clear()
        self.addCleanup(suwayomi._down.clear)
        srv = _HungSuwayomi(answers=False)
        self.addCleanup(srv.close)
        with mock.patch.object(pagewarm, "PAGE_TIMEOUT_SECS", 1), mock.patch.object(suwayomi, "PROBE_SECS", 1), \
             self.assertRaises(SuwayomiUnreachable):
            self.run_source(Client(srv.url))
        self.assertEqual((self.memo.throttle, self.memo.gave_up), (set(), set()))   # no false rate-limit mark


class DownloadOneWarmTest(WarmBase):
    def test_a_listed_source_is_warmed(self):
        a = comick([4])
        client = FakePageClient(self.clock, pages=2)
        with self.assertLogs("mangarr.pagewarm", "INFO"):
            ok, failed, why = downloader.download_one(client, 1, a.chapters[0], "T", COMICK)
        self.assertTrue(ok)
        self.assertEqual(client.kinds(), ["urls", "page", "page", "enqueue"])

    def test_unlisted_or_unreadable_setting_downloads_normally(self):
        a = comick([4])
        for value in ([], ["weeb central"], 8.0, True, None, "comick (unoriginal) (en)"):
            self.values["page_warm_sources"] = value
            client = FakePageClient(self.clock, pages=2, builds_missing=2)
            ok, failed, why = downloader.download_one(client, 1, a.chapters[0], "T", COMICK)
            self.assertTrue(ok, repr(value))
            self.assertEqual(client.kinds(), ["enqueue"], repr(value))
        with mock.patch("mangarr.settings.get", mock.Mock(side_effect=RuntimeError("database is locked"))):
            self.assertFalse(downloader._page_warm_name(COMICK))


class PacerTest(WarmBase):
    def test_busy_widens_the_spacing_and_answers_ease_it_back(self):
        p = pagewarm.pacer(COMICK)
        self.assertEqual(p.gap(), 2.5)
        p.done("busy")
        self.assertAlmostEqual(p.gap(), 3.75)
        p.done("timeout")
        self.assertAlmostEqual(p.gap(), 5.625)
        for _ in range(10):
            p.done("busy")
        self.assertEqual(p.gap(), pagewarm.DELAY_MAX_SECS)
        for _ in range(4):
            p.done("ok")
        self.assertEqual(p.gap(), 30.0)                                 # eased every 5 answers, not every one
        p.done("ok")
        self.assertAlmostEqual(p.gap(), 25.5)
        p.done("gone")
        self.assertAlmostEqual(p.gap(), 25.5)
        for _ in range(200):
            p.done("ok")
        self.assertEqual(p.gap(), 2.5)                                  # never below the setting
        self.values["page_delay_seconds"] = 4.0
        self.assertEqual(p.gap(), 4.0)                                  # which is read at every wait

    def test_one_pacer_per_site(self):
        self.assertIs(pagewarm.pacer(COMICK), pagewarm.pacer("Comick (Unoriginal) (ALL)"))
        self.assertIsNot(pagewarm.pacer(COMICK), pagewarm.pacer("Weeb Central"))
        comick_p, weeb = pagewarm.pacer(COMICK), pagewarm.pacer("Weeb Central")
        self.assertFalse(comick_p.wait())
        comick_p.done("busy")
        self.assertFalse(weeb.wait())
        self.assertEqual(self.page_waits, [0.0, 0.0])                   # another site does not wait
        self.assertFalse(comick_p.wait())
        self.assertEqual(self.page_waits[-1], 3.75)

    def test_a_cancel_cuts_the_wait_short(self):
        p = pagewarm.pacer(COMICK)
        p.wait()
        p.done("ok")
        self.assertTrue(p.wait(lambda: True))

    def test_overlapping_callers_take_their_own_slots(self):
        p = pagewarm.pacer(COMICK)
        gaps = []
        with mock.patch.object(pagewarm, "_pause", lambda secs, c=None: gaps.append(round(secs, 6)) or False):
            threads = [threading.Thread(target=p.wait) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
        self.assertEqual(sorted(gaps), [0.0, 2.5, 5.0, 7.5])


class _HealthSources:
    """health._compute's view of Suwayomi: a version, these sources and its
    'max sources in parallel' (None: cannot be read)."""

    def __init__(self, sources, cap=3):
        self._sources, self.cap, self.asked = sources, cap, 0

    def gq(self, query, *a, **k):
        return {"aboutServer": {"version": "v2"}, "sources": {"totalCount": len(self._sources)}}

    def sources(self):
        return list(self._sources)

    def max_sources_in_parallel(self):
        self.asked += 1
        return self.cap

    def set_max_sources_in_parallel(self, n):
        raise AssertionError("a health check must never change Suwayomi's settings")


class _HealthBase(unittest.TestCase):
    def checks(self, sources, cap=3, lanes=None):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("mangarr.config.DB_PATH", tmp + "/t.db"), \
             mock.patch("mangarr.config.STAGING_ROOT", tmp), mock.patch("mangarr.config.LIBRARY_ROOT", tmp), \
             mock.patch.object(health, "_ping", lambda name, url, timeout=8: health.Check("ok", name, "x")), \
             mock.patch.object(health, "_alert", lambda out: []), \
             mock.patch("mangarr.komga.configured", return_value=False), \
             mock.patch.dict(health._cache, {"at": 0.0, "checks": []}):
            settings._cache.clear()
            try:
                if lanes is not None:
                    from mangarr import db
                    with db.connect() as con:
                        settings.set_many(con, {"download_lanes": lanes})
                self.client = _HealthSources(sources, cap)
                return {c.name: c for c in health._compute(self.client)}
            finally:
                settings._cache.clear()


class PageWarmHealthTest(_HealthBase):
    def test_a_usable_page_by_page_source(self):
        c = self.checks([Source("1", COMICK, "en", page_warm=True), Source("2", "Weeb Central", "en")])
        self.assertEqual(c["Page-by-page sources"].level, "ok")
        self.assertIn(f"{COMICK}: the image server refuses bursts, so chapters only it has are fetched page by page",
                      c["Page-by-page sources"].detail)

    def test_one_that_is_switched_off(self):
        c = self.checks([Source("1", COMICK, "en", unusable=True, page_warm=True), Source("2", "Weeb Central", "en")])
        self.assertEqual(c["Page-by-page sources"].level, "ok")                  # a hint, never a page
        self.assertEqual(c["Page-by-page sources"].detail,
                         f"{COMICK} is set to page by page but disabled in Settings -> Sources; tick it to use it "
                         "for chapters no other source has.")
        self.assertNotIn("Page-by-page sources", self.checks([Source("2", "Weeb Central", "en")]))


class LanesHealthTest(_HealthBase):
    """The "Download lanes" check: Download Lanes against Suwayomi's 'max sources in parallel'. A warning
    at most (only an error pages), and Suwayomi's setting is only read."""
    SOURCES = [Source("2", "Weeb Central", "en")]

    def test_enough(self):
        c = self.checks(self.SOURCES, cap=6)["Download lanes"]
        self.assertEqual((c.level, c.detail), ("ok", "3 lanes; Suwayomi allows 6"))

    def test_suwayomi_allows_fewer(self):
        c = self.checks(self.SOURCES, cap=1)["Download lanes"]
        self.assertEqual(c.level, "warning")
        self.assertEqual(c.detail, "Suwayomi allows 1 source in parallel but Download Lanes is 3: downloads from "
                                   "different sources take turns. Raise it on the Settings page.")
        c = self.checks(self.SOURCES, cap=2, lanes=5)["Download lanes"]
        self.assertIn("Suwayomi allows 2 sources in parallel but Download Lanes is 5", c.detail)

    def test_cannot_be_read(self):
        c = self.checks(self.SOURCES, cap=None)["Download lanes"]
        self.assertEqual(c.level, "warning")
        self.assertIn("could not be read, so a pass downloads from one source at a time", c.detail)

    def test_one_lane_does_not_ask(self):
        c = self.checks(self.SOURCES, cap=None, lanes=1)["Download lanes"]
        self.assertEqual(c.level, "ok")
        self.assertEqual(self.client.asked, 0)

    def test_never_an_error(self):
        for cap in (None, 1, 2, 3, 8):
            for lanes in (1, 3, 8):
                self.assertNotEqual(self.checks(self.SOURCES, cap=cap, lanes=lanes)["Download lanes"].level,
                                    "error", (cap, lanes))


if __name__ == "__main__":
    unittest.main()

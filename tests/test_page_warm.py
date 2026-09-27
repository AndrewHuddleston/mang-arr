"""Page-by-page fetching for sources whose image server refuses bursts:
the client's page GET (Client.fetch_page over outbound.drain) and the
spacing between requests to one source (limits.Spacer). Only 127.0.0.1
servers; nothing reaches a real Suwayomi."""
import socket
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from mangarr import limits, outbound, suwayomi
from mangarr.suwayomi import CircuitOpen, Client, SuwayomiUnreachable

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


if __name__ == "__main__":
    unittest.main()

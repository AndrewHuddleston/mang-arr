"""A text import list is fetched from a URL anyone may control: the download
is bounded in size and time, redirects stay on the same host, the lines are
capped, the sync can be cancelled, and the result quotes only a little."""
import http.server
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from mangarr import db, lists
from mangarr.model import Series


class Handler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):
        fn = self.routes.get(self.path)
        if fn is None:
            self.send_error(404)
            return
        fn(self)

    def log_message(self, *a):
        pass


def big(h):
    h.send_response(200)
    h.end_headers()
    try:
        for _ in range(400):                      # 400 x 64 KB = 25 MB, far over the cap
            h.wfile.write(b"Title\n" * 10923)
    except (BrokenPipeError, ConnectionResetError):
        pass


def trickle(h):
    h.send_response(200)
    h.send_header("Content-Length", "1000")
    h.end_headers()
    try:
        for _ in range(1000):
            h.wfile.write(b"x")
            h.wfile.flush()
            time.sleep(0.05)
    except (BrokenPipeError, ConnectionResetError):
        pass


def small(h):
    body = b"One Piece\nBerserk\n"
    h.send_response(200)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def redirect_away(h):
    h.send_response(302)
    h.send_header("Location", "http://example.invalid/secret")
    h.end_headers()


def redirect_same(h):
    h.send_response(302)
    h.send_header("Location", "/small")
    h.end_headers()


class FetchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Handler.routes = {"/big": big, "/trickle": trickle, "/small": small, "/away": redirect_away,
                          "/same": redirect_same}
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_small_list(self):
        self.assertEqual(lists._get_text(self.base + "/small"), "One Piece\nBerserk\n")

    def test_huge_body_is_refused(self):
        with self.assertRaises(lists.ListFetchError) as cm, self.assertLogs("mangarr.lists", "WARNING"):
            lists._get_text(self.base + "/big")
        self.assertIn("larger than", str(cm.exception))

    def test_trickling_body_hits_the_deadline(self):
        t0 = time.monotonic()
        with self.assertRaises(lists.ListFetchError) as cm, self.assertLogs("mangarr.lists", "WARNING"):
            lists._get_text(self.base + "/trickle", deadline=1)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("longer than", str(cm.exception))

    def test_redirects(self):
        self.assertEqual(lists._get_text(self.base + "/same"), "One Piece\nBerserk\n")
        with self.assertRaises(lists.ListFetchError):
            lists._get_text(self.base + "/away")

    def test_only_http(self):
        for url in ("file:///etc/passwd", "ftp://x/list.txt"):
            with self.assertRaises(ValueError):
                lists._get_text(url)
            with self.assertRaises(ValueError):
                lists.validate_params("url_text", {"url": url})

    def test_link_local_peer_is_refused(self):
        class Sock:
            def __init__(self, ip):
                self.ip = ip
                self.closed = False

            def getpeername(self):
                return (self.ip, 80)

            def close(self):
                self.closed = True
        for ip in ("169.254.169.254", "fe80::1", "::ffff:169.254.169.254", "0.0.0.0"):
            s = Sock(ip)
            with self.assertRaises(lists.ListFetchError, msg=ip):
                lists._check_peer(s)
            self.assertTrue(s.closed)
        for ip in ("192.168.1.10", "10.0.0.2", "127.0.0.1", "93.184.216.34"):
            lists._check_peer(Sock(ip))                       # a NAS on the LAN is the normal case


class LinesTest(unittest.TestCase):
    def test_lines_are_capped_deduplicated_and_length_checked(self):
        text = "\n".join([f"Title {i}" for i in range(2000)] + ["Title 1"] * 10 + ["y" * 5000])
        with self.assertLogs("mangarr.lists", "WARNING"):
            titles = lists.parse_titles(text, lists.MAX_LIST_LINES)
        self.assertEqual(len(titles), lists.MAX_LIST_LINES)
        self.assertEqual(len(set(titles)), len(titles))
        self.assertTrue(all(len(t) <= lists.MAX_LINE for t in titles))

    def test_lookups_are_bounded_and_cancellable(self):
        text = "\n".join(f"Title {i}" for i in range(5000))
        calls = []

        def lookup(t):
            calls.append(t)
            return None, []
        with mock.patch.object(lists, "_get_text", lambda url: text), \
                mock.patch.object(lists.metadata, "lookup", lookup), self.assertLogs("mangarr.lists", "WARNING"):
            lists.fetch_url_text({"url": "http://x"})
        self.assertEqual(len(calls), lists.MAX_LIST_LINES)
        calls.clear()
        with mock.patch.object(lists, "_get_text", lambda url: text), \
                mock.patch.object(lists.metadata, "lookup", lookup), self.assertLogs("mangarr.lists", "INFO"):
            lists.fetch("url_text", {"url": "http://x"}, should_cancel=lambda: len(calls) >= 3)
        self.assertEqual(len(calls), 3)


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "l.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_review_is_quoted_short(self):
        review = [f"line {i} " + "q" * 250 for i in range(100)] + ["evil\nforged"]
        with db.connect(self.path) as con:
            lid = lists.add_list(con, "L", "url_text", {"url": "http://x"})
            with mock.patch.dict(lists.FETCHERS, {"url_text": lambda p: ([], review)}):
                msg = lists.sync(con, lists.get_list(con, lid), lambda *a: None)
        self.assertIn("101 review", msg)                        # the count is still complete
        self.assertLessEqual(msg.count(" | "), lists.MAX_REVIEW_SHOWN)
        self.assertNotIn("q" * (lists.MAX_REVIEW_CHARS + 1), msg)

    def test_last_sync_is_stamped_before_the_fetch(self):
        seen = {}
        with db.connect(self.path) as con:
            lid = lists.add_list(con, "L", "url_text", {"url": "http://x"})
            con.commit()

            def fetcher(p):
                with db.connect(self.path) as other:                  # what a restart would see
                    seen["row"] = dict(lists.get_list(other, lid))
                return [Series(anilist_id=1, english="A")], []
            with mock.patch.dict(lists.FETCHERS, {"url_text": fetcher}):
                lists.sync(con, lists.get_list(con, lid), lambda *a: None)
        self.assertIsNotNone(seen["row"]["last_sync"])
        self.assertFalse(lists.is_due(seen["row"]))


if __name__ == "__main__":
    unittest.main()

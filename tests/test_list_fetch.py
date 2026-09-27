"""A text import list is fetched from a URL anyone may control: the download
is bounded in size and time, redirects stay on the same host, loopback and
link-local addresses are refused, only lines that look like titles are
looked up or quoted, other documents are refused without quoting them, the
lines are capped, the sync can be cancelled, and the result quotes only a
little."""
import http.server
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from mangarr import db, lists
from mangarr.model import Series

SECRET_JSON = b'{"secret_db_password": "hunter2",\n"internal_token": "abc123"}\n'


class Handler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}
    served: list = []

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


def trickle_headers(h):
    """The status line and then a header one byte at a time, never ending:
    the body loop is never reached, so only a whole-fetch deadline helps."""
    try:
        h.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
        h.wfile.flush()
        for _ in range(1200):                     # up to ~60 s
            h.wfile.write(b"a")
            h.wfile.flush()
            time.sleep(0.05)
    except (BrokenPipeError, ConnectionResetError):
        pass
    h.close_connection = True


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


def redirect_away_secret(h):
    h.send_response(302)
    h.send_header("Location", "http://example.invalid/admin?token=hunter2")
    h.end_headers()


def serve(body: bytes, content_type: str | None = None, status: int = 200, reason: str | None = None):
    def handler(h):
        Handler.served.append(h.path)
        h.send_response(status, reason)
        if content_type:
            h.send_header("Content-Type", content_type)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
    return handler


def not_http(h):
    h.wfile.write(b"SSH-2.0-OpenSSH_8.9 hunter2\r\n")
    h.close_connection = True


def allow_loopback(real):
    """The test server listens on 127.0.0.1, which lists may not use; the
    fetch mechanics are tested against it with loopback allowed."""
    return lambda ip: None if ip.is_loopback else real(ip)


class ServerTest(unittest.TestCase):
    """A local HTTP server; loopback allowed unless allow = False."""
    allow = True

    @classmethod
    def setUpClass(cls):
        Handler.routes = {"/big": big, "/trickle": trickle, "/small": small, "/away": redirect_away,
                          "/same": redirect_same, "/slow-headers": trickle_headers,
                          "/away-secret": redirect_away_secret, "/not-http": not_http,
                          "/secret": serve(SECRET_JSON, "text/plain"),
                          "/secret-json": serve(SECRET_JSON, "application/json"),
                          "/page": serve(b"<!DOCTYPE html>\n<html><head><title>Router admin</title></head>\n"
                                         b"<body>password: hunter2</body></html>\n", "text/plain"),
                          "/env": serve(b"DB_HOST=10.0.0.5\nDB_PASSWORD=hunter2\nAPI_TOKEN=abc123\nOK\n"),
                          "/mixed": serve(b"One Piece\n{\"token\": \"abc123\"}\nBerserk\n", "text/plain; charset=utf-8"),
                          "/oops": serve(b"", "text/plain", 500, "secret=hunter2")}
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.patch = mock.patch.object(lists, "_refused", allow_loopback(lists._refused)) if cls.allow else None
        if cls.patch:
            cls.patch.start()

    @classmethod
    def tearDownClass(cls):
        if cls.patch:
            cls.patch.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.served.clear()


class FetchTest(ServerTest):

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

    def test_trickling_headers_hit_the_deadline(self):
        # the per-read timeout never trips (a byte every 0.05 s); before the
        # watchdog this blocked in http.client's header readline for a minute
        t0 = time.monotonic()
        with self.assertRaises(lists.ListFetchError) as cm, self.assertLogs("mangarr.lists", "WARNING"):
            lists._get_text(self.base + "/slow-headers", deadline=1)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("longer than", str(cm.exception))

    def test_cancel_stops_a_trickling_fetch(self):
        t0 = time.monotonic()
        with self.assertRaises(lists.ListFetchError) as cm, self.assertLogs("mangarr.lists", "INFO"):
            lists._get_text(self.base + "/slow-headers", deadline=60, should_cancel=lambda: time.monotonic() - t0 > 0.5)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("cancelled", str(cm.exception))

    def test_silent_tls_handshake_hits_the_deadline(self):
        # a server that accepts and never answers the TLS ClientHello
        srv = socket.create_server(("127.0.0.1", 0))
        conns = []
        threading.Thread(target=lambda: conns.append(srv.accept()), daemon=True).start()
        try:
            t0 = time.monotonic()
            with self.assertRaises(lists.ListFetchError), self.assertLogs("mangarr.lists", "WARNING"):
                lists._get_text(f"https://127.0.0.1:{srv.getsockname()[1]}/list.txt", deadline=1)
            self.assertLess(time.monotonic() - t0, 5)
        finally:
            for c, _ in conns:
                c.close()
            srv.close()

    def test_redirects(self):
        self.assertEqual(lists._get_text(self.base + "/same"), "One Piece\nBerserk\n")
        with self.assertRaises(lists.ListFetchError):
            lists._get_text(self.base + "/away")
        with self.assertRaises(lists.ListFetchError) as cm:     # where it points, not its path or query
            lists._get_text(self.base + "/away-secret")
        self.assertIn("http://example.invalid", str(cm.exception))
        self.assertNotIn("hunter2", str(cm.exception))

    def test_only_http(self):
        for url in ("file:///etc/passwd", "ftp://x/list.txt"):
            with self.assertRaises(ValueError):
                lists._get_text(url)
            with self.assertRaises(ValueError):
                lists.validate_params("url_text", {"url": url})

    def test_errors_quote_nothing_the_server_sent(self):
        with self.assertRaises(lists.ListFetchError) as cm:
            lists._get_text(self.base + "/oops")                # the reason phrase is the server's text
        self.assertEqual(str(cm.exception), "the server answered HTTP 500")
        with self.assertRaises(lists.ListFetchError) as cm:
            lists._get_text(self.base + "/not-http")            # nor is a banner of a service that is not HTTP
        self.assertEqual(str(cm.exception), "the server did not answer with valid HTTP")

    def test_non_text_content_type_is_refused(self):
        with self.assertRaises(lists.ListFetchError) as cm, self.assertLogs("mangarr.lists", "WARNING"):
            lists._get_text(self.base + "/secret-json")
        self.assertEqual(str(cm.exception), "the URL did not return a title list (it sent JSON)")
        self.assertEqual(lists._get_text(self.base + "/mixed"), "One Piece\n{\"token\": \"abc123\"}\nBerserk\n")


class AddressTest(ServerTest):
    """Loopback, link-local, multicast and unspecified addresses are refused
    when a list is added and again at connect; LAN addresses are allowed."""
    allow = False

    def test_peer_check(self):
        class Sock:
            def __init__(self, ip):
                self.ip = ip
                self.closed = False

            def getpeername(self):
                return (self.ip, 80)

            def close(self):
                self.closed = True
        for ip in ("169.254.169.254", "fe80::1", "::ffff:169.254.169.254", "0.0.0.0", "127.0.0.1", "127.0.0.11",
                   "::1", "::ffff:127.0.0.1", "224.0.0.251", "ff02::1"):
            s = Sock(ip)
            with self.assertRaises(lists.ListFetchError, msg=ip), self.assertLogs("mangarr.lists", "WARNING"):
                lists._check_peer(s)
            self.assertTrue(s.closed)
        for ip in ("192.168.1.10", "10.0.0.2", "172.17.0.1", "fd00::5", "93.184.216.34"):
            lists._check_peer(Sock(ip))                       # a NAS on the LAN is the normal case

    def test_loopback_server_is_refused_at_connect(self):
        # a list stored before the check, or a host name that resolves to
        # 127.0.0.1: refused after connect, before a request is sent
        with self.assertRaises(lists.ListFetchError) as cm, self.assertLogs("mangarr.lists", "WARNING"):
            lists._get_text(self.base + "/secret")
        self.assertIn("loopback", str(cm.exception))
        self.assertEqual(Handler.served, [])
        with tempfile.TemporaryDirectory() as tmp, db.connect(os.path.join(tmp, "l.db")) as con:
            lid = lists.add_list(con, "L", "url_text", {"url": self.base + "/secret"})
            with mock.patch.object(lists.metadata, "lookup", lambda t: self.fail(f"looked up {t!r}")), \
                    self.assertLogs("mangarr.lists", "WARNING"):
                msg = lists.sync(con, lists.get_list(con, lid), lambda *a: None)
        self.assertEqual(msg, "error: refusing to fetch a list from 127.0.0.1 (a loopback address)")

    def test_validation(self):
        for url in ("http://127.0.0.1:6789/api/v1/settings", "http://localhost/x", "http://LocalHost./x",
                    "http://mangarr.localhost/x", "http://169.254.169.254/latest/meta-data/", "http://[::1]/x",
                    "http://[::ffff:127.0.0.1]/x", "http://127.1/x", "http://2130706433/x", "http://0x7f.0.0.1/x",
                    "http://0.0.0.0/x", "http://0/x", "http://[fe80::1%25eth0]/x", "http://224.0.0.1/x"):
            with self.assertRaises(ValueError, msg=url):
                lists.validate_params("url_text", {"url": url})
        for url in ("http://192.168.1.10/list.txt", "http://nas.lan:8080/manga.txt", "http://nas/manga.txt",
                    "https://example.com/list.txt", "http://10.0.0.2/l.txt", "http://[fd00::1]/l.txt"):
            self.assertEqual(lists.validate_params("url_text", {"url": url}), {"url": url})


class ContentTest(ServerTest):
    """What a list URL returns is shown on /lists and sent to AniList and
    MangaDex only line by line and only when the line looks like a title."""

    def _sync(self, path):
        looked_up = []

        def lookup(t):
            looked_up.append(t)
            return None, []
        with tempfile.TemporaryDirectory() as tmp, db.connect(os.path.join(tmp, "l.db")) as con:
            lid = lists.add_list(con, "L", "url_text", {"url": self.base + path})
            with mock.patch.object(lists.metadata, "lookup", lookup), self.assertLogs("mangarr.lists", "INFO"):
                msg = lists.sync(con, lists.get_list(con, lid), lambda *a: None)
            self.assertEqual(lists.get_list(con, lid)["last_result"], msg)
        return msg, looked_up

    def test_json_reply_is_neither_quoted_nor_looked_up(self):
        # the review repro: an internal JSON answer served as text
        for path in ("/secret", "/secret-json"):
            msg, looked_up = self._sync(path)
            self.assertTrue(msg.startswith("error: the URL did not return a title list"), msg)
            self.assertNotIn("hunter2", msg)
            self.assertNotIn("secret", msg)
            self.assertEqual(looked_up, [])

    def test_web_page_and_config_file_are_refused(self):
        for path, why in (("/page", "it looks like a web page or XML"), ("/env", "most of its lines are not titles")):
            msg, looked_up = self._sync(path)
            self.assertEqual(msg, f"error: the URL did not return a title list ({why})")
            self.assertEqual(looked_up, [])

    def test_junk_lines_in_a_real_list_are_skipped(self):
        msg, looked_up = self._sync("/mixed")
        self.assertEqual(looked_up, ["One Piece", "Berserk"])
        self.assertEqual(msg, "0 fetched, 0 added, 2 review, 1 line(s) skipped (not titles); "
                              "needs review: One Piece | Berserk")


class LinesTest(unittest.TestCase):
    TITLES = ["One Piece", "Re:Zero kara Hajimeru Isekai Seikatsu", "Kaguya-sama: Love Is War", "[Oshi no Ko]",
              '"Oshi no Ko"', "Ajin: Demi-Human", "1+2=Paradise", "Steins;Gate", ".hack//G.U.+", "Dr. STONE",
              "Yu-Gi-Oh!", "Is It Wrong to Try to Pick Up Girls in a Dungeon?", "100% Perfect Girl", "Tokyo Ghoul:re",
              "Hunter × Hunter", "葬送のフリーレン", "나 혼자만 레벨업", "<Infinite Dendrogram>", "Kaiju No. 8",
              "Zom 100: Bucket List of the Dead", "magi: the labyrinth of magic", "JoJo's Bizarre Adventure Part 7: Steel Ball Run",
              "anilist:30013", "mangadex:12345678-1234-1234-1234-123456789abc", "Me & Roboco", "Haikyu!!", "B.A.D."]
    JUNK = ['{"secret_db_password": "hunter2",', '"internal_token": "abc123"}', "DB_PASSWORD=hunter2",
            "export TOKEN=abc", "<title>Admin</title>", '<div class="x">', "<!DOCTYPE html>", "<?xml version='1.0'?>",
            "database:", "db_password: hunter2", "postgres://user:pass@db/app", "see http://10.0.0.5:8200/v1/secret",
            "admin@corp.example", "host: 10.0.0.5", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
            "3f9a7b2c4d5e6f708192a3b4c5d6e7f8a9b0c1d2", "12345", "---", "}", 'go_gc_duration_seconds{quantile="0"} 1e-05',
            "C:\\Windows\\System32", "a\tb"]

    def test_looks_like_title(self):
        for t in self.TITLES:
            self.assertTrue(lists.looks_like_title(t), t)
        for t in self.JUNK:
            self.assertFalse(lists.looks_like_title(t), t)

    def test_list_titles(self):
        text = "\n".join(["# my list", *self.TITLES, *self.JUNK[:5]])
        with self.assertLogs("mangarr.lists", "WARNING"):
            titles, skipped = lists.list_titles(text)
        self.assertEqual(titles, self.TITLES)
        self.assertEqual(skipped, 5)
        self.assertEqual(lists.list_titles("[Oshi no Ko]\nBerserk\n"), (["[Oshi no Ko]", "Berserk"], 0))
        self.assertEqual(lists.list_titles(""), ([], 0))
        for body, kind in (('["One Piece", "Berserk"]', "JSON"), ('{"a": 1}', "JSON"), ("\ufeff  {\n", "JSON"),
                           ("<?xml version='1.0'?><titles/>", "a web page or XML"), ("PK\x03\x04\x00\x00", "binary data"),
                           ("\n".join(self.JUNK[2:]), "most of its lines are not titles")):
            with self.assertRaises(lists.ListFetchError, msg=body) as cm, self.assertLogs("mangarr.lists", "WARNING"):
                lists.list_titles(body)
            self.assertIn(kind, str(cm.exception))

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
        with mock.patch.object(lists, "_get_text", lambda url, **kw: text), \
                mock.patch.object(lists.metadata, "lookup", lookup), self.assertLogs("mangarr.lists", "WARNING"):
            lists.fetch_url_text({"url": "http://x"})
        self.assertEqual(len(calls), lists.MAX_LIST_LINES)
        calls.clear()
        with mock.patch.object(lists, "_get_text", lambda url, **kw: text), \
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

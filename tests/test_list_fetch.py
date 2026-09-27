"""A text import list is fetched from a URL anyone may control: the download
is bounded in size and time, redirects stay on the same host, loopback and
link-local addresses are refused before anything is connected to, only
lines that look like titles are looked up, other documents are refused
without quoting them, a body is only taken for a title list once AniList or
MangaDex know its lines (until then nothing of it is quoted and only a few
lines are looked up), the lines are capped, the sync can be cancelled, and
the result quotes only a little."""
import http.server
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from mangarr import db, lists, metadata
from mangarr.model import Series

SECRET_JSON = b'{"secret_db_password": "hunter2",\n"internal_token": "abc123"}\n'
SECRET_CONFIG = b"database:\n  host: db.internal\n  password: hunter2\n  username: admin\n"
SECRET_OCTETS = b"admin:hunter2\nbackup:S3cretPass!\n"


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


def redirect_config(h):
    h.send_response(302)
    h.send_header("Location", "/config")
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
                          "/config": serve(SECRET_CONFIG, "text/plain"),
                          "/octets": serve(SECRET_OCTETS, "application/octet-stream"),
                          "/to-config": redirect_config,
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

    def test_content_types_a_text_file_is_served_with(self):
        # what NAS shares and file hosts send for a .txt / .md / .tsv file
        for ctype in ("", "text/plain; charset=utf-8", "text/markdown", "text/x-markdown", "text/csv",
                      "text/tab-separated-values", "text/yaml", "application/octet-stream", "binary/octet-stream",
                      "application/x-download", "application/force-download", "application/unknown"):
            self.assertIsNone(lists._not_text(ctype), ctype)
        for ctype, what in (("application/json", "JSON"), ("application/problem+json", "JSON"), ("text/html", "a web page"),
                            ("application/xhtml+xml", "a web page"), ("application/rss+xml", "XML"),
                            ("image/png", "not plain text"), ("application/pdf", "not plain text"),
                            ("application/zip", "not plain text"), ("text/css", "not plain text")):
            self.assertEqual(lists._not_text(ctype), what, ctype)


def addrinfo(*ips):
    """A getaddrinfo() that resolves every name to these addresses."""
    def resolve(host, port, *a, **kw):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0)) if ":" in ip
                else (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    return resolve


class FakeSocket:
    connected: list = []

    def __init__(self, *a):
        pass

    def settimeout(self, t):
        pass

    def connect(self, sockaddr):
        FakeSocket.connected.append(sockaddr[0])

    def close(self):
        pass


class AddressTest(ServerTest):
    """Loopback, link-local, multicast and unspecified addresses are refused
    when a list is added, and on every fetch before anything is connected
    to; LAN addresses are allowed."""
    allow = False

    def test_only_loopback_names_this_machine(self):
        # round 3: the docs said "not on the machine mang-arr runs on"; its other addresses (Docker's gateway,
        # its LAN IP) are private addresses like any other and are allowed, as the docs now say
        for url in ("http://172.17.0.1:8080/kv", "http://192.168.1.213:4567/x", "http://[fd00::1]/l.txt"):
            self.assertEqual(lists.validate_params("url_text", {"url": url}), {"url": url})
        for url in ("http://127.0.0.1:8080/kv", "http://localhost:8080/kv", "http://[::1]:8080/kv", "http://0.0.0.0/"):
            with self.assertRaises(ValueError, msg=url):
                lists.validate_params("url_text", {"url": url})

    def test_refused_addresses_are_never_connected_to(self):
        def no_socket(*a, **kw):
            raise AssertionError("a socket was opened for a refused address")
        for ip in ("169.254.169.254", "fe80::1", "fe80::1%eth0", "::ffff:169.254.169.254", "0.0.0.0", "127.0.0.1",
                   "127.0.0.11", "127.0.1.1", "::1", "::ffff:127.0.0.1", "224.0.0.251", "ff02::1"):
            with mock.patch.object(lists.socket, "getaddrinfo", addrinfo(ip)), \
                    mock.patch.object(lists.socket, "socket", no_socket), \
                    self.assertRaises(lists.ListFetchError, msg=ip) as cm, self.assertLogs("mangarr.lists", "WARNING"):
                lists._connect(("list.example", 80), 5)
            self.assertIn("refusing to fetch a list from", str(cm.exception))
        FakeSocket.connected = []
        for ip in ("192.168.1.10", "10.0.0.2", "172.17.0.1", "fd00::5", "93.184.216.34"):
            with mock.patch.object(lists.socket, "getaddrinfo", addrinfo(ip)), \
                    mock.patch.object(lists.socket, "socket", FakeSocket):
                lists._connect(("nas.lan", 80), 5)                    # a NAS on the LAN is the normal case
        self.assertEqual(FakeSocket.connected, ["192.168.1.10", "10.0.0.2", "172.17.0.1", "fd00::5", "93.184.216.34"])
        FakeSocket.connected = []
        with mock.patch.object(lists.socket, "getaddrinfo", addrinfo("::1", "192.168.1.10")), \
                mock.patch.object(lists.socket, "socket", FakeSocket):
            lists._connect(("nas.lan", 80), 5)
        self.assertEqual(FakeSocket.connected, ["192.168.1.10"])      # only the address that passed

    def test_a_name_for_loopback_is_no_port_scanner(self):
        # the review's note: through a name resolving to 127.x, an open port
        # gave 'refusing ... loopback' (checked after connect) and a closed
        # one 'the connection was refused'
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        closed_port = closed.getsockname()[1]
        closed.close()
        real = socket.getaddrinfo

        def resolve(host, *a, **kw):
            return real("127.0.0.1" if host == "lists.internal" else host, *a, **kw)
        msgs = []
        with mock.patch.object(lists.socket, "getaddrinfo", resolve):
            for port in (self.server.server_address[1], closed_port):
                with self.assertRaises(lists.ListFetchError) as cm, self.assertLogs("mangarr.lists", "WARNING"):
                    lists._get_text(f"http://lists.internal:{port}/secret")
                msgs.append(str(cm.exception))
        self.assertEqual(msgs, ["refusing to fetch a list from 127.0.0.1 (a loopback address)"] * 2)
        self.assertEqual(Handler.served, [])

    def test_loopback_server_is_refused_at_connect(self):
        # a list stored before the check, or a host name that resolves to
        # 127.0.0.1: refused before connect, so before a request is sent
        with self.assertRaises(lists.ListFetchError) as cm, self.assertLogs("mangarr.lists", "WARNING"):
            lists._get_text(self.base + "/secret")
        self.assertIn("loopback", str(cm.exception))
        self.assertEqual(Handler.served, [])
        with tempfile.TemporaryDirectory() as tmp, db.connect(os.path.join(tmp, "l.db")) as con:
            lid = lists.add_list(con, "L", "url_text", {"url": self.base + "/secret"})
            with mock.patch.object(lists.metadata, "lookup", lambda t, **kw: self.fail(f"looked up {t!r}")), \
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
    """What a list URL returns is sent to AniList and MangaDex only line by
    line and only when the line looks like a title, and is shown on /lists
    only once AniList or MangaDex know most of its lines."""
    SECRETS = ("hunter2", "db.internal", "admin", "S3cret", "abc123", "Mystery")

    def _sync(self, path, known=(), similar=()):
        """(last_result, lines looked up); lines in `known` are confident
        picks, AniList suggests a series of nearly that title for lines in
        `similar`. Nothing secret may reach last_result or any log line."""
        looked_up = []

        def lookup(t, unreached=None):
            looked_up.append(t)
            like = [Series(anilist_id=900 + len(looked_up), english=f"{t}s")] if t in similar else []
            return (Series(anilist_id=len(looked_up), english=t) if t in known else None), like
        with tempfile.TemporaryDirectory() as tmp, db.connect(os.path.join(tmp, "l.db")) as con:
            lid = lists.add_list(con, "L", "url_text", {"url": self.base + path})
            with mock.patch.object(lists.metadata, "lookup", lookup), self.assertLogs("mangarr", "DEBUG") as logs:
                msg = lists.sync(con, lists.get_list(con, lid), lambda *a: None)
            self.assertEqual(lists.get_list(con, lid)["last_result"], msg)
        logged = "\n".join(logs.output)
        for secret in self.SECRETS:
            self.assertNotIn(secret, logged)
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
        msg, looked_up = self._sync("/mixed", known={"One Piece"}, similar={"Berserk"})
        self.assertEqual(looked_up, ["One Piece", "Berserk"])
        self.assertEqual(msg, "1 fetched, 1 added, 1 review, 1 line(s) skipped (not titles); needs review: Berserk")

    def test_text_answers_that_look_like_titles_are_never_quoted(self):
        # the verifier's repro, with loopback standing in for a LAN or
        # container-network host: a config file as text/plain, key:value as
        # octet-stream, and the config again behind a same-host redirect.
        # Every line passes as a title, and none is a series anyone knows.
        for path, n in (("/config", 3), ("/octets", 2), ("/to-config", 3)):
            msg, looked_up = self._sync(path)
            self.assertEqual(msg, f"error: the URL did not return a title list: none of the {n} line(s) looked up "
                                  "is a series AniList or MangaDex knows", path)
            self.assertEqual(len(looked_up), n)
            for secret in self.SECRETS:
                self.assertNotIn(secret, msg)

    def test_an_unknown_text_answer_is_looked_up_only_a_little(self):
        body = "".join(f"Status line {i} of the service\n" for i in range(40)).encode()
        with mock.patch.dict(Handler.routes, {"/status": serve(body, "text/plain")}):
            msg, looked_up = self._sync("/status")
        self.assertEqual(len(looked_up), lists.PROBE_LINES)
        self.assertTrue(msg.startswith(f"error: the URL did not return a title list: none of the {lists.PROBE_LINES} "
                                       "line(s)"), msg)

    def test_a_list_mostly_unknown_is_reported_by_line_number(self):
        body = b"One Piece\nMystery A\nBerserk\nMystery B\nMystery C\nMystery D\n"
        with mock.patch.dict(Handler.routes, {"/weak": serve(body, "text/plain")}):
            msg, looked_up = self._sync("/weak", known={"One Piece", "Berserk"})
        self.assertEqual(len(looked_up), 6)
        self.assertEqual(msg, "2 fetched, 2 added, 4 review; needs review: line 2 | line 4 | line 5 | line 6 "
                              "(not quoted: fewer than half of the lines looked up are series AniList or MangaDex "
                              "knows)")


class ProviderAndProbeTest(ServerTest):
    """Round 3: a text list is judged only on what AniList and MangaDex answered, a few unrecognised lines
    at its start do not throw the rest away, and a review line is quoted only when it is nearly the title of a
    series the providers suggested for it."""

    def sync(self, body: bytes, lookup=None) -> tuple[str, list]:
        """(last_result, series handed to the add job) of a sync of `body`, with lists.metadata.lookup
        replaced by `lookup` when given."""
        added = []
        with mock.patch.dict(Handler.routes, {"/l": serve(body, "text/plain")}), \
                tempfile.TemporaryDirectory() as tmp, db.connect(os.path.join(tmp, "l.db")) as con:
            lid = lists.add_list(con, "L", "url_text", {"url": self.base + "/l"})
            with mock.patch.object(lists.metadata, "lookup", lookup or metadata.lookup), \
                    self.assertLogs("mangarr", "INFO"):
                msg = lists.sync(con, lists.get_list(con, lid), lambda s, *a: added.append(s.title))
            self.assertEqual(lists.get_list(con, lid)["last_result"], msg)          # the list itself stays
        return msg, added

    def test_providers_down_is_try_again_later_not_a_non_list(self):
        """The verifier's repro (medium): with AniList and MangaDex down, a valid list was 'not a title list:
        none of the 5 line(s) looked up is a series AniList or MangaDex knows', because the metadata lookup
        swallowed every provider error."""
        def down(q, **kw):
            raise OSError("network down")

        def knows(q, **kw):
            return [Series(mangadex_id=f"md-{q}", english=q)]
        body = b"One Piece\nNaruto\nBleach\nBerserk\nMonster\nVagabond\n"
        unreachable = "error: " + lists.UNREACHABLE
        for al, md in ((down, down), (down, lambda q, **kw: [])):          # one down, the other no verdict
            with mock.patch.object(metadata.anilist, "search", al), mock.patch.object(metadata.mangadex, "search", md):
                msg, added = self.sync(body)
            self.assertEqual((msg, added), (unreachable, []))
        self.assertIn("try again later", unreachable)
        self.assertNotIn(lists.NOT_A_LIST, unreachable)
        with mock.patch.object(metadata.anilist, "search", down), mock.patch.object(metadata.mangadex, "search", knows):
            msg, added = self.sync(body)                                    # MangaDex alone knows them all
        self.assertEqual((msg, len(added)), ("6 fetched, 6 added", 6))
        with mock.patch.object(metadata.anilist, "search", lambda q, **kw: []), \
                mock.patch.object(metadata.mangadex, "search", lambda q, **kw: []):
            msg, added = self.sync(body)                                    # both answered: a verdict
        self.assertTrue(msg.startswith(f"error: {lists.NOT_A_LIST}: none of the 6 line(s)"), msg)

    def test_unknown_lines_first_do_not_throw_the_good_ones_away(self):
        """The verifier's repro: five misspelt titles before the good ones made the whole list fail."""
        def lookup(t, unreached=None):
            return (Series(mangadex_id=f"md-{t}", english=t) if t in ("One Piece", "Naruto") else None), \
                [Series(anilist_id=1000 + len(t), english=t + " Gaiden")]
        msg, added = self.sync(b"One Peice\nNaurto\nBleech\nBerserkk\nMonstr\nOne Piece\nNaruto\n", lookup)
        self.assertEqual(added, ["One Piece", "Naruto"])
        self.assertTrue(msg.startswith("2 fetched, 2 added, 5 review; needs review: line 1 | line 2 | line 3 | "
                                       "line 4 | line 5 (not quoted"), msg)
        looked = []
        body = "".join(f"Unknown {chr(65 + i)}\n" for i in range(lists.PROBE_LINES)).encode() + b"One Piece\n"
        msg, added = self.sync(body, lambda t, **kw: looked.append(t) or lookup(t))
        self.assertEqual((len(looked), added), (lists.PROBE_LINES, []))  # the probe still ends somewhere
        self.assertTrue(msg.startswith(f"error: {lists.NOT_A_LIST}"), msg)

    def test_a_review_line_is_quoted_only_when_it_is_nearly_a_known_title(self):
        """The verifier's repro: once at least half of the lines looked up were known titles, every other
        line was quoted in last_result, secrets included. Then a line was quoted as soon as AniList or
        MangaDex suggested any series for it, and their search is fuzzy. A line is now quoted only when it
        is nearly the title of a series they suggested (a misspelt or ambiguous title), else given by
        number."""
        known = {"One Piece", "Naruto", "Bleach"}
        fuzzy = {"root password hunter2": ["Root", "Password"], "vault token s.abcdef": ["Vault of Stars"],
                 "Berserkk": ["Berserk", "Berserk: The Prototype"]}

        def lookup(t, unreached=None):
            if t in known:
                return Series(mangadex_id=f"md-{t}", english=t), []
            return None, [Series(anilist_id=90 + i, english=c) for i, c in enumerate(fuzzy.get(t, []))]
        body = b"One Piece\nNaruto\nBleach\nroot password hunter2\nvault token s.abcdef\nBerserkk\n"
        for suggest in (False, True):           # the secrets without any suggestion, then with fuzzy ones
            with mock.patch.dict(fuzzy, {} if suggest else {"root password hunter2": [], "vault token s.abcdef": []}):
                msg, added = self.sync(body, lookup)
            self.assertEqual(msg, "3 fetched, 3 added, 3 review; needs review: line 4 | line 5 | Berserkk (lines not "
                                  "close to the title of a series AniList or MangaDex suggested are given by number)")
            self.assertNotIn("hunter2", msg)

    def test_one_provider_down_is_not_a_verdict(self):
        """The verifier's repro: with MangaDex unreachable and AniList working, 'One Peice\\nNaruto' failed
        with 'try again later', and 'Naruto\\nOne Peice' reported line 2 as a line AniList and MangaDex found
        nothing like, although MangaDex was never asked."""
        def md_down(q, **kw):
            raise OSError("network down")

        def anilist(q, **kw):
            if q == "Naruto":
                return [Series(anilist_id=1, english="Naruto")]
            if q == "One Peice":
                return [Series(anilist_id=2, english="One Piece"), Series(anilist_id=3, english="One Punch-Man")]
            return [Series(anilist_id=4, english="Wanted!")] if q == "Mystery Webtoon" else []
        with mock.patch.object(metadata.anilist, "search", anilist), \
                mock.patch.object(metadata.mangadex, "search", md_down):
            for body in (b"One Peice\nNaruto\n", b"Naruto\nOne Peice\n"):
                msg, added = self.sync(body)
                self.assertEqual((msg, added), ("1 fetched, 1 added, 1 review; needs review: One Peice", ["Naruto"]))
            # AniList suggests nothing close and MangaDex was not asked: not reviewed, looked up again next time
            msg, added = self.sync(b"Naruto\nMystery Webtoon\nOne Peice\n")
            self.assertEqual(msg, "1 fetched, 1 added, 1 review, 1 line(s) not checked (AniList or MangaDex could not "
                                  "be reached; the next sync tries again); needs review: One Peice")
            # nothing known in the probe and MangaDex not asked about the lines: no verdict either way
            msg, added = self.sync(b"Mystery Webtoon\nAnother One\n")
            self.assertEqual((msg, added), ("error: " + lists.UNREACHABLE, []))

    def test_both_down_after_the_list_is_known(self):
        """A line neither provider answered for, once the list proved to be one, is counted for the next sync,
        not reported as a line nothing is like."""
        calls = []

        def lookup(t, unreached=None):
            calls.append(t)
            if t == "Bleach":
                raise metadata.LookupError_("AniList and MangaDex could not be reached")
            return Series(anilist_id=len(calls), english=t), []
        msg, added = self.sync(b"One Piece\nBleach\nNaruto\n", lookup)
        self.assertEqual(calls, ["One Piece", "Bleach", "Naruto"])
        self.assertEqual((msg, added), ("2 fetched, 2 added, 1 line(s) not checked (AniList or MangaDex could not be "
                                        "reached; the next sync tries again)", ["One Piece", "Naruto"]))


class LinesTest(unittest.TestCase):
    TITLES = ["One Piece", "Re:Zero kara Hajimeru Isekai Seikatsu", "Kaguya-sama: Love Is War", "[Oshi no Ko]",
              '"Oshi no Ko"', "Ajin: Demi-Human", "1+2=Paradise", "Steins;Gate", ".hack//G.U.+", "Dr. STONE",
              "Yu-Gi-Oh!", "Is It Wrong to Try to Pick Up Girls in a Dungeon?", "100% Perfect Girl", "Tokyo Ghoul:re",
              "Hunter × Hunter", "葬送のフリーレン", "나 혼자만 레벨업", "<Infinite Dendrogram>", "Kaiju No. 8",
              "Zom 100: Bucket List of the Dead", "magi: the labyrinth of magic", "JoJo's Bizarre Adventure Part 7: Steel Ball Run",
              "anilist:30013", "mangadex:12345678-1234-1234-1234-123456789abc", "Me & Roboco", "Haikyu!!", "B.A.D.",
              "86", "Love=Game", "Sword Art Online: Progressive", "citrus", "Fate/Zero", "2001 Nights"]
    JUNK = ['{"secret_db_password": "hunter2",', '"internal_token": "abc123"}', "DB_PASSWORD=hunter2",
            "export TOKEN=abc", "<title>Admin</title>", '<div class="x">', "<!DOCTYPE html>", "<?xml version='1.0'?>",
            "database:", "db_password: hunter2", "postgres://user:pass@db/app", "see http://10.0.0.5:8200/v1/secret",
            "admin@corp.example", "host: 10.0.0.5", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
            "3f9a7b2c4d5e6f708192a3b4c5d6e7f8a9b0c1d2", "12345", "---", "}", 'go_gc_duration_seconds{quantile="0"} 1e-05',
            "C:\\Windows\\System32", "password=hunter2", "root:x:0:0:root:/root:/bin/bash", "aa:bb:cc:dd:ee:ff"]

    def test_looks_like_title(self):
        for t in self.TITLES:
            self.assertTrue(lists.looks_like_title(t), t)
        for t in self.JUNK:
            self.assertFalse(lists.looks_like_title(t), t)

    def test_list_titles(self):
        text = "\n".join(["# my list", *self.TITLES, *self.JUNK[:5]])
        with self.assertLogs("mangarr.lists", "WARNING"):
            titles, skipped = lists.list_titles(text)
        self.assertEqual([ln.text for ln in titles], self.TITLES)
        self.assertEqual(titles[0].no, 2)                          # line numbers count every line, from 1
        self.assertEqual(skipped, 5)
        self.assertEqual(lists.list_titles("[Oshi no Ko]\nBerserk\n"),
                         ([lists.Line(1, "[Oshi no Ko]"), lists.Line(2, "Berserk")], 0))
        self.assertEqual(lists.list_titles(""), ([], 0))
        # a title in angle brackets first is not a web page; a tab is a space
        self.assertEqual(lists.list_titles("<Infinite Dendrogram>\nOne Piece\tReading\n"),
                         ([lists.Line(1, "<Infinite Dendrogram>"), lists.Line(2, "One Piece Reading")], 0))
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

        def lookup(t, unreached=None):
            calls.append(t)
            return Series(anilist_id=len(calls), english=t), []
        with mock.patch.object(lists, "_get_text", lambda url, **kw: text), \
                mock.patch.object(lists.metadata, "lookup", lookup), self.assertLogs("mangarr.lists", "WARNING"):
            lists.fetch_url_text({"url": "http://x"})
        self.assertEqual(len(calls), lists.MAX_LIST_LINES)
        calls.clear()
        with mock.patch.object(lists, "_get_text", lambda url, **kw: text), \
                mock.patch.object(lists.metadata, "lookup", lookup), self.assertLogs("mangarr.lists", "INFO"):
            lists.fetch("url_text", {"url": "http://x"}, should_cancel=lambda: len(calls) >= 3)
        self.assertEqual(len(calls), 3)

    def test_an_exact_title_counts_as_known(self):
        # several series of exactly that title: no confident pick, but a title
        text = "Berserk\nMystery\n"
        twins = [Series(anilist_id=1, english="Berserk"), Series(anilist_id=2, english="Berserk")]
        lookup = {"Berserk": (None, twins), "Mystery": (None, [Series(anilist_id=3, english="Mysteria")])}
        with mock.patch.object(lists, "_get_text", lambda url, **kw: text), \
                mock.patch.object(lists.metadata, "lookup", lambda t, **kw: lookup[t]):
            fetched = lists.fetch_url_text({"url": "http://x"})
        self.assertEqual((fetched.review, fetched.quoted), (["Berserk", "Mystery"], True))

    def test_filters_are_linear(self):
        # the verifier's repro: repeated 300-character word lines took 6.5 s
        # and 7.5 s per MB (quadratic backtracking, and each repeat judged again)
        bodies = [("a" * 300 + "\n") * 3300, ("x_" * 150 + "\n") * 3300]
        # ... and distinct lines of every shape a pattern could stall on
        for unit in ("a", "x_", "a.", "1.", '"', "<a ", "a:", "a@", "a@a.", "a://", "a="):
            bodies.append("".join(unit * (295 // len(unit)) + f"{i:05d}\n" for i in range(3300)))
        with mock.patch.object(lists.log, "warning"):
            for body in bodies:
                t0 = time.perf_counter()
                lists._split(body, lists.MAX_LIST_LINES)
                self.assertLess(time.perf_counter() - t0, 1.5, body[:20])    # about 0.2 s at worst
        judged = []
        real = lists.looks_like_title
        with mock.patch.object(lists, "looks_like_title", lambda t: judged.append(t) or real(t)):
            lists._split(("a" * 300 + "\n") * 3300)
        self.assertEqual(len(judged), 1)                          # a repeated line is judged once

    def test_a_failed_provider_does_not_log_the_query(self):
        def boom(q):
            raise RuntimeError("HTTP Error 503: Service Unavailable")
        with mock.patch.object(metadata.anilist, "search", boom), \
                mock.patch.object(metadata.mangadex, "search", lambda q: []), \
                self.assertLogs("mangarr.metadata", "WARNING") as cm:
            metadata.lookup("password: hunter2")
        self.assertIn("503", cm.output[0])
        self.assertNotIn("hunter2", "\n".join(cm.output))


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

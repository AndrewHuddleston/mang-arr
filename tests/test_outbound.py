"""Outbound HTTP (outbound.py) and its users komga/updates: http(s) only, no
redirects for credentialed calls, a hard deadline, capped answers; plus the
save-time checks for URL settings and smtp_security. Only 127.0.0.1 servers."""
import json
import socket
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from mangarr import komga, notify, outbound, settings, updates


class _Server:
    """A local HTTP server; `routes` maps path -> callable(handler) and every
    request is recorded as (method, path, headers, body)."""

    def __init__(self, routes):
        self.requests = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                srv.requests.append((self.command, self.path, dict(self.headers), body))
                route = routes.get(self.path.split("?")[0])
                if route is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                route(self)
            do_GET = do_POST = _any

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _reply(status=200, body=b"{}", headers=None):
    def route(h):
        h.send_response(status)
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
    return route


def _trickle_server(prefix: bytes = b""):
    """A raw server that sends `prefix` then one byte every 0.2 s for ever."""
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
                c.sendall(prefix)
                while not stop.is_set():
                    c.sendall(b"x")
                    time.sleep(0.2)
            except OSError:
                pass
            finally:
                c.close()
    threading.Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{ls.getsockname()[1]}", lambda: (stop.set(), ls.close())


class OutboundTest(unittest.TestCase):
    def setUp(self):
        self.target = _Server({"/landing": _reply(200, b"<html>login page</html>")})
        self.origin = _Server({
            "/hook": _reply(302, b"", {"Location": self.target.url + "/landing"}),
            "/same": _reply(302, b"", {"Location": "/ok"}),
            "/ok": _reply(200, b'{"ok": true}'),
            "/to-ftp": _reply(302, b"", {"Location": "ftp://127.0.0.1/x"}),
            "/big": _reply(200, b"x" * 5000),
        })

    def tearDown(self):
        self.origin.close()
        self.target.close()

    def test_only_http_and_https(self):
        for url in ("file:///etc/hostname", "ftp://127.0.0.1/x", "data:text/plain,hi", "gopher://x/", "http:///nohost",
                    "/relative"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                outbound.fetch(url)

    def test_credentialed_post_redirect_is_refused_not_downgraded(self):
        with self.assertRaises(urllib.error.HTTPError) as cm, self.assertLogs("mangarr.outbound", "WARNING") as logs:
            outbound.fetch(self.origin.url + "/hook", b"{}", {"X-Gotify-Key": "SECRET"}, method="POST")
        self.assertIn("refused to follow HTTP 302", logs.output[0])
        self.assertEqual(cm.exception.code, 302)
        self.assertIn("redirected to " + self.target.url + "/landing", str(cm.exception.reason))
        self.assertEqual(self.target.requests, [])              # the key never reached the other host

    def test_followed_redirect_drops_credentials_across_hosts(self):
        status, body = outbound.fetch(self.origin.url + "/hook", headers={"X-Api-Key": "K", "Authorization": "Bearer T",
                                                                         "User-Agent": "ua"}, follow_redirects=True)
        self.assertEqual((status, body), (200, b"<html>login page</html>"))
        sent = self.target.requests[0][2]
        self.assertNotIn("X-Api-Key", sent)
        self.assertNotIn("Authorization", sent)
        self.assertEqual(sent["User-Agent"], "ua")
        # same host: the key is still sent (it is the same server)
        status, _ = outbound.fetch(self.origin.url + "/same", headers={"X-Api-Key": "K"}, follow_redirects=True)
        self.assertEqual(status, 200)
        self.assertEqual(self.origin.requests[-1][2].get("X-Api-Key"), "K")

    def test_redirect_to_ftp_refused_even_when_following(self):
        with self.assertRaises(urllib.error.HTTPError) as cm, self.assertLogs("mangarr.outbound", "WARNING"):
            outbound.fetch(self.origin.url + "/to-ftp", follow_redirects=True)
        self.assertIn("non-http(s)", str(cm.exception.reason))

    def test_answer_size_capped(self):
        with self.assertRaises(ValueError), self.assertLogs("mangarr.outbound", "WARNING"):
            outbound.fetch(self.origin.url + "/big", max_bytes=1000)
        self.assertEqual(len(outbound.fetch(self.origin.url + "/big", max_bytes=5000)[1]), 5000)

    def test_trickling_server_hits_the_deadline(self):
        for prefix in (b"", b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n"):     # in the headers, in the body
            url, stop = _trickle_server(prefix)
            try:
                t0 = time.monotonic()
                with self.subTest(prefix=prefix), self.assertRaises(TimeoutError):
                    outbound.fetch(url, timeout=1.0)          # each byte comes well within the socket timeout
                self.assertLess(time.monotonic() - t0, 3.0)
            finally:
                stop()


    def test_connect_that_outlasts_the_deadline_is_not_used(self):
        # a slow DNS answer or a dead first address: the socket appears only after the deadline, and the
        # server then trickles; the per-read timeout alone would let this run for ever
        url, stop = _trickle_server(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
        real = socket.create_connection

        def slow_connect(*a, **kw):
            time.sleep(1.5)
            return real(*a, **kw)
        result = []

        def call():
            try:
                outbound.fetch(url, timeout=1.0)
                result.append("returned")
            except Exception as e:
                result.append(e)
        try:
            with mock.patch.object(outbound.http.client.socket, "create_connection", slow_connect):
                t = threading.Thread(target=call, daemon=True)
                t.start()
                t.join(5)
            self.assertFalse(t.is_alive(), "fetch ran on past its deadline")
            self.assertIsInstance(result[0], TimeoutError)
        finally:
            stop()

    def test_watchdog_catches_a_socket_created_after_the_deadline(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        box = []
        wd = outbound.Watchdog(time.monotonic() + 0.1, lambda: box[0] if box else None).start()
        self.addCleanup(wd.cancel)
        time.sleep(0.3)                           # the deadline passed with no socket
        box.append(a)
        a.settimeout(3)
        t0 = time.monotonic()
        self.assertEqual(a.recv(10), b"")         # shut down, not left to block
        self.assertLess(time.monotonic() - t0, 1.0)


class NotifyChannelTest(unittest.TestCase):
    """The real notify._post against local servers."""

    def setUp(self):
        self.target = _Server({"/message": _reply(200, b"<html>web UI</html>")})
        self.origin = _Server({"/message": _reply(302, b"", {"Location": self.target.url + "/message"})})

    def tearDown(self):
        self.origin.close()
        self.target.close()

    def send(self, **kw):
        v = dict(settings.DEFAULTS)
        v.update({k: "" for k, d in settings.DEFAULTS.items() if isinstance(d, str)}, **kw)
        with mock.patch.object(notify.settings, "all_values", lambda: v), \
             self.assertLogs("mangarr.notify", "ERROR"):
            return notify.send_detailed("T", "B", "test", force=True)

    def test_redirected_notification_is_a_failure_and_keeps_the_token(self):
        res = self.send(gotify_url=self.origin.url, gotify_token="GOTIFY-SECRET", ntfy_url=self.origin.url + "/message",
                        ntfy_token="NTFY-SECRET")
        self.assertIn("redirected to " + self.target.url, res["gotify"])
        self.assertIn("redirected to " + self.target.url, res["ntfy"])
        self.assertEqual(self.target.requests, [])

    def test_file_webhook_is_not_sent(self):
        res = self.send(webhook_url="file:///etc/hostname")
        self.assertIn("ValueError", res["webhook"])


class KomgaTest(unittest.TestCase):
    def setUp(self):
        self.srv = _Server({"/api/v1/libraries": _reply(302, b"", {"Location": "http://127.0.0.1:1/login"})})

    def tearDown(self):
        self.srv.close()

    def values(self, url):
        v = dict(settings.DEFAULTS, komga_url=url, komga_api_key="KOMGA-KEY")
        return mock.patch.object(komga.settings, "all_values", lambda: v)

    def test_redirect_reported_not_followed(self):
        with self.values(self.srv.url), self.assertLogs("mangarr.outbound", "WARNING"):
            ok, msg = komga.test()
        self.assertFalse(ok)
        self.assertIn("redirected to http://127.0.0.1:1/login", msg)

    def test_file_url_refused(self):
        for url in ("file:///etc/hostname#", "file:///nonexistent"):
            with self.subTest(url=url), self.values(url):
                ok, msg = komga.test()
                self.assertFalse(ok)
                self.assertIn("must be an http:// or https:// URL", msg)   # same answer: no file-existence oracle


class UpdatesTest(unittest.TestCase):
    def test_follows_redirect_and_ignores_non_https_link(self):
        rel = json.dumps({"tag_name": "v99.0.0", "html_url": "javascript:alert(1)"}).encode()
        srv = _Server({"/old": _reply(301, b"", {"Location": "/new"}), "/new": _reply(200, rel)})
        try:
            with mock.patch.object(updates, "RELEASES_URL", srv.url + "/old"):
                s = updates.check(force=True)
        finally:
            srv.close()
        self.assertEqual((s["latest"], s["url"], s["error"]), ("99.0.0", None, None))


class SettingsValidationTest(unittest.TestCase):
    def test_url_settings_http_only(self):
        for key in settings.URL_KEYS:
            with self.subTest(key=key):
                self.assertEqual(settings._coerce(key, " https://x.example/a "), "https://x.example/a")
                self.assertEqual(settings._coerce(key, ""), "")
                for bad in ("file:///etc/hostname#", "ftp://h/x", "data:,x", "komga:25600"):
                    with self.assertRaises(ValueError), self.assertLogs("mangarr.settings", "WARNING"):
                        settings._coerce(key, bad)

    def test_smtp_security_fails_closed(self):
        self.assertEqual(settings._coerce("smtp_security", " STARTTLS "), "starttls")
        self.assertEqual(settings._coerce("smtp_security", "SSL"), "ssl")
        self.assertEqual(settings._coerce("smtp_security", "none"), "none")
        for bad in ("tls", "TLS", "plain", "start-tls"):
            with self.subTest(bad=bad), self.assertRaises(ValueError), self.assertLogs("mangarr.settings", "WARNING"):
                settings._coerce("smtp_security", bad)


if __name__ == "__main__":
    unittest.main()

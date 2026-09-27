"""Web security regressions: output escaping, CSRF / Host checks, login
hardening, sessions, fail-closed settings, body limits, open-endpoint
disclosure, short Suwayomi timeouts and the health cache."""
import asyncio
import http.cookiejar
import logging
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

try:
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError):      # web extras or httpx not installed
    TestClient = None

from mangarr import model
from mangarr.suwayomi import SuwayomiError

XSS_TITLE = 'X" autofocus onfocus="alert(document.domain)" x="'


def _offline(*a, **k):
    raise SuwayomiError("offline in tests")


def no_network_patches(calls: list) -> list:
    """Never reach the outside from web tests, even from the background
    health thread: notifications and the Komga test are faked, and any
    other outbound connection attempt fails and is recorded in `calls`
    (tearDown asserts it stays empty)."""
    def blocked(host, *a, **k):
        calls.append((host, threading.current_thread().name))
        raise OSError(f"network disabled in tests ({host})")
    return [mock.patch("mangarr.notify.send", return_value=None),
            mock.patch("mangarr.notify.send_detailed", return_value={}),
            mock.patch("mangarr.komga.test", return_value=(True, "faked in tests")),
            mock.patch("socket.getaddrinfo", blocked),
            mock.patch("socket.create_connection", lambda addr, *a, **k: blocked(addr[0]))]


class _FakeSource:
    def __init__(self, name):
        self.id, self.name, self.lang, self.unusable, self.throttled = "1", name, "en", False, False


@unittest.skipIf(TestClient is None, "web extras not installed")
class WebBase(unittest.TestCase):
    """A temporary database, no Suwayomi, no network, fast password hashing."""
    login = None           # (user, password) to enable forms login in setUp

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from mangarr import health, settings
        from mangarr.web import app as web
        from mangarr.web import lists_routes
        try:                                  # optional, so these tests fail on a version without the fixes
            from mangarr.web import security  # for what they check, not with an ImportError in setUp
        except ImportError:
            security = None
        self.patches = [mock.patch("mangarr.config.DATA_DIR", self.tmp.name),
                        mock.patch("mangarr.config.DB_PATH", os.path.join(self.tmp.name, "t.db")),
                        mock.patch("mangarr.config.STAGING_ROOT", self.tmp.name),
                        mock.patch("mangarr.config.LIBRARY_ROOT", self.tmp.name),
                        mock.patch.object(web.client, "gq", _offline),
                        mock.patch.object(web.client, "sources", lambda *a, **k: [_FakeSource("Weeb Central")]),
                        mock.patch.object(health, "_ping", lambda name, url, timeout=8: health.Check("ok", name, "x")),
                        mock.patch.dict(health._cache, {"at": 0.0, "checks": []}),
                        mock.patch.object(settings, "PBKDF2_ITERATIONS", 1000, create=True),
                        mock.patch.object(lists_routes, "_app", web)]
        if security is not None:
            self.patches += [mock.patch.object(security, "throttle", security.Throttle()),
                             mock.patch.object(security, "log_budget", security.LogBudget()),
                             mock.patch.object(security, "_verified", {})]
        if hasattr(web, "asyncio"):
            self.patches.append(mock.patch("mangarr.web.app.asyncio.sleep", self._no_sleep))
        self.net_calls: list = []
        self.patches += no_network_patches(self.net_calls)
        for p in self.patches:
            p.start()
        settings._cache.clear()
        from mangarr import db
        with db.connect() as con:
            getattr(settings, "ensure_security", settings.ensure_api_key)(con)
            if self.login:
                settings.set_many(con, {"auth_user": self.login[0], "auth_password": self.login[1],
                                        "auth_method": "forms"})
            self.key = settings.all_values(con)["api_key"]
        self.web, self.settings, self.security, self.health = web, settings, security, health
        self.client = TestClient(web.app)

    @staticmethod
    async def _no_sleep(s):
        return None

    def tearDown(self):
        self.client.close()
        from mangarr import health
        running = getattr(health, "_running", None)  # a health run still going: let it end while the fakes are in place
        if running is not None:
            running.wait(30)
        for p in reversed(self.patches):
            p.stop()
        from mangarr import settings
        settings._cache.clear()
        self.tmp.cleanup()
        self.assertEqual(self.net_calls, [], "a web test tried to reach the network")

    def set(self, **values):
        from mangarr import db
        with db.connect() as con:
            return self.settings.set_many(con, values)

    def signin(self, user="andy", password="pw"):
        r = self.client.post("/login", data={"username": user, "password": password}, follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text[:200])
        return r


class EscapingTest(WebBase):
    """#0, #16, #58, #60, #61: nothing attacker-reachable is printed raw."""

    def test_series_title_in_wanted_checkbox_is_escaped(self):
        from mangarr import db
        with db.connect() as con:
            sid = db.upsert_series(con, model.manual(XSS_TITLE))
            con.execute("INSERT INTO chapter (series_id, number, status, updated_at) VALUES (?, 1, 'wanted', 'x')",
                        (sid,))
        html = self.client.get("/wanted").text
        self.assertNotIn('autofocus onfocus="alert', html)
        self.assertIn('aria-label="Select X&#34; autofocus onfocus=&#34;alert(document.domain)&#34; x=&#34;"', html)
        self.assertIn(f'data-select="{sid}"', html)

    def test_source_name_in_settings_checkbox_is_escaped(self):
        with mock.patch.object(self.web.client, "sources", lambda *a, **k: [_FakeSource(XSS_TITLE)]):
            html = self.client.get("/settings").text
        self.assertNotIn('autofocus onfocus="alert', html)
        self.assertIn('aria-label="Enable X&#34; autofocus', html)
        self.assertNotIn("onchange=", html)                  # inline handlers moved to app.js (CSP)
        self.assertIn('data-hides="order-warning"', html)

    def test_cover_url_cannot_break_out_of_css(self):
        from mangarr import db
        cover = "https://x/a.jpg');position:fixed;top:0;background:url('https://evil/p.png"
        with db.connect() as con:
            sid = db.upsert_series(con, model.manual("Covered"))
            con.execute("UPDATE series SET cover=? WHERE id=?", (cover, sid))
        html = self.client.get(f"/series/{sid}").text
        self.assertNotIn("');position:fixed", html)
        self.assertIn("background-image:url('https://x/a.jpg%27%29;position:fixed", html)
        from mangarr.web import views
        self.assertEqual(views.css_url("javascript:alert(1)"), "")

    def test_flash_only_shows_messages_the_server_signed(self):
        html = self.client.get("/?m=Security+update:+re-enter+your+password+at+http://evil").text
        self.assertNotIn("Security update", html)
        r = self.client.post("/wanted/search", follow_redirects=False)
        self.assertIn("ms=", r.headers["location"])
        self.assertIn('class="alert success flash"', self.client.get(r.headers["location"]).text)
        html = self.client.get("/settings?komga_test=1:all+good&komga_ok=1").text
        self.assertNotIn("all good", html)

    def test_exclusion_ref_is_one_path_segment(self):
        from mangarr import db, lists
        ref = "manual:../../../../series/5"
        with db.connect() as con:
            lists.add_exclusion(con, ref, "t", "test")
        html = self.client.get("/lists").text
        self.assertIn('action="/lists/exclusions/manual%3A..%2F..%2F..%2F..%2Fseries%2F5/delete"', html)
        self.assertEqual(self.client.post("/lists/exclusions/manual%3A..%2F..%2F..%2F..%2Fseries%2F5/delete",
                                          follow_redirects=False).status_code, 303)
        with db.connect() as con:
            self.assertEqual([r["ref"] for r in lists.exclusions(con)], [])

    def test_no_inline_script_and_csp_header(self):
        import glob
        for path in glob.glob(os.path.join(os.path.dirname(self.web.__file__), "templates", "*.html")):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("|safe", text, path)
            self.assertNotRegex(text, r"\son(submit|change|click)=", path)
            self.assertNotIn("<script>", text, path)
        r = self.client.get("/")
        self.assertIn("script-src 'self'", r.headers["content-security-policy"])
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")


class CsrfHostTest(WebBase):
    """#12-#15, #45-#48, #52: cross-site writes and foreign Host names are refused."""

    def test_cross_site_post_refused_same_origin_and_scripts_allowed(self):
        c = self.client
        before = self.settings.all_values()["refresh_hours"]
        r = c.post("/settings", data={"refresh_hours": "1"}, headers={"Origin": "https://evil.example"},
                   follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.settings.all_values()["refresh_hours"], before)
        for headers in ({"Referer": "https://evil.example/x"}, {"Origin": "null"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Origin": "http://testserver:8080"}):
            self.assertEqual(c.post("/wanted/search", headers=headers).status_code, 403, headers)
        self.assertEqual(c.post("/settings", data={"refresh_hours": "2"}, headers={"Origin": "http://testserver"},
                                follow_redirects=False).status_code, 303)
        self.assertEqual(c.post("/settings", data={"refresh_hours": "3"}, headers={"Referer": "http://testserver/s"},
                                follow_redirects=False).status_code, 303)
        self.assertEqual(c.post("/settings", data={"refresh_hours": "4"}, follow_redirects=False).status_code, 303)
        self.assertEqual(self.settings.all_values()["refresh_hours"], 4.0)
        # a valid API key header is allowed whatever the Origin; a reverse proxy's X-Forwarded-Host counts
        self.assertEqual(c.put("/api/v1/settings", json={"refresh_hours": 5},
                               headers={"Origin": "https://evil.example", "X-Api-Key": self.key}).status_code, 200)
        self.assertEqual(c.post("/wanted/search", headers={"Origin": "https://manga.example.com",
                                                           "X-Forwarded-Host": "manga.example.com"},
                                follow_redirects=False).status_code, 303)

    def test_series_delete_cross_site_refused(self):
        from mangarr import db
        with db.connect() as con:
            sid = db.upsert_series(con, model.manual("Keep Me"))
        r = self.client.post(f"/series/{sid}/delete", data={"files": "1"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        with db.connect() as con:
            self.assertIsNotNone(db.get_series(con, sid))

    def test_proxy_that_drops_the_port(self):
        """Review 3(b), round 2 #48: nginx `proxy_set_header Host $host` on port 8443 (Host without its
        port) works when the proxy names the port (X-Forwarded-Port) or the browser vouches for the
        request (Sec-Fetch-Site); a bare Host no longer stands for any port."""
        c = self.client
        proxied = {"Origin": "https://manga.lan:8443", "Host": "manga.lan", "X-Forwarded-For": "192.168.1.4"}
        for good in ({**proxied, "X-Forwarded-Port": "8443"}, {**proxied, "Sec-Fetch-Site": "same-origin"},
                     {**proxied, "Origin": "https://manga.lan"}):                    # default port: Host says it
            self.assertEqual(c.post("/wanted/search", headers=good, follow_redirects=False).status_code, 303, good)
        for bad in (proxied,                                                          # which port? not said
                    {**proxied, "X-Forwarded-Port": "443"},                         # proxy says another port
                    {**proxied, "Origin": "https://evil.example:8443", "X-Forwarded-Port": "8443"},  # another host
                    {"Origin": "https://manga.lan:8443", "Host": "manga.lan"},
                    {**proxied, "Host": "manga.lan:6789"},                           # Host has a port: strict
                    {**proxied, "X-Forwarded-Port": "8443", "Sec-Fetch-Site": "same-site"}):   # browser says no
            self.assertEqual(c.post("/wanted/search", headers=bad).status_code, 403, bad)

    def test_allowed_host_typed_with_a_port(self):
        """Round 2 regression: an entry typed the way the 400 page quoted the Host (with its port) never
        matched. The page now names the bare host, and entries are compared without a port."""
        c = TestClient(self.web.app, base_url="http://manga.example.com:8443")
        r = c.get("/api/v1/series")
        self.assertEqual(r.status_code, 400)
        self.assertIn("add 'manga.example.com' (the name alone, without a port)", r.text)
        self.assertNotIn("8443", r.text)
        self.set(allowed_hosts="Manga.Example.com:8443, https://other.example.org/x, [2001:db8::1]:6789, :80")
        self.assertEqual(self.settings.all_values()["allowed_hosts"],
                         ["2001:db8::1", "manga.example.com", "other.example.org"])
        self.assertEqual(c.get("/api/v1/series").status_code, 200)
        from mangarr import db
        with db.connect() as con:                  # stored with its port by the previous version
            con.execute("""UPDATE setting SET value='["manga.example.com:8443"]' WHERE key='allowed_hosts'""")
            con.commit()
            self.settings.refresh(con)
        self.assertEqual(c.get("/api/v1/series").status_code, 200)
        self.set(allowed_hosts="")
        self.assertEqual(c.get("/api/v1/series").status_code, 400)
        with mock.patch.dict(os.environ, {"MANGARR_ALLOWED_HOSTS": "manga.example.com:8443"}):
            self.assertEqual(c.get("/api/v1/series").status_code, 200)
        r = c.get("/api/v1/series", headers={"Host": "[::1"})               # garbled: no name to suggest
        self.assertEqual(r.status_code, 400)
        self.assertIn("is not a host name", r.text)
        c.close()

    def test_host_allowlist(self):
        c = self.client
        for host in ("evil.example", "rebind.attacker.com:6789", "evil.box", "home.example.com"):
            self.assertEqual(c.get("/api/v1/series", headers={"Host": host}).status_code, 400, host)
        for host in ("localhost:6789", "127.0.0.1", "192.168.1.10:6789", "[::1]:6789", "mangarr", "nas.lan",
                     "nas.local:6789", "box.home.arpa", "testserver",
                     # review 3(a): common home names
                     "nas.fritz.box", "fritz.box", "nas.home:6789", "nas.tail1234.ts.net", "mangarr.docker"):
            self.assertEqual(c.get("/api/v1/series", headers={"Host": host}).status_code, 200, host)
        with mock.patch.dict(os.environ, {"MANGARR_ALLOWED_HOSTS": "manga.example.com, .mydomain.org"}):
            self.assertEqual(c.get("/api/v1/series", headers={"Host": "manga.example.com"}).status_code, 200)
            self.assertEqual(c.get("/api/v1/series", headers={"Host": "a.mydomain.org"}).status_code, 200)
        self.set(allowed_hosts="other.example.net")
        self.assertEqual(c.get("/api/v1/series", headers={"Host": "other.example.net"}).status_code, 200)
        self.assertEqual(c.get("/api/v1/ping", headers={"Host": "evil.example"}).json(), {"ok": True})


class SameSiteCsrfTest(WebBase):
    """Round 2, #48: with a login and its SameSite=Lax cookie, a page on another port of the same host
    (Suwayomi :4567, Komga :25600) or on a sibling subdomain is same-site. Its POSTs are refused however
    the reverse proxy passes Host, while mang-arr's own pages, proxies and scripts keep working."""
    login = ("andy", "pw")

    def test_same_site_pages_refused(self):
        self.signin()
        proxied = {"Host": "nas.lan", "X-Forwarded-For": "192.168.1.20"}
        attacks = (
            {**proxied, "Origin": "http://nas.lan:4567", "Sec-Fetch-Site": "same-site"},    # the repro
            {**proxied, "Origin": "http://nas.lan:4567"},                                   # older browser
            {**proxied, "Origin": "http://nas.lan:4567", "X-Forwarded-Proto": "http"},
            {**proxied, "Referer": "http://nas.lan:25600/book/1"},
            {**proxied, "Origin": "http://komga.nas.lan", "Sec-Fetch-Site": "same-site"},   # sibling subdomain
            {"Host": "nas.lan:6789", "Origin": "http://nas.lan:4567", "Sec-Fetch-Site": "same-site"},
            {"Host": "nas.lan:6789", "Origin": "http://nas.lan:4567"},
            # Sec-Fetch-Site decides when a browser sends it, even if Origin looks right
            {"Host": "192.168.1.10:6789", "Origin": "http://192.168.1.10:6789", "Sec-Fetch-Site": "same-site"},
            {"Host": "192.168.1.10:6789", "Sec-Fetch-Site": "cross-site"},
        )
        with self.assertLogs("mangarr.web.app", logging.WARNING) as logs:
            for headers in attacks:
                r = self.client.post("/settings", data={"auth_user": "andy", "auth_password": "attacker-pw"},
                                     headers=headers, follow_redirects=False)
                self.assertEqual(r.status_code, 403, headers)
        self.assertIn("Sec-Fetch-Site 'same-site'", logs.output[0])
        self.assertIn("'http://nas.lan:4567'", logs.output[0])
        self.assertTrue(self.settings.verify_password(self.settings.all_values()["auth_password"], "pw"))
        self.assertEqual(self.client.get("/api/v1/series").status_code, 200)        # still signed in

    def test_own_pages_proxies_and_scripts_still_work(self):
        self.signin()
        traefik = {"Host": "mangarr.lan", "Origin": "http://mangarr.lan", "X-Forwarded-For": "192.168.1.4",
                   "X-Forwarded-Proto": "http", "X-Forwarded-Port": "80", "X-Forwarded-Host": "mangarr.lan"}
        allowed = (
            {"Host": "192.168.1.10:6789", "Origin": "http://192.168.1.10:6789", "Sec-Fetch-Site": "same-origin"},
            {"Host": "192.168.1.10:6789", "Origin": "http://192.168.1.10:6789"},        # no Sec-Fetch-Site
            {"Host": "nas.local:6789", "Origin": "http://nas.local:6789", "Sec-Fetch-Site": "same-origin"},
            {"Host": "nas.local:6789", "Referer": "http://nas.local:6789/settings"},
            {**traefik, "Sec-Fetch-Site": "same-origin"},                                  # Traefik on port 80
            traefik,
            {"Host": "mangarr.lan", "Origin": "http://mangarr.lan"},
            {"Host": "192.168.1.10:6789", "Sec-Fetch-Site": "none"},                     # the user's own action
            {"Host": "192.168.1.10:6789"},                                                # curl: no Origin
        )
        for i, headers in enumerate(allowed):
            r = self.client.post("/settings", data={"refresh_hours": str(i + 1)}, headers=headers,
                                 follow_redirects=False)
            self.assertEqual(r.status_code, 303, headers)
        self.assertEqual(self.settings.all_values()["refresh_hours"], float(len(allowed)))
        anon = TestClient(self.web.app)                  # an API client: its key skips the check
        r = anon.put("/api/v1/settings", json={"refresh_hours": 2},
                     headers={"X-Api-Key": self.key, "Origin": "http://nas.lan:4567", "Sec-Fetch-Site": "same-site"})
        self.assertEqual(r.status_code, 200)
        anon.close()


class LoginTest(WebBase):
    login = ("andy", "pw")

    def test_password_is_hashed_and_plaintext_migrates(self):
        """#117"""
        from mangarr import db
        stored = self.settings.all_values()["auth_password"]
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertNotIn("pw", stored.split("$", 2)[2])
        with db.connect() as con:                              # an old version / old backup: clear text
            con.execute("UPDATE setting SET value='\"legacy pw\"' WHERE key='auth_password'")
            con.commit()
            self.settings.refresh(con)
        self.signin("andy", "legacy pw")                       # still works ...
        self.assertTrue(self.settings.all_values()["auth_password"].startswith("pbkdf2_sha256$"))  # ... and upgraded
        with db.connect() as con:
            con.execute("UPDATE setting SET value='\"legacy2\"' WHERE key='auth_password'")
            con.commit()
            self.settings.refresh(con)
            self.settings.ensure_security(con)                 # startup migration
        self.assertTrue(self.settings.verify_password(self.settings.all_values()["auth_password"], "legacy2"))

    def test_non_ascii_credentials_and_malformed_cookies(self):
        """#30"""
        c = TestClient(self.web.app)
        for cookie in ("andy|x|y|z|w", "andy|0|ab|notanint|sig", "andy|9999999999|deadbeef", "garbage"):
            c.cookies.set("mangarr_session", cookie)
            self.assertEqual(c.get("/api/v1/series").status_code, 401, cookie)
            self.assertEqual(c.get("/login").status_code, 200, cookie)
        c.cookies.clear()
        self.assertEqual(c.get("/api/v1/series", headers={"X-Api-Key": "schlüssel".encode("latin-1")}).status_code,
                         401)
        self.set(auth_user="jörg", auth_password="Passwört!")
        self.signin("jörg", "Passwört!")
        self.assertEqual(c.post("/login", data={"username": "jörg", "password": "wrong ü"}).status_code, 401)
        c.close()

    def test_empty_password_never_logs_in(self):
        """#51"""
        with self.assertRaises(ValueError):
            self.set(auth_password="")                        # user set: a password is required
        with self.assertRaises(ValueError):
            self.set(auth_user="admin", auth_password=None)   # JSON null
        with self.assertRaises(ValueError):
            self.set(auth_user="a:b")
        self.assertEqual(self.settings.all_values()["auth_user"], "andy")
        r = self.client.put("/api/v1/settings", json={"auth_user": "admin", "auth_password": ""},
                            headers={"X-Api-Key": self.key})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.post("/login", data={"username": "andy", "password": ""}).status_code, 401)
        self.set(auth_user="", auth_password="")              # turning the login off is allowed
        self.assertEqual(self.client.get("/api/v1/series").status_code, 200)

    def test_open_redirect(self):
        """#59"""
        for bad in ("//evil.example/x", "/\\evil.example", "https://evil.example", "javascript:alert(1)"):
            r = self.client.post("/login", data={"username": "andy", "password": "pw", "next": bad},
                                 follow_redirects=False)
            self.assertEqual(r.headers["location"], "/", bad)
            r = self.client.get("/login", params={"next": bad}, follow_redirects=False)
            self.assertEqual(r.headers["location"], "/", bad)
        r = self.client.post("/login", data={"username": "andy", "password": "pw", "next": "/wanted?x=1"},
                             follow_redirects=False)
        self.assertEqual(r.headers["location"], "/wanted?x=1")

    def test_throttle_covers_login_and_basic(self):
        """#49, #53, #70, #77"""
        c = self.client
        big = "u" * 5000
        with self.assertLogs("mangarr.web.app", logging.WARNING) as logs:
            for _ in range(5):
                self.assertEqual(c.post("/login", data={"username": big, "password": "bad"}).status_code, 401)
        self.assertTrue(all(len(line) < 300 for line in logs.output), "attacker-sized log lines")
        r = c.post("/login", data={"username": "andy", "password": "bad"})     # 6th: now blocked
        self.assertEqual(r.status_code, 401)
        r = c.post("/login", data={"username": "andy", "password": "pw"})
        self.assertEqual(r.status_code, 429)                                   # even the right password waits
        self.assertIn("Retry-After", r.headers)
        # Basic is not accepted in forms mode at all
        self.security.throttle.succeeded("testclient")
        self.assertEqual(c.get("/api/v1/series", auth=("andy", "pw")).status_code, 401)
        self.set(auth_method="basic")
        r = c.get("/api/v1/series", auth=("andy", "pw"))
        self.assertEqual(r.status_code, 200)
        owner_cookie = c.cookies.get("mangarr_session")        # Basic checked once -> browser-session cookie
        self.assertTrue(owner_cookie)
        self.assertNotIn("max-age", r.headers["set-cookie"].lower())
        c.cookies.clear()
        for _ in range(6):                                     # someone else at the same address guesses
            c.get("/api/v1/series", auth=("andy", "nope"))
        self.assertEqual(c.get("/api/v1/series", auth=("andy", "pw")).status_code, 429)
        # review 3(c): the owner's browser, already signed in, is not locked out by those failures
        c.cookies.set("mangarr_session", owner_cookie)
        self.assertEqual(c.get("/api/v1/series", auth=("andy", "pw")).status_code, 200)

    def test_trusted_proxy_client_address(self):
        """Review 3(c): behind a listed reverse proxy, throttling is per real client."""
        from types import SimpleNamespace

        from starlette.datastructures import Headers

        def req(peer, xff=None):
            return SimpleNamespace(client=SimpleNamespace(host=peer),
                                   headers=Headers({"x-forwarded-for": xff} if xff else {}))
        ip = self.security.client_ip
        self.assertEqual(ip(req("10.0.0.2", "1.2.3.4")), "10.0.0.2")          # no trusted proxies: the peer
        with mock.patch.dict(os.environ, {"MANGARR_TRUSTED_PROXIES": "10.0.0.0/24, bogus"}):
            self.assertEqual(ip(req("10.0.0.2", "6.6.6.6, 1.2.3.4")), "1.2.3.4")   # left hops are client-made
            self.assertEqual(ip(req("10.0.0.2", "1.2.3.4, 10.0.0.9")), "1.2.3.4")  # chained proxies skipped
            self.assertEqual(ip(req("10.0.0.2", "not-an-ip")), "10.0.0.2")
            self.assertEqual(ip(req("10.0.0.2")), "10.0.0.2")
            self.assertEqual(ip(req("192.168.1.5", "1.2.3.4")), "192.168.1.5")    # not a proxy: header ignored

    def test_login_without_password_explains_recovery(self):
        """Review 3(d): a login left without a password says how to get back in."""
        from mangarr import db
        with db.connect() as con:
            con.execute("UPDATE setting SET value='\"\"' WHERE key='auth_password'")    # the old bug's state
            con.commit()
            self.settings.refresh(con)
        self.assertIn("MANGARR_RESET_LOGIN", self.client.get("/login").text)
        r = self.client.post("/login", data={"username": "andy", "password": "x"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("MANGARR_RESET_LOGIN", r.text)
        self.assertEqual(self.client.get("/api/v1/series").status_code, 401)         # still closed
        self.web._startup_security()                             # an ordinary restart changes nothing
        self.assertEqual(self.client.get("/api/v1/series").status_code, 401)
        with mock.patch.dict(os.environ, {"MANGARR_RESET_LOGIN": "1"}), \
                self.assertLogs("mangarr.settings", logging.WARNING) as logs:
            self.web._startup_security()                         # restart with the variable
        self.assertIn("MANGARR_RESET_LOGIN", "\n".join(logs.output))
        self.assertEqual(self.settings.all_values()["auth_user"], "")
        self.assertEqual(self.client.get("/api/v1/series").status_code, 200)

    def test_corrupt_login_value_stays_closed(self):
        """Review 4 (#27): an undecodable auth_user never switches the login off."""
        from mangarr import db
        self.signin()
        with db.connect() as con:
            con.execute("UPDATE setting SET value='{broken' WHERE key='auth_user'")
            con.commit()
            with self.assertLogs("mangarr.settings", logging.ERROR):
                self.settings.refresh(con)
        v = self.settings.all_values()
        self.assertEqual((v["auth_user"], v["auth_password"]), (self.settings.UNREADABLE_USER, ""))
        self.assertEqual(TestClient(self.web.app).get("/api/v1/series").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/series").status_code, 401)        # old session no longer valid
        self.assertEqual(self.client.post("/login", data={"username": "(unreadable)", "password": "x"}).status_code,
                         401)
        self.assertEqual(self.client.get("/api/v1/series", headers={"X-Api-Key": self.key}).status_code, 200)
        self.set(auth_user="")                                  # turning it off rewrites the pair
        self.assertEqual(self.settings.all_values()["auth_user"], "")
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT value FROM setting WHERE key='auth_user'").fetchone()[0], '""')

    def test_corrupt_session_value_signs_everyone_out(self):
        """Review 4: an undecodable session_epoch must not fall back to 0 (reviving old cookies)."""
        from mangarr import db
        self.signin()
        self.assertEqual(self.client.get("/api/v1/series").status_code, 200)
        with db.connect() as con:
            con.execute("UPDATE setting SET value='oops' WHERE key='session_epoch'")
            con.commit()
            self.settings.refresh(con)
            self.assertEqual(self.settings.all_values()["session_secret"], "")
        self.assertEqual(self.client.get("/api/v1/series").status_code, 401)
        self.signin()                                           # signing in repairs it (new secret, epoch)
        self.assertEqual(self.client.get("/api/v1/series").status_code, 200)
        with db.connect() as con:
            self.settings.refresh(con)
        self.assertTrue(self.settings.all_values()["session_secret"])

    def test_login_failure_delay_does_not_block_a_thread(self):
        """#70: the delay is an asyncio sleep in an async route."""
        calls = []

        async def record(s):
            calls.append((s, threading.current_thread().name))
        with mock.patch("mangarr.web.app.asyncio.sleep", record):
            self.client.post("/login", data={"username": "andy", "password": "bad"})
        self.assertEqual(len(calls), 1)
        self.assertTrue(asyncio.iscoroutinefunction(self.web.login_submit))


class ThrottleBurstTest(WebBase):
    """Round 2, #53: password attempts are reserved before the (slow) check, so parallel guesses from one
    address get no more checks than the same guesses one after another."""
    login = ("andy", "pw")

    def burst(self, n: int, send, cookies: bool = True) -> tuple[Counter, int]:
        """n requests at once from one address on one event loop (like parallel connections); returns
        (status codes, passwords checked). The check is slowed down so every request is in flight
        before the first one fails."""
        import httpx
        checked, lock, real = [], threading.Lock(), self.settings.verify_password

        def slow_verify(stored, given):
            time.sleep(0.05)
            with lock:
                checked.append(given)
            return real(stored, given)

        async def run():
            jar = None if cookies else http.cookiejar.CookieJar(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.web.app, client=("192.168.1.66", 1)),
                                         base_url="http://testserver", cookies=jar) as ac:
                return await asyncio.gather(*(send(ac) for _ in range(n)))
        with mock.patch.object(self.settings, "verify_password", slow_verify):
            responses = asyncio.run(run())
        return Counter(r.status_code for r in responses), len(checked)

    def test_reserve_is_atomic(self):
        t = self.security.Throttle()
        with ThreadPoolExecutor(32) as ex:
            waits = list(ex.map(lambda _: t.reserve("10.0.0.9"), range(1000)))
        self.assertEqual(waits.count(0), t.FREE + 1)
        self.assertTrue(all(w >= 1 for w in waits if w))
        t.succeeded("10.0.0.9")
        self.assertEqual(t.reserve("10.0.0.9"), 0)

    def test_parallel_wrong_logins(self):
        allowed = self.security.Throttle.FREE + 1                  # what sequential guesses get before 429
        with self.assertLogs("mangarr.web.app", logging.WARNING) as logs:
            codes, checked = self.burst(1000, lambda ac: ac.post("/login", data={"username": "andy",
                                                                                 "password": "guess"}))
        self.assertEqual(checked, allowed)
        self.assertEqual(codes, Counter({401: allowed, 429: 1000 - allowed}))
        self.assertLessEqual(len(logs.output), allowed + self.security.LogBudget.PER_MINUTE + 1)
        codes, checked = self.burst(1, lambda ac: ac.post("/login", data={"username": "andy", "password": "pw"}))
        self.assertEqual((codes, checked), (Counter({429: 1}), 0))  # blocked, as after 6 failures in a row

    def test_parallel_wrong_basic_auth(self):
        self.set(auth_method="basic")
        allowed = self.security.Throttle.FREE + 1
        codes, checked = self.burst(300, lambda ac: ac.get("/api/v1/series", auth=("andy", "nope")))
        self.assertEqual(checked, allowed)
        self.assertEqual(codes, Counter({401: allowed, 429: 300 - allowed}))

    def test_parallel_right_basic_auth_is_not_throttled(self):
        """A Basic client without a cookie jar sends the password with every request: once it has been
        checked, parallel requests need no reservation, so they are never refused."""
        self.set(auth_method="basic")
        self.assertEqual(self.client.get("/api/v1/series", auth=("andy", "pw")).status_code, 200)
        codes, checked = self.burst(50, lambda ac: ac.get("/api/v1/series", auth=("andy", "pw")), cookies=False)
        self.assertEqual((codes, checked), (Counter({200: 50}), 0))


class SessionTest(WebBase):
    """#118: sessions can be revoked."""
    login = ("andy", "pw")

    def _copy(self):
        c = TestClient(self.web.app)
        c.cookies.set("mangarr_session", self.client.cookies.get("mangarr_session"))
        return c

    def test_logout_revokes_the_cookie_server_side(self):
        self.signin()
        copy = self._copy()
        self.assertEqual(copy.get("/api/v1/series").status_code, 200)
        self.client.post("/logout")
        self.assertEqual(copy.get("/api/v1/series").status_code, 401)
        copy.close()

    def test_logout_everywhere_and_changes_sign_sessions_out(self):
        self.signin()
        copy = self._copy()
        self.client.post("/settings/logout-all")
        self.assertEqual(copy.get("/api/v1/series").status_code, 401)
        copy.close()
        # changing the password signs others out but keeps the browser that did it
        self.signin()
        copy = self._copy()
        r = self.client.post("/settings", data={"auth_user": "andy", "auth_password": "new pw"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(copy.get("/api/v1/series").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/series").status_code, 200)
        copy.close()
        # rotating the API key: the old key and other sessions stop working
        copy = self._copy()
        self._old_cookie = self.client.cookies.get("mangarr_session")
        old = self.key
        self.client.post("/settings/api-key/regenerate")
        self.assertEqual(self.client.get("/api/v1/series").status_code, 200)           # the actor stays signed in
        copy.cookies.clear()
        self.assertEqual(copy.get("/api/v1/series", headers={"X-Api-Key": old}).status_code, 401)
        copy.cookies.set("mangarr_session", self._old_cookie)
        self.assertEqual(copy.get("/api/v1/series").status_code, 401)
        new = self.client.get("/api/v1/settings").json()["api_key"]
        self.assertNotEqual(new, old)
        self.assertEqual(copy.get("/api/v1/series", headers={"X-Api-Key": new}).status_code, 200)
        copy.close()

    def test_session_not_tied_to_api_key_restore(self):
        """Setting the old key back does not revive old sessions (epoch moves on)."""
        self.signin()
        copy = self._copy()
        old = self.key
        self.set(api_key="0" * 32)
        self.set(api_key=old)
        self.assertEqual(copy.get("/api/v1/series").status_code, 401)
        copy.close()


class ApiKeyTest(WebBase):
    """#50, #108, #119"""
    login = ("andy", "pw")

    def test_api_key_masked_and_not_logged(self):
        self.assertEqual(self.settings.masked(self.settings.all_values())["api_key"], self.settings.MASK)
        self.signin()
        html = self.client.get("/settings").text
        self.assertNotIn(self.key, html)
        self.assertNotIn('name="api_key"', html)                 # not settable from the general form
        self.assertEqual(self.client.get("/api/v1/settings").json()["api_key"], self.key)   # authed caller
        self.assertNotIn("session_secret", self.client.get("/api/v1/settings").json())
        with self.assertLogs("mangarr.settings", logging.INFO) as logs:
            self.set(api_key="k" * 32, webhook_url="https://hooks.example/secret-token-123", refresh_hours=3,
                     komga_url="https://komga.example/x?token=other-secret-456")
        text = "\n".join(logs.output)
        self.assertNotIn("k" * 32, text)
        self.assertNotIn("secret-token-123", text)
        self.assertNotIn("other-secret-456", text)
        self.assertIn("setting webhook_url = ***", text)
        self.assertIn("https://komga.example/...", text)
        self.assertEqual(self.settings.masked(self.settings.all_values())["webhook_url"], self.settings.MASK)
        # unchanged values are neither written nor logged again
        with self.assertLogs("mangarr.settings", logging.INFO) as logs:
            self.set(refresh_hours=3, min_pages=9)
        self.assertEqual(len([x for x in logs.output if "setting " in x]), 1)
        self.assertEqual(self.client.put("/api/v1/settings", json={"session_epoch": 99},
                                         headers={"X-Api-Key": "k" * 32}).status_code, 400)

    def test_query_api_key_only_for_get_under_api(self):
        c = TestClient(self.web.app)
        self.assertEqual(c.get(f"/api/v1/series?apikey={self.key}").status_code, 200)
        self.assertEqual(c.get(f"/settings?apikey={self.key}").status_code, 401)
        self.assertEqual(c.post(f"/settings?apikey={self.key}", data={"auth_user": "x"}).status_code, 401)
        self.assertEqual(c.post(f"/api/v1/command?apikey={self.key}", json={"name": "Nope"}).status_code, 401)
        c.close()


class ApiKeyRotationTest(WebBase):
    """#50 (d): a key readable while there was no login stops working when a login is switched on."""

    def test_form_enabling_login_rotates_the_key(self):
        c = self.client
        old = c.get("/api/v1/settings").json()["api_key"]              # anyone could read it: no login yet
        self.assertEqual(old, self.key)
        with self.assertLogs("mangarr.web.app", logging.WARNING) as logs:
            r = c.post("/settings", data={"auth_user": "andy", "auth_password": "pw", "auth_method": "forms"},
                       follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("API key was regenerated", "\n".join(logs.output))
        anon = TestClient(self.web.app)
        self.assertEqual(anon.get("/api/v1/series", headers={"X-Api-Key": old}).status_code, 401)
        new = self.settings.all_values()["api_key"]
        self.assertNotEqual(new, old)
        self.assertEqual(anon.get("/api/v1/series", headers={"X-Api-Key": new}).status_code, 200)
        anon.close()

    def test_api_enabling_login_without_the_key_rotates_it(self):
        old = self.key
        r = self.client.put("/api/v1/settings", json={"auth_user": "andy", "auth_password": "pw"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("API key was regenerated", r.headers.get("x-mangarr-notice", ""))
        new = r.json()["api_key"]                                         # the caller gets the new key
        self.assertNotEqual(new, old)
        self.assertEqual(self.client.get("/api/v1/series", headers={"X-Api-Key": old}).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/series", headers={"X-Api-Key": new}).status_code, 200)

    def test_installer_flow_gets_a_new_key(self):
        """Round 2, installer #9: GET key while open -> PUT login + Komga key with X-Api-Key (install.sh).
        A copy of the key read in between must not survive: the key is replaced even though the PUT used
        it, and the response carries the new one so the installer can carry on."""
        anon = TestClient(self.web.app)
        stolen = anon.get("/api/v1/settings").json()["api_key"]           # anyone on the LAN, before the PUT
        key = self.client.get("/api/v1/settings").json()["api_key"]
        self.assertEqual(stolen, key)
        r = self.client.put("/api/v1/settings", headers={"X-Api-Key": key},
                            json={"auth_user": "admin", "auth_password": "pw", "komga_url": "http://komga:25600",
                                  "komga_api_key": "ADMIN-KEY-FROM-INSTALLER"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("API key was regenerated", r.headers.get("x-mangarr-notice", ""))
        new = r.json()["api_key"]
        self.assertNotEqual(new, key)
        self.assertEqual(new, self.settings.all_values()["api_key"])
        for method, path in (("GET", "/api/v1/series"), ("GET", "/api/v1/settings"), ("GET", "/system/backup")):
            self.assertEqual(anon.request(method, path, headers={"X-Api-Key": stolen}).status_code, 401, path)
        self.assertEqual(anon.put("/api/v1/settings", json={"komga_url": "http://attacker.example"},
                                  headers={"X-Api-Key": stolen}).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/series", headers={"X-Api-Key": new}).status_code, 200)
        anon.close()
        # a key chosen in the same call is kept (it was never readable); sending the old one back is not
        self.set(auth_user="")
        self.client.put("/api/v1/settings", json={"auth_user": "andy", "auth_password": "pw", "api_key": "c" * 32})
        self.assertEqual(self.client.get("/api/v1/series", headers={"X-Api-Key": "c" * 32}).status_code, 200)
        self.set(auth_user="")
        r = self.client.put("/api/v1/settings", json={"auth_user": "andy", "auth_password": "pw", "api_key": "c" * 32},
                            headers={"X-Api-Key": "c" * 32})
        self.assertNotEqual(r.json()["api_key"], "c" * 32)
        self.assertEqual(self.client.get("/api/v1/series", headers={"X-Api-Key": "c" * 32}).status_code, 401)


class SecretDestinationTest(WebBase):
    """#10: a stored secret is never sent to a new destination."""

    def test_changed_destination_clears_masked_secret(self):
        self.set(komga_url="http://komga:25600", komga_api_key="KEY", smtp_host="smtp.good", smtp_password="PW",
                 ntfy_url="https://ntfy.sh/t", ntfy_token="T", gotify_url="http://g", gotify_token="G")
        with mock.patch("mangarr.komga.test", return_value=(False, "HTTP 401")) as test:
            r = self.client.post("/settings", data={"komga_url": "https://attacker.example",
                                                    "komga_api_key": self.settings.MASK, "action": "test-komga"},
                                 follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        test.assert_called_once()
        v = self.settings.all_values()
        self.assertEqual((v["komga_url"], v["komga_api_key"]), ("https://attacker.example", ""))
        self.assertIn("stored komga_api_key was cleared", self.client.get(r.headers["location"]).text)  # user told
        notices = self.set(smtp_host="attacker.example")                               # field not sent at all
        self.assertTrue(notices and "smtp_password" in notices[0])
        self.assertEqual(self.settings.all_values()["smtp_password"], "")
        self.set(ntfy_url="https://ntfy.sh/other", ntfy_token="T2")                     # re-entered: kept
        self.assertEqual(self.settings.all_values()["ntfy_token"], "T2")
        self.set(gotify_url="http://g", gotify_token=self.settings.MASK)                # unchanged: kept
        self.assertEqual(self.settings.all_values()["gotify_token"], "G")
        r = self.client.put("/api/v1/settings", json={"gotify_url": "http://elsewhere"})
        self.assertIn("gotify_token", r.headers.get("x-mangarr-notice", ""))


class FailClosedTest(WebBase):
    """#8, #27: unreadable settings never switch the login off."""
    login = ("andy", "pw")

    def test_keeps_last_good_values_when_the_db_breaks(self):
        self.settings.all_values()
        with mock.patch.object(self.settings, "TTL", 0.0), \
                mock.patch("mangarr.db.connect", side_effect=sqlite3.OperationalError("disk I/O error")):
            self.assertEqual(self.client.get("/api/v1/series").status_code, 401)
            self.assertEqual(self.client.get("/api/v1/system/backup").status_code, 401)
            self.assertEqual(self.client.put("/api/v1/settings", json={"auth_user": "x"}).status_code, 401)
            self.assertEqual(self.settings.all_values()["auth_user"], "andy")

    def test_never_loaded_means_503(self):
        self.settings._cache.clear()
        with mock.patch.object(self.settings, "_good", False), \
                mock.patch("mangarr.db.connect", side_effect=sqlite3.OperationalError("unable to open database")):
            self.assertEqual(self.client.get("/api/v1/series").status_code, 503)
            self.assertEqual(self.client.get("/system/backups/x.db").status_code, 503)
            self.assertEqual(self.client.get("/api/v1/ping").status_code, 200)

    def test_missing_setting_table_is_an_error_not_defaults(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        with self.assertRaises(sqlite3.OperationalError):
            self.settings._load(con)


class BodyLimitTest(WebBase):
    """#25"""
    login = ("andy", "pw")

    def test_large_bodies_refused(self):
        c = self.client
        r = c.post("/login", data={"username": "x", "password": "y"}, files={"f": ("a", b"0" * (2 << 20))})
        self.assertEqual(r.status_code, 413)

        def chunks():
            for _ in range(30):
                yield b"a" * 100_000
        r = c.post("/api/v1/command", content=chunks(), headers={"Content-Type": "application/json",
                                                                 "X-Api-Key": self.key})
        self.assertEqual(r.status_code, 413)
        r = c.post("/login", content=chunks(), headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(c.post("/login", data={"username": "andy", "password": "pw"},
                                follow_redirects=False).status_code, 303)
        self.assertGreater(self.security.BODY_LIMITS["/system/backups/upload"], self.security.MAX_BODY)


class DisclosureTest(WebBase):
    """#54, #120, exception text: open endpoints say little to anonymous callers."""
    login = ("andy", "pw")

    def test_open_endpoints_anonymous(self):
        self.set(komga_url="http://user:secretpw@komga.invalid:25600", komga_api_key="k")
        with mock.patch("mangarr.komga.test", return_value=(False, "URLError: nope")):
            d = self.client.get("/api/v1/health").json()
        self.assertFalse(d["ok"])
        self.assertIn("Suwayomi", d["problems"])
        self.assertNotIn("localhost", str(d))
        self.assertNotIn("secretpw", str(d))
        full = self.client.get("/api/v1/health", headers={"X-Api-Key": self.key}).json()
        self.assertTrue(any("offline in tests" in p for p in full["problems"]))
        self.assertFalse(any("secretpw" in p for p in full["problems"] + full["warnings"]))
        job = mock.Mock(kind="refresh-all", status="running", title="Secret Title")
        job.as_dict.return_value = {"title": "Secret Title", "items": [{"title": "Private"}]}
        with mock.patch.object(self.web.runner, "current", job):
            anon = self.client.get("/api/v1/system/status").json()
            authed = self.client.get("/api/v1/system/status", headers={"X-Api-Key": self.key}).json()
        self.assertNotIn("Secret Title", str(anon))
        self.assertEqual(anon["job"], {"kind": "refresh-all", "status": "running"})
        self.assertIn("Secret Title", str(authed))

    def test_error_details_only_for_signed_in_callers(self):
        c = TestClient(self.web.app, raise_server_exceptions=False)
        with mock.patch("mangarr.web.app.health.summary", side_effect=RuntimeError("secret detail")):
            r = c.get("/api/v1/system/status")
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("secret detail", r.text)
        with mock.patch("mangarr.web.app.db.wanted_all", side_effect=RuntimeError("detail for admins")):
            r = c.get("/api/v1/wanted", headers={"X-Api-Key": self.key})
        self.assertEqual(r.status_code, 500)
        self.assertIn("detail for admins", r.text)
        c.close()


class BlockingWorkTest(WebBase):
    """#19, #26, #34, #44, #71, #110"""

    def test_settings_test_actions_run_off_the_event_loop(self):
        where = []

        def fake_test():
            try:
                asyncio.get_running_loop()
                where.append("event loop")
            except RuntimeError:
                where.append("worker thread")
            return True, "ok"
        with mock.patch("mangarr.komga.test", fake_test):
            self.client.post("/settings", data={"action": "test-komga"})
        self.assertEqual(where, ["worker thread"])

    def test_ui_client_uses_short_timeouts(self):
        seen = []

        def gq(query, variables=None, timeout=180, retries=3):
            seen.append((timeout, retries))
            return {"sources": {"nodes": [{"id": "1", "displayName": "A", "lang": "en"}]}}
        from mangarr.suwayomi import Client
        base = Client("http://suwayomi.invalid")
        base.gq = gq                                            # the transport; sources() is the real method
        srcs = self.web._UIClient(base, 10).sources()
        with mock.patch.object(self.web.client, "gq", gq):
            self.web.ui_client.gq("{ x }", timeout=20, retries=3)
        self.assertEqual([s.name for s in srcs], ["A"])
        self.assertEqual(seen, [(10, 1), (10, 1)])

    def test_cross_site_lookup_not_run(self):
        with mock.patch("mangarr.web.app.metadata.lookup") as lookup:
            html = self.client.get("/add?term=x", headers={"Sec-Fetch-Site": "cross-site"}).text
            self.assertEqual(self.client.get("/api/v1/lookup?term=x",
                                             headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
        lookup.assert_not_called()
        self.assertIn("search not run", html)

    def test_lists_minimum_sync_interval(self):
        r = self.client.post("/lists/add", data={"kind": "anilist_top", "sort": "POPULARITY_DESC", "limit": "5",
                                                 "sync_hours": "0.001", "sync_now": "0"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        from mangarr import db, lists
        with db.connect() as con:
            self.assertEqual([r["sync_hours"] for r in lists.all_lists(con)], [1.0])


class HealthCacheTest(unittest.TestCase):
    """#20, #35, #75, #76: one run at a time, cached, with a deadline."""

    def setUp(self):
        from mangarr import health
        self.health = health
        self._settle()
        self.p = mock.patch.dict(health._cache, {"at": 0.0, "checks": []})
        self.p.start()

    def tearDown(self):
        self._settle()
        self.p.stop()

    def _settle(self):
        """Wait for a background run another test started to finish."""
        done = getattr(self.health, "_running", None)
        if done is not None:
            done.wait(5)

    def test_single_flight_and_cache(self):
        calls = []

        def compute(client):
            calls.append(1)
            time.sleep(0.2)
            with self.health._lock:
                self.health._cache.update(at=time.monotonic(), checks=[self.health.Check("ok", "X", "y")])
            return []
        with mock.patch.object(self.health, "_compute", compute):
            results = []
            threads = [threading.Thread(target=lambda: results.append(self.health.run(None))) for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(calls), 1)
            self.assertTrue(all(r and r[0].name == "X" for r in results))
            self.health.run(None)
            self.health.run(None, force=True)                  # younger than FORCE_MIN_SECS: cached
            self.assertEqual(len(calls), 1)

    def test_deadline(self):
        gate = threading.Event()

        def hang(client):
            gate.wait(5)
        with mock.patch.object(self.health, "_compute", hang):
            t0 = time.monotonic()
            checks = self.health.run(None, wait=0.2)
            self.assertLess(time.monotonic() - t0, 2)
            self.assertEqual(checks[-1].name, "Health checks")
            self.assertEqual(self.health.summary(None, wait=0.1)["errors"], 0)
            gate.set()

    def test_hung_run_is_reported_to_cached_callers(self):
        """Review regression 2: a run hung past DEADLINE must not leave everyone on the last good result."""
        gate = threading.Event()

        def hang(client):
            gate.wait(5)
        with self.health._lock:
            self.health._cache.update(at=0.0, checks=[self.health.Check("ok", "X", "fine")])   # stale, good
        try:
            with mock.patch.object(self.health, "_compute", hang), mock.patch.object(self.health, "DEADLINE", 0.3):
                self.assertEqual([c.name for c in self.health.run(None)], ["X"])   # stale-while-revalidate
                self.assertEqual(self.health.summary(None)["errors"], 0)            # not overdue yet
                time.sleep(0.5)
                with self.assertLogs("mangarr.health", logging.WARNING):
                    checks = self.health.run(None)
                self.assertEqual(checks[-1].name, "Health checks")
                self.assertEqual(self.health.summary(None)["errors"], 1)            # the status poller too
                with self.health._lock:                                            # even a fresh cache
                    self.health._cache["at"] = time.monotonic()
                self.assertEqual(self.health.run(None)[-1].level, "error")
        finally:
            gate.set()


class LogFileTest(unittest.TestCase):
    def test_log_file_is_private(self):
        import logging
        import os
        import stat
        import tempfile

        from mangarr import logsetup
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mangarr.log")
            root = logging.getLogger()
            old = list(root.handlers), root.level
            try:
                logsetup.setup("INFO", path, console=False)
                logging.getLogger("t").info("hello")
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            finally:
                for h in list(root.handlers):
                    root.removeHandler(h)
                    h.close()
                for h in old[0]:
                    root.addHandler(h)
                root.setLevel(old[1])


if __name__ == "__main__":
    unittest.main()

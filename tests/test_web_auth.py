"""Login page, session cookie, API key and basic auth through the real app."""
import os
import tempfile
import unittest
from unittest import mock

try:
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError):      # web extras or httpx not installed
    TestClient = None


@unittest.skipIf(TestClient is None, "web extras not installed")
class AuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch("mangarr.config.DATA_DIR", self.tmp.name),
                        mock.patch("mangarr.config.DB_PATH", os.path.join(self.tmp.name, "t.db")),
                        mock.patch("mangarr.config.STAGING_ROOT", self.tmp.name),
                        mock.patch("mangarr.config.LIBRARY_ROOT", self.tmp.name)]
        for p in self.patches:
            p.start()
        from mangarr import db, settings
        settings._cache.clear()
        with db.connect() as con:
            settings.set_many(con, {"auth_user": "andy", "auth_password": "pw", "auth_method": "forms"})
            self.key = settings.ensure_api_key(con)
        from mangarr.web import app as web
        self.web = web
        self.client = TestClient(web.app)

    def tearDown(self):
        self.client.close()
        for p in self.patches:
            p.stop()
        from mangarr import settings
        settings._cache.clear()
        self.tmp.cleanup()

    def test_forms_flow(self):
        c = self.client
        r = c.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/login"))
        self.assertEqual(c.get("/api/v1/series").status_code, 401)
        self.assertEqual(c.get("/api/v1/series", headers={"X-Api-Key": self.key}).status_code, 200)
        self.assertIn(c.get("/api/v1/health").status_code, (200, 503))      # open for monitoring (503 = problems)
        with mock.patch("mangarr.web.app.time.sleep", lambda s: None):
            self.assertEqual(c.post("/login", data={"username": "andy", "password": "bad"}).status_code, 401)
        r = c.post("/login", data={"username": "andy", "password": "pw", "next": "/wanted"}, follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/wanted"))
        self.assertIn("mangarr_session", c.cookies)
        self.assertEqual(c.get("/wanted").status_code, 200)
        c.post("/logout", follow_redirects=False)
        self.assertEqual(c.get("/api/v1/series").status_code, 401)

    def test_basic_method(self):
        from mangarr import db, settings
        with db.connect() as con:
            settings.set_many(con, {"auth_method": "basic"})
        c = self.client
        r = c.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(c.get("/", auth=("andy", "pw")).status_code, 200)

    def test_tampered_cookie_rejected(self):
        c = self.client
        c.cookies.set("mangarr_session", "andy|9999999999|deadbeef")
        self.assertEqual(c.get("/api/v1/series").status_code, 401)


if __name__ == "__main__":
    unittest.main()

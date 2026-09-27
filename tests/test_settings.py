"""Settings: defaults, coercion, secrets that are kept when submitted blank."""
import os
import tempfile
import unittest

from mangarr import db, settings


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "s.db")
        settings._cache.clear()

    def tearDown(self):
        settings._cache.clear()
        self.tmp.cleanup()

    def test_defaults_and_coercion(self):
        with db.connect(self.path) as con:
            v = settings.all_values(con)
            self.assertEqual(v["min_pages"], 8)
            settings.set_many(con, {"min_pages": "12", "refresh_hours": "3.5",
                                    "throttled_sources": "Manganato (EN), Bbato (EN)"})
            v = settings.all_values(con)
        self.assertEqual(v["min_pages"], 12)
        self.assertEqual(v["refresh_hours"], 3.5)
        self.assertEqual(v["throttled_sources"], ["bbato (en)", "manganato (en)"])

    def test_masked_secret_keeps_blank_clears(self):
        with db.connect(self.path) as con:
            settings.set_many(con, {"komga_api_key": "abc"})
            self.assertEqual(settings.masked(settings.all_values(con))["komga_api_key"], settings.MASK)
            settings.set_many(con, {"komga_api_key": settings.MASK})      # form submitted untouched
            self.assertEqual(settings.all_values(con)["komga_api_key"], "abc")
            settings.set_many(con, {"komga_api_key": "new"})
            self.assertEqual(settings.all_values(con)["komga_api_key"], "new")
            settings.set_many(con, {"komga_api_key": ""})                 # cleared
            self.assertEqual(settings.all_values(con)["komga_api_key"], "")

    def test_api_key_generated_once(self):
        with db.connect(self.path) as con:
            k1 = settings.ensure_api_key(con)
            k2 = settings.ensure_api_key(con)
        self.assertEqual(k1, k2)
        self.assertEqual(len(k1), 32)

    def test_unknown_key_rejected(self):
        with db.connect(self.path) as con, self.assertRaises(KeyError):
            settings.set_many(con, {"nope": 1})
        with db.connect(self.path) as con, self.assertRaises(KeyError):
            settings.set_many(con, {"session_epoch": 5})              # internal keys: not from forms or the API

    def test_password_stored_as_salted_hash(self):
        with db.connect(self.path) as con:
            settings.set_many(con, {"auth_user": "andy", "auth_password": " pw with spaces "})
            stored = settings.all_values(con)["auth_password"]
            self.assertTrue(settings.is_hashed(stored))
            self.assertTrue(settings.verify_password(stored, " pw with spaces "))   # not stripped
            self.assertFalse(settings.verify_password(stored, "pw with spaces"))
            self.assertFalse(settings.verify_password(stored, ""))
            self.assertNotEqual(settings.hash_password("x"), settings.hash_password("x"))  # salted
            epoch = settings.all_values(con)["session_epoch"]
            settings.set_many(con, {"auth_password": " pw with spaces "})              # same password again
            self.assertEqual(settings.all_values(con)["auth_password"], stored)
            self.assertEqual(settings.all_values(con)["session_epoch"], epoch)
            self.assertEqual(settings.masked(settings.all_values(con))["auth_password"], settings.MASK)

    def test_login_validation(self):
        with db.connect(self.path) as con:
            for bad in ({"auth_user": "andy"}, {"auth_user": "andy", "auth_password": None},
                        {"auth_user": "a:b", "auth_password": "x"}, {"auth_user": "a\nb", "auth_password": "x"},
                        {"auth_method": "nope"}):
                with self.assertRaises(ValueError, msg=str(bad)):
                    settings.set_many(con, bad)
            self.assertEqual(settings.all_values(con)["auth_user"], "")              # nothing half-written
            with self.assertRaises(ValueError):
                settings.set_many(con, {"komga_url": None})

    def test_secret_follows_its_destination(self):
        with db.connect(self.path) as con:
            settings.set_many(con, {"komga_url": "http://komga:25600", "komga_api_key": "abc"})
            notes = settings.set_many(con, {"komga_url": "http://evil", "komga_api_key": settings.MASK})
            self.assertEqual(settings.all_values(con)["komga_api_key"], "")
            self.assertEqual(len(notes), 1)
            settings.set_many(con, {"komga_api_key": "abc"})
            settings.set_many(con, {"komga_url": "http://komga2", "komga_api_key": "abc"})   # re-entered
            self.assertEqual(settings.all_values(con)["komga_api_key"], "abc")

    def test_fail_closed(self):
        from unittest import mock
        with db.connect(self.path) as con:
            settings.set_many(con, {"auth_user": "andy", "auth_password": "pw"})
        with mock.patch.object(settings, "TTL", 0.0), \
                mock.patch("mangarr.db.connect", side_effect=OSError("disk I/O error")):
            self.assertEqual(settings.all_values()["auth_user"], "andy")            # last good values kept
            self.assertTrue(settings.available())
        settings._cache.clear()
        with mock.patch.object(settings, "_good", False), \
                mock.patch("mangarr.db.connect", side_effect=OSError("unable to open database file")):
            settings.all_values()
            self.assertFalse(settings.available())                                  # the web UI answers 503

    def test_legacy_clear_text_password_migrated(self):
        with db.connect(self.path) as con:
            con.execute("INSERT INTO setting (key, value) VALUES ('auth_password', '\"old\"')")
            con.commit()
            settings.refresh(con)
            settings.ensure_security(con)
            v = settings.all_values(con)
        self.assertTrue(settings.verify_password(v["auth_password"], "old"))
        self.assertTrue(settings.is_hashed(v["auth_password"]))
        self.assertEqual(len(v["session_secret"]), 64)

    def test_legacy_url_without_scheme_does_not_block_other_changes(self):     # round 2 regression
        # stored unchecked by v0.1.0 (komga_url) and the pre-fix build (gotify_url); the form posts both back
        with db.connect(self.path) as con:
            con.execute("INSERT INTO setting (key, value) VALUES ('komga_url', '\"192.168.1.213:25600\"'),"
                        " ('gotify_url', '\"gotify.lan\"'), ('gotify_token', '\"TOK\"')")
            con.commit()
            settings.refresh(con)
            self.assertEqual(settings.invalid_urls(settings.all_values(con)), {"komga_url", "gotify_url"})
            form = {k: v for k, v in settings.masked(settings.all_values(con)).items()}
            form["refresh_hours"] = "12"
            settings.set_many(con, form)                                      # everything echoed back, one edit
            settings.set_many(con, {"komga_url": " 192.168.1.213:25600 ", "min_pages": "9"})
            v = settings.all_values(con)
            self.assertEqual((v["refresh_hours"], v["min_pages"]), (12.0, 9))
            self.assertEqual((v["komga_url"], v["gotify_url"], v["gotify_token"]),
                             ("192.168.1.213:25600", "gotify.lan", "TOK"))   # kept as stored, secret not unbound
            with self.assertRaises(ValueError):                               # a changed value is still checked
                settings.set_many(con, {"komga_url": "192.168.1.214:25600", "refresh_hours": "3"})
            self.assertEqual(settings.all_values(con)["refresh_hours"], 12.0)  # nothing saved
            settings.set_many(con, {"komga_url": "http://192.168.1.213:25600"})
            self.assertEqual(settings.invalid_urls(settings.all_values(con)), {"gotify_url"})


if __name__ == "__main__":
    unittest.main()

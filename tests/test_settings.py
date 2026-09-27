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


if __name__ == "__main__":
    unittest.main()

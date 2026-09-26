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


if __name__ == "__main__":
    unittest.main()

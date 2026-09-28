"""komga.library_settings and series_books, the checks a rename makes before
it touches a file, against a fake Komga (fake_komga.py): nothing reaches a
real one."""
import os
import sys
import tempfile
import unicodedata
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_komga import FakeKomga  # noqa: E402

from mangarr import db, komga, settings  # noqa: E402


class KomgaBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.komga = FakeKomga()
        for p in (mock.patch("mangarr.config.DB_PATH", os.path.join(tmp.name, "t.db")),
                  mock.patch.object(komga, "_call", self.komga.call)):
            p.start()
            self.addCleanup(p.stop)
        settings._cache.clear()
        self.addCleanup(settings._cache.clear)
        with db.connect() as con:
            settings.set_many(con, {"komga_url": "http://komga:25600", "komga_api_key": "k"})


class LibrarySettingsTest(KomgaBase):
    def test_hashing_on_and_off(self):
        lib = komga.library_settings("LIB1")
        self.assertEqual(lib, {"id": "LIB1", "name": "Manga (mang-arr)", "root": "/library", "hash_files": True,
                               "empty_trash_after_scan": False})
        self.komga.add_library("LIB2", "Old manga", hash_files=False)
        self.assertFalse(komga.library_settings("LIB2")["hash_files"])
        self.assertEqual(self.komga.calls[-1], ("GET", "/api/v1/libraries/LIB2"))

    def test_the_id_is_quoted(self):
        with self.assertRaises(urllib.error.HTTPError):
            komga.library_settings("../series?x=1")
        self.assertEqual(self.komga.calls[-1], ("GET", "/api/v1/libraries/..%2Fseries%3Fx%3D1"))

    def test_an_answer_that_is_not_a_library(self):
        for answer in (None, [], {"name": "no id"}):
            with mock.patch.object(komga, "_call", return_value=(200, answer)), self.assertRaises(ValueError):
                komga.library_settings("LIB1")

    def test_hashing_is_only_on_when_komga_says_true(self):
        self.komga.libraries["LIB1"]["hashFiles"] = "yes"
        self.assertFalse(komga.library_settings("LIB1")["hash_files"])


class SeriesBooksTest(KomgaBase):
    def test_found_by_folder_name(self):
        self.komga.add_series("S1", "Berserk", ["Chapter 001.0.cbz", "Chapter 002.0.cbz"], hashed=[True, False])
        self.komga.add_series("S2", "Berserk of Gluttony", ["Chapter 001.0.cbz"])
        got = komga.series_books("Berserk", "LIB1")
        self.assertEqual(got["series_id"], "S1")
        self.assertEqual(got["library_id"], "LIB1")
        self.assertEqual([(b["id"], b["file_hash"]) for b in got["books"]], [("S1-0", "hash0"), ("S1-1", "")])
        self.assertEqual(got["books"][0]["url"], "/library/Berserk/Chapter 001.0.cbz")
        self.assertIsNone(komga.series_books("Vinland Saga", "LIB1"))

    def test_folder_names_compare_as_nfc_on_any_path_style(self):
        self.komga.add_series("S1", unicodedata.normalize("NFD", "Pokémon"), ["a.cbz"])
        self.komga.add_series("S2", "Wind Breaker", ["a.cbz"], root="D:\\Manga")
        self.komga.series[1]["url"] = "D:\\Manga\\Wind Breaker\\"
        self.assertEqual(komga.series_books("Pokémon", "LIB1")["series_id"], "S1")
        self.assertEqual(komga.series_books("Wind Breaker", "LIB1")["series_id"], "S2")

    def test_only_the_chosen_library(self):
        self.komga.add_library("OLD", "Manga")
        self.komga.add_series("S1", "Berserk", ["a.cbz"], library="OLD", root="/manga")
        self.assertIsNone(komga.series_books("Berserk", "LIB1"))
        self.assertEqual(komga.series_books("Berserk", "OLD")["series_id"], "S1")
        self.assertIn(("GET", "/api/v1/series?deleted=false&library_id=OLD&page=0&size=200"), self.komga.calls)

    def test_every_library_without_one(self):
        self.komga.add_library("OLD", "Manga")
        self.komga.add_series("S1", "Berserk", ["a.cbz"], library="OLD", root="/manga")
        self.assertEqual(komga.series_books("Berserk")["library_id"], "OLD")
        self.komga.add_series("S2", "Berserk", ["a.cbz"])
        with self.assertRaises(komga.AmbiguousSeries):
            komga.series_books("Berserk")
        self.assertEqual(komga.series_books("Berserk", "LIB1")["series_id"], "S2")

    def test_trash_is_left_out(self):
        self.komga.add_series("S1", "Berserk", ["a.cbz"], deleted=True)
        self.assertIsNone(komga.series_books("Berserk", "LIB1"))
        self.komga.add_series("S2", "Berserk", ["a.cbz", "b.cbz"])
        self.komga.books["S2"][1]["deleted"] = True
        self.assertEqual([b["id"] for b in komga.series_books("Berserk", "LIB1")["books"]], ["S2-0"])

    def test_every_page(self):
        self.komga.add_series("S1", "Berserk", [f"Chapter {i:03d}.0.cbz" for i in range(7)])
        for i in range(5):
            self.komga.add_series(f"X{i}", f"Other {i}", ["a.cbz"])
        with mock.patch.object(komga, "PAGE_SIZE", 2):
            got = komga.series_books("Other 4", "LIB1")
            self.assertEqual(got["series_id"], "X4")                      # on the third page of series
            self.assertEqual(len(komga.series_books("Berserk", "LIB1")["books"]), 7)

    def test_endless_pages_are_refused(self):
        self.komga.add_series("S1", "Berserk", ["a.cbz"])
        self.komga.endless = True
        with mock.patch.object(komga, "MAX_PAGES", 3), self.assertRaises(ValueError):
            komga.series_books("Berserk", "LIB1")
        self.assertEqual(len(self.komga.calls), 3)

    def test_an_answer_that_is_not_a_page(self):
        with mock.patch.object(komga, "_call", return_value=(200, {"items": []})), self.assertRaises(ValueError):
            komga.series_books("Berserk", "LIB1")

    def test_errors_are_raised(self):
        self.komga.fail = urllib.error.URLError("connection refused")
        with self.assertRaises(urllib.error.URLError):
            komga.series_books("Berserk", "LIB1")


if __name__ == "__main__":
    unittest.main()

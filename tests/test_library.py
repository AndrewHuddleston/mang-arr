import os
import tempfile
import unittest

from mangarr.library import (
    chapter_filename,
    fmt_number,
    link_into_library,
    parse_number,
    parse_season,
    safe_title,
    scan_series_dir,
)
from mangarr.resolver import ranges


class ParseTest(unittest.TestCase):
    cases = {
        "Official_Chapter 10.cbz": 10.0,
        "www.natomanga.com_Chapter 174.cbz": 174.0,
        "Chapter 012.5.cbz": 12.5,
        "Official_Episode 7.cbz": 7.0,
        "Unknown_# 3.cbz": 3.0,
        "Official_Page. 12.cbz": 12.0,
        "Unknown_Day 4.cbz": 4.0,
        "Unknown_Mission 21.cbz": 21.0,
        "Official_Room 9.cbz": 9.0,
        "Unknown_Act 2.cbz": 2.0,
        "Unknown_bullet 5.cbz": 5.0,
        "Official_Step. 8.cbz": 8.0,
        "Vol.3 chapter 21.cbz": 21.0,
        "Boredom Society_Vol.2 Ch.11.5 - Extra Chapter.cbz": 11.5,
        "_a_nonymous_Vol.1 Ch.3.5 - Twitter Extra.cbz": 3.5,
        "Chapter 61 - Episode 61.cbz": 61.0,
        "Chapter 16.5.cbz": 16.5,
        "Official_Chapter 0.cbz": 0.0,
        "Ch.100.cbz": 100.0,
    }

    def test_numbers(self):
        for name, want in self.cases.items():
            with self.subTest(name=name):
                self.assertEqual(parse_number(name), want)

    def test_season_based(self):
        self.assertEqual(parse_number("Official_S2 - Episode 5.0.cbz"), 200005.0)
        self.assertEqual(parse_season("Official_S2 - Episode 5.0.cbz"), (2, 5.0))
        self.assertEqual(chapter_filename(200005.0), "S02 - Episode 005.0.cbz")
        self.assertEqual(parse_number(chapter_filename(200005.0)), 200005.0)
        self.assertEqual(fmt_number(200005.0), "S2E5")
        self.assertEqual(ranges([100000.0, 100001.0, 100002.0, 200000.0]), "S1E0-S1E2, S2E0")
        self.assertLess(chapter_filename(100012.0), chapter_filename(200001.0))

    def test_no_number(self):
        self.assertIsNone(parse_number("Official_Prologue.cbz"))


class NamesTest(unittest.TestCase):
    def test_filename_sorts(self):
        names = [chapter_filename(n) for n in (12.0, 12.5, 100.0, 1.0, 16.0, 16.5)]
        self.assertEqual(sorted(names), [chapter_filename(n) for n in (1.0, 12.0, 12.5, 16.0, 16.5, 100.0)])

    def test_safe_title(self):
        self.assertEqual(safe_title('Wind Breaker: The "Real" One?'), "Wind Breaker_ The _Real_ One_")
        self.assertEqual(safe_title("Ends with dot."), "Ends with dot")


class LinkTest(unittest.TestCase):
    def test_scan_and_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = os.path.join(tmp, "src")
            os.makedirs(staging)
            for name in ("Official_Chapter 1.cbz", "Official_Chapter 2.5.cbz", "notes.txt", "Official_Extra.cbz"):
                with open(os.path.join(staging, name), "wb") as f:
                    f.write(b"x")
            found, unparsed = scan_series_dir(staging)
            self.assertEqual(sorted(found), [1.0, 2.5])
            self.assertEqual(len(unparsed), 1)
            lib = os.path.join(tmp, "lib")
            folder = safe_title("A: Title")
            dst = link_into_library(found[2.5], folder, 2.5, root=lib)
            self.assertEqual(dst, os.path.join(lib, "A_ Title", "Chapter 002.5.cbz"))
            self.assertEqual(os.stat(dst).st_nlink, 2)
            # idempotent
            self.assertEqual(link_into_library(found[2.5], folder, 2.5, root=lib), dst)
            # never overwrites a file it did not make
            with open(dst, "wb") as f:
                f.write(b"other")
            self.assertIsNone(link_into_library(found[1.0], folder, 2.5, root=lib))
            self.assertEqual(link_into_library(found[1.0], folder, 2.5, root=lib, replace=True), dst)

    def test_two_decimal_chapter_names(self):
        self.assertEqual(chapter_filename(5.25), "Chapter 005.25.cbz")
        self.assertEqual(chapter_filename(12.0), "Chapter 012.0.cbz")
        self.assertEqual(parse_number("Chapter 005.25.cbz"), 5.25)

    def test_volume_only_file_is_not_a_chapter(self):
        self.assertIsNone(parse_number("Official_Vol.3.cbz"))
        self.assertEqual(parse_number("Vol.3 Ch.21.cbz"), 21.0)


if __name__ == "__main__":
    unittest.main()

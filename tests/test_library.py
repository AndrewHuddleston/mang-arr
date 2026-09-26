import os
import tempfile
import unittest

from mangarr.library import (
    chapter_filename,
    chapter_label,
    link_into_library,
    parse_number,
    parse_season,
    safe_title,
    scan_series_dir,
    suwayomi_name_map,
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
        # a season episode has no global number in its name: import matches it via Suwayomi
        self.assertIsNone(parse_number("Official_S2 - Episode 5.0.cbz"))
        self.assertEqual(parse_season("Official_S2 - Episode 5.0.cbz"), (2, 5.0))
        # ... and once in the library the number leads and the label follows
        name = chapter_filename(131.0, "S2 - Episode 5")
        self.assertEqual(name, "Chapter 131.0 - S2 - Episode 5.cbz")
        self.assertEqual(parse_number(name), 131.0)

    def test_labels(self):
        self.assertIsNone(chapter_label(12.0, "Chapter 12"))
        self.assertIsNone(chapter_label(12.0, "Ch.12"))
        self.assertIsNone(chapter_label(12.5, "Chapter 12.5"))
        self.assertEqual(chapter_label(12.0, "Chapter 12: The Storm"), "Chapter 12_ The Storm")
        self.assertEqual(chapter_filename(1.0, "S1 - Episode 0"), "Chapter 001.0 - S1 - Episode 0.cbz")
        self.assertEqual(ranges([1.0, 2.0, 3.0, 5.5]), "1-3, 5.5")

    def test_suwayomi_name_map(self):
        from mangarr.suwayomi import Chapter
        m = suwayomi_name_map([Chapter(1, 131.0, "S2 - Episode 5", "Official", False),
                               Chapter(2, 1.0, "Chapter 1", None, False)])
        self.assertEqual(m["official_s2 - episode 5"], 131.0)
        self.assertEqual(m["chapter 1"], 1.0)

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

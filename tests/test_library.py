import os
import tempfile
import unittest

from mangarr.library import (
    chapter_filename,
    chapter_label,
    link_into_library,
    match_unparsed,
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
        self.assertEqual(chapter_label(12.0, "Chapter 12: The Storm"), "The Storm")
        self.assertEqual(chapter_label(133.0, "Chapter 133 - [Part 2] Ep.65"), "[Part 2] Ep.65")
        self.assertEqual(chapter_label(5.0, "The Storm"), "The Storm")
        self.assertEqual(chapter_filename(1.0, "S1 - Episode 0"), "Chapter 001.0 - S1 - Episode 0.cbz")
        self.assertEqual(ranges([1.0, 2.0, 3.0, 5.5]), "1-3, 5.5")

    def test_suwayomi_name_map(self):
        from mangarr.suwayomi import Chapter
        m = suwayomi_name_map([Chapter(1, 131.0, "S2 - Episode 5", "Official", False),
                               Chapter(2, 1.0, "Chapter 1", None, False)])
        self.assertEqual(m["official_s2 - episode 5"], 131.0)
        self.assertEqual(m["chapter 1"], 1.0)
        self.assertEqual(match_unparsed("/x/Official_S2 - Episode 5.cbz", m), 131.0)
        self.assertEqual(match_unparsed("/x/S2 - Episode 005.0.cbz", m), 131.0)   # renamed/padded file
        self.assertIsNone(match_unparsed("/x/S3 - Episode 1.cbz", m))

    def test_no_number(self):
        self.assertIsNone(parse_number("Official_Prologue.cbz"))


class ChapterMarkerTest(unittest.TestCase):
    """A chapter marker after a scanlator or site prefix decides the number:
    never a number from the prefix or from the title after the marker. The
    first 17 names are real files 0.3.0 read wrong (2026-09-28): three were
    linked into the library as chapters 2020, 101 and 2026, the others were
    taken for chapters already on disk and never imported."""
    real = {
        "Losers in eXile_Ch.150 - Hana to Yume March 2020 Special.cbz": 150.0,
        "Humane Scans_Ch.17 - Maidens 101_ A Success_.cbz": 17.0,
        "_a_nonymous_Ch.185.5 - Twitter Extra - Valentine's Day 2026.cbz": 185.5,
        "www.natomanga.com_Chapter 9.1_ Hana-kun\u2019s Obsession is a Powerful Poison - Part 1.cbz": 9.1,
        **{f"www.natomanga.com_Chapter 171.{i:02d}_ Spin-off {i}.cbz": float(f"171.{i:02d}")
           for i in (1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13)},
        "www.natomanga.com_Chapter 171.1_ Spin-off 10.cbz": 171.1,
    }
    variants = {
        # the marker in any case, with or without a dot or a space
        "Humane Scans_ch 17 - Maidens 101.cbz": 17.0,
        "Humane Scans_CH.17 - Maidens 101.cbz": 17.0,
        "Humane Scans_Ch17 Maidens 101.cbz": 17.0,
        "Scans_CHAPTER 12_ Room 404.cbz": 12.0,
        "Scans_chapter12.cbz": 12.0,
        "Scans_Chap. 8 - 2020 Special.cbz": 8.0,
        "Team_Episode 3 - Day 2020.cbz": 3.0,
        "Team_Ep. 4 - Part 9.cbz": 4.0,
        "Team_EP 4_ Stage 7.cbz": 4.0,
        # after a volume, after a prefix with digits of its own, after a prefix full of "_"
        "www.natomanga.com_Vol.3 Chapter 21_ 1000 Cranes.cbz": 21.0,
        "Humane Scans_Vol.2 Ch.11 - Winter 2020.cbz": 11.0,
        "Uploaded by Phuocphuc46_Vol.25 Ch.149.6.cbz": 149.6,
        "Scans 2020_Ch.5 - Five.cbz": 5.0,
        "Group 101_Chapter 7.cbz": 7.0,
        "_a_nonymous_Vol.1 Ch.3.5 - Day 14 Extra.cbz": 3.5,
        "__Chapter 2_ Part 3.cbz": 2.0,
        # a chapter marker before another keyword: the marker wins
        "Scans_Day 5 - Chapter 12.cbz": 12.0,
        # the marker's number even when it is 0 and a volume follows
        "www.natomanga.com_Chapter 0_ Volume 10.cbz": 0.0,
        # a word that only contains a marker is not one
        "Rich 2_Chapter 3.cbz": 3.0,
        "Deep Epilogue 2.cbz": 2.0,
        "Scans_Chapters 1-3 Recap 4.cbz": 4.0,
        # other keywords after a prefix count like before, and the last number without any
        "Unknown_Day 4 - 2020.cbz": 4.0,
        "Unknown_#3 - Room 101.cbz": 3.0,
        "Oneshot 2020.cbz": 2020.0,
    }

    # real names downloaded after those (11:15 UTC the same day), which 0.3.0 read as chapter 0 (the 0 of "1r0n")
    later = {
        "www.natomanga.com_Chapter 16.1_ (1r0n).cbz": 16.1,
        "www.natomanga.com_Chapter 4.1_ (1r0n).cbz": 4.1,
        "www.natomanga.com_Chapter 14.1_ (1r0n).cbz": 14.1,
        "www.natomanga.com_Chapter 49.1_ (1r0n).cbz": 49.1,
    }

    def test_real_names(self):
        for name, want in {**self.real, **self.later}.items():
            with self.subTest(name=name):
                self.assertEqual(parse_number(name), want)
        self.assertEqual(len(self.real), 17)

    def test_variants(self):
        for name, want in self.variants.items():
            with self.subTest(name=name):
                self.assertEqual(parse_number(name), want)

    def test_season_episodes_after_a_prefix_stay_unnumbered(self):
        for name in ("www.natomanga.com_S2 - Episode 5_ Part 1.cbz", "Humane Scans_S2 - Ch.5 - 2020.cbz",
                     "_a_nonymous_S2 - Ep. 5.cbz"):
            with self.subTest(name=name):
                self.assertIsNone(parse_number(name))
                self.assertEqual(parse_season(name), (2, 5.0))
        self.assertEqual(parse_number("Team_Part 1 - S2 - Episode 5.cbz"), 1.0)     # a keyword before the season

    def test_suwayomi_file_names_read_as_suwayomi_numbers(self):
        """The file Suwayomi writes for a chapter ('<scanlator>_<name>', made
        safe) reads as the number Suwayomi gives the chapter, so import and
        the name map (match_unparsed) agree on every one of these."""
        from mangarr.suwayomi import Chapter
        chapters = [
            Chapter(1, 171.01, "Chapter 171.01: Spin-off 1", "www.natomanga.com", True),
            Chapter(2, 171.1, "Chapter 171.1: Spin-off 10", "www.natomanga.com", True),
            Chapter(3, 150.0, "Ch.150 - Hana to Yume March 2020 Special", "Losers in eXile", True),
            Chapter(4, 17.0, "Ch.17 - Maidens 101: A Success!", "Humane Scans", True),
            Chapter(5, 185.5, "Ch.185.5 - Twitter Extra - Valentine's Day 2026", "/a/nonymous", True),
            Chapter(6, 9.1, "Chapter 9.1: Hana-kun\u2019s Obsession is a Powerful Poison - Part 1", "www.natomanga.com",
                    True),
            Chapter(7, 0.0, "Chapter 0: Volume 10", "www.natomanga.com", True),
        ]
        names = suwayomi_name_map(chapters)
        for c in chapters:
            path = "/staging/Src/Series/" + safe_title(f"{c.scanlator}_{c.name}") + ".cbz"
            with self.subTest(name=path):
                self.assertEqual(parse_number(path), c.number)
                self.assertEqual(match_unparsed(path, names), c.number)


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



class VerifyTest(unittest.TestCase):
    def test_verify_and_quarantine(self):
        import zipfile

        from mangarr.library import quarantine, verify_archive
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.cbz")
            with zipfile.ZipFile(good, "w") as z:
                z.writestr("001.jpg", b"\xff\xd8" + b"x" * 2000)
            self.assertEqual(verify_archive(good), (True, "1 pages"))
            empty = os.path.join(tmp, "empty.cbz")
            with open(empty, "wb") as f:
                f.write(b"")
            self.assertFalse(verify_archive(empty)[0])
            noimg = os.path.join(tmp, "noimg.cbz")
            with zipfile.ZipFile(noimg, "w") as z:
                z.writestr("readme.txt", b"x" * 2000)
            self.assertEqual(verify_archive(noimg), (False, "no images inside"))
            moved = quarantine(noimg)
            self.assertTrue(moved.endswith(".corrupt") and os.path.exists(moved) and not os.path.exists(noimg))


if __name__ == "__main__":
    unittest.main()

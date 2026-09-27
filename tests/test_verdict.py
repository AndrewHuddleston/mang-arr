"""The "stuck behind chapter N" verdict and MangaDex's English chapter list,
from real cases and from the adversarial check's false positives. MangaDex is
faked (its answers below are trimmed copies of the real ones); nothing here
reaches the network."""
import email.message
import io
import json
import sqlite3
import time
import unittest
import urllib.error
from unittest import mock

from mangarr import db, mangadex, verdict
from mangarr.mangadex import ChapterList, MdChapter
from mangarr.model import Series
from mangarr.verdict import HIGH, LOW, Blocker, Chapter, Verdict, classify, sign


def have(numbers, pages, source="Weeb Central"):
    return [Chapter(float(n), "have", None, source, pages(n) if callable(pages) else pages) for n in numbers]


# -- the real cases ----------------------------------------------------------

DANGERS = Series(anilist_id=101557, romaji="Boku no Kokoro no Yabai Yatsu", english="The Dangers in My Heart",
                 status="FINISHED")
DANGERS_ROWS = [*have(range(1, 40), 11), Chapter(7.5, "have", "Twitter Extra", "Weeb Central", 2),
                Chapter(7.2, "failed", "Chapter 7.2", "Manganato"), *[Chapter(float(n), "wanted") for n in range(40, 60)]]
# MangaDex's English list: ... 6, 6.1, 7, 7.5, 8 ... up to 199 (515 numbers)
DANGERS_MD = ChapterList("3df1a9a3-a1be-47a3-9e90-9b3e55b1d0ac", 515, {
    7.0: MdChapter(7.0, "I Mixed It Around", 11), 7.5: MdChapter(7.5, "Twitter Extra - Update Schedule 📅", 2)},
    following=8.0, highest=199.0)

TAMON = Series(anilist_id=140495, romaji="Tamon-kun Ima Docchi!?", english="Tamon's B-Side", status="RELEASING")
TAMON_ROWS = [Chapter(1.0, "have", "SWITCH 1", "Weeb Central", 53), *have(range(2, 25), lambda n: 30 + n % 7),
              Chapter(1.1, "failed", "Chapter 1.1", "Manganato"), Chapter(1.2, "wanted", "Chapter 1.2", "Manganato")]
# MangaDex's English list: 1, 2, 3, 4, 4.5, 5, then 50 ... 65, 79, 80
TAMON_MD = ChapterList("cfcc577d-14a3-4332-beaa-346068d2afbd", 25, {1.0: MdChapter(1.0, "SWITCH 1", 53)},
                       following=2.0, highest=80.0)

FREEDOM = Series(anilist_id=110000, english="Dreaming Freedom", status="FINISHED", chapters=171)
FREEDOM_ROWS = [*have(range(1, 172), lambda n: 20 + n % 5),
                *[Chapter(171 + i / 100, "wanted", f"Spin-off {i}", "Manganato") for i in range(1, 14)],
                Chapter(171.14, "wanted", "Afterword", "Manganato")]

NOZAKI = Series(anilist_id=59211, romaji="Gekkan Shoujo Nozaki-kun", english="Monthly Girls' Nozaki-kun",
                status="RELEASING")
NOZAKI_ROWS = [Chapter(1.0, "have", None, "Weeb Central", 15), *have(range(2, 40), lambda n: 28 + n % 5),
               Chapter(1.1, "failed", "Chapter 1.1", "Manganato")]
# MangaDex's English list starts at 1.5 "First Issue", hosted on the publisher's site (0 pages there), then 2
NOZAKI_MD = ChapterList("bc634b22-0c05-4a55-9b7e-3f55438a1ad3", 122, {1.5: MdChapter(1.5, "First Issue", None)},
                        following=2.0)

WRONG = Series(anilist_id=120000, english="Something's Wrong With Us", status="RELEASING")
WRONG_ROWS = [*have(range(1, 30), 36), Chapter(15.5, "failed", "Chapter 15.5", "Manganato")]


class RealCasesTest(unittest.TestCase):
    def test_dangers_7_2_is_covered(self):
        v = classify(DANGERS, Blocker(7.2, {"Manganato": "Chapter 7.2"}), DANGERS_ROWS, DANGERS_MD)
        self.assertEqual((v.kind, v.confidence), ("covered", HIGH))
        self.assertTrue(v.skippable)
        self.assertTrue(v.auto_skip)
        self.assertEqual(v.headline, "Probably already covered by chapter 7 you have")
        self.assertEqual(v.evidence, ["MangaDex's English chapter list has 7 and 7.5 but no 7.2; you have both.",
                                      "It goes on with chapter 8, so it does not simply end before 7.2.",
                                      "Weeb Central, where your chapter 8 is from, does not list 7.2.",
                                      "Your chapter 7 has 11 pages, like MangaDex's chapter 7 (11)."])

    def test_tamon_1_1_is_covered(self):
        v = classify(TAMON, Blocker(1.1, {"Manganato": "Vol.1 Chapter 1.1"}), TAMON_ROWS, TAMON_MD)
        self.assertEqual((v.kind, v.confidence), ("covered", HIGH))
        self.assertIn("MangaDex's English chapter list has chapter 1 but no 1.1; you have it.", v.evidence)
        self.assertIn("Your chapter 1 has 53 pages, like MangaDex's chapter 1 (53).", v.evidence)

    def test_dreaming_freedom_spin_offs_and_afterword_are_side_stories(self):
        for n, name in ((171.01, "Chapter 171.01: Spin-off 1"), (171.13, "Spin-off 13"), (171.14, "Afterword")):
            with self.subTest(n):
                v = classify(FREEDOM, Blocker(n, {"Manganato": name}), FREEDOM_ROWS, None)
                self.assertEqual((v.kind, v.confidence), ("side_story", HIGH))
                self.assertTrue(v.auto_skip)
                self.assertEqual(v.evidence[0], f'Manganato names it "{name}", which marks a side story or extra.')
                self.assertIn(f"The series is finished and its last chapter is 171; {n:g} comes after it, where "
                              f"epilogues and extras go.", v.evidence)

    def test_nozaki_1_1_is_the_rest_of_chapter_1(self):
        v = classify(NOZAKI, Blocker(1.1, {"Manganato": "Chapter 1.1"}), NOZAKI_ROWS, NOZAKI_MD)
        self.assertEqual((v.kind, v.confidence), ("rest_of_chapter", LOW))
        self.assertFalse(v.skippable)
        self.assertEqual(v.headline, "Probably the rest of chapter 1 (skipping leaves a gap)")
        self.assertEqual(v.evidence, ["Your chapter 1 has 15 pages while this series' chapters usually have about 30: "
                                      "the site probably split chapter 1 in two."])

    def test_nozaki_mangadex_without_chapter_1_is_no_evidence(self):
        v = classify(NOZAKI, Blocker(1.1, {"Manganato": "Chapter 1.1"}),
                     [r if r.number != 1 else Chapter(1.0, "have", None, "WC", 30) for r in NOZAKI_ROWS], NOZAKI_MD)
        self.assertEqual(v.kind, "unknown")
        self.assertIn("MangaDex's English chapter list has no chapter 1 (it lists 1.5), so it cannot compare.",
                      v.evidence)

    def test_somethings_wrong_15_5_is_unknown(self):
        for md in (None, ChapterList(None)):
            with self.subTest(md=md):
                v = classify(WRONG, Blocker(15.5, {"Manganato": "Chapter 15.5"}), WRONG_ROWS, md)
                self.assertEqual((v.kind, v.confidence), ("unknown", LOW))
                self.assertFalse(v.skippable)
                self.assertEqual(v.headline, "Unknown: no clear sign either way")
                self.assertIn("Chapters numbered .5 are often extras, but that alone does not decide it.", v.evidence)
                self.assertIn('It has no title of its own, only "Chapter 15.5".', v.evidence)
        v = classify(WRONG, Blocker(15.5, {"Manganato": "Chapter 15.5"}), WRONG_ROWS, ChapterList(None))
        self.assertIn("No MangaDex entry found under this series' titles links to its AniList entry, so MangaDex's "
                      "chapter list cannot help.", v.evidence)


# -- names ---------------------------------------------------------------------

class SignTest(unittest.TestCase):
    S = Series(english="Some Series")

    def test_side_story_words_and_romanised_forms(self):
        for name in ("Side Story 3", "Sidestory", "Spin-off 1", "Spin Off", "Omake", "Afterword", "Gaiden 2",
                     "Bangaihen", "Bangai-hen", "Fanwai 1", "Oejeon 4", "Atogaki", "番外編", "外伝", "외전 1",
                     "Side Story 2 (Part 2)"):
            with self.subTest(name):
                self.assertEqual(sign(name, self.S), "side")

    def test_extras_and_bonuses(self):
        for name in ("Extra", "Extra 2", "Extras", "Extra: Beach Day", "Chapter 45.5 - Extra Chapter",
                     "Twitter Extra", "Volume 2 Extra", "Christmas Extra - Special"):
            with self.subTest(name):
                self.assertEqual(sign(name, self.S), "extra")
        for name in ("Bonus Chapter", "Volume 3 Bonus", "Christmas Special", "Special", "New Year’s Special",
                     "1st Anniversary Special", "10 Million Views Special", "Christmas Special, Part 2"):
            with self.subTest(name):
                self.assertEqual(sign(name, self.S), "bonus")

    def test_ordinary_words_are_not_extras(self):
        for name in ("Extra Innings", "Special Training", "Bonus Round", "The Spinning Top", "I Mixed It Around",
                     "Just an Extra", "Becoming an Extra", "I'm Not Special", "Nothing Special",
                     "Everyone Is Special", "Signing Bonus", "The Bonus", "You're Special (1)", "12a",
                     "Chapter 15.5", "Vol.1 Chapter 7.2", "Ch. 12", "Final Chapter Special", "", None):
            with self.subTest(name):
                self.assertIsNone(sign(name, self.S))

    def test_notices(self):
        for name in ("Hiatus Notice", "Announcement", "Update Schedule", "Twitter Extra - Update Schedule",
                     "We are recruiting!", "Release postponed", "Series on Hiatus", "休載のお知らせ", "Hiatus",
                     "The Announcement", "The Schedule", "Recruitment", "公告栏的秘密"):
            with self.subTest(name):
                self.assertEqual(sign(name, self.S), "junk")

    def test_notice_words_in_a_title_are_not_a_notice(self):
        for name in ("Notice Me, Senpai", "Did You Notice?", "Without Notice", "The Engagement Announcement",
                     "Recruiting Party Members", "Guild Recruitment", "The Wedding Is Postponed",
                     "Chapter 12: The Duel - Hiatus Notice"):
            with self.subTest(name):
                self.assertIsNone(sign(name, self.S))

    def test_next_part_and_epilogue(self):
        for name in ("Part 2", "The Duel (Part II)", "Pt. 2", "Second Half", "(2/2)", "The Festival (cont.)",
                     "Something Special, Part 2"):
            with self.subTest(name):
                self.assertEqual(sign(name, self.S), "part")
        self.assertEqual(sign("Epilogue", self.S), "epilogue")

    def test_series_title_in_the_name_does_not_count(self):
        s = Series(english="The Novel's Extra", synonyms=["Trash of the Count’s Family Special"])
        self.assertIsNone(sign("The Novel's Extra Chapter 45", s))
        self.assertIsNone(sign("The Novel’s Extra - Chapter 45", s))
        self.assertIsNone(sign("The Novelʼs Extra - Chapter 45", s))           # any apostrophe (plain_quotes)
        self.assertIsNone(sign("Trash of the Count's Family Special 12", s))
        self.assertEqual(sign("The Novel's Extra - Side Story 1", s), "side")

    def test_scraped_names_cost_little_and_stay_one_line(self):
        s = Series(english="X", synonyms=["y" * 300] * 3)
        for name in ("(" * 50_000, "Chapter 1 " * 5_000, " -" * 25_000, "extra\n" * 8_000, "part " * 10_000):
            t0 = time.monotonic()
            v = classify(s, Blocker(3.5, {"Evil\nSource": name}), have(range(1, 10), 20))
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertTrue(all("\n" not in e and len(e) < 400 for e in v.evidence), v.evidence)


# -- staying careful -----------------------------------------------------------

def md_list(*chapters, following=None, highest=None, count=40):
    return ChapterList("u", count, {c.number: c for c in chapters}, following, highest)


class ConservativeTest(unittest.TestCase):
    ROWS = [*have(range(1, 30), 30), Chapter(7.5, "failed")]
    SHORT_7 = [*have([n for n in range(1, 30) if n != 7], 30), Chapter(7.0, "have", None, "WC", 12),
               Chapter(7.5, "failed")]

    def test_an_extras_name_with_a_short_chapter_before_it_disagrees(self):
        v = classify(Series(english="S"), Blocker(7.5, {"Manganato": "Extra"}), self.SHORT_7)
        self.assertEqual(v.kind, "unknown")
        self.assertEqual(v.headline, "Unknown: the signs disagree")

    def test_an_extras_name_alone_is_a_side_story(self):
        v = classify(Series(english="S"), Blocker(7.5, {"Manganato": "Extra"}), self.ROWS)
        self.assertEqual((v.kind, v.confidence), ("side_story", HIGH))

    def test_a_notice_s_name_never_decides(self):
        for n in (7.5, 12):
            for name in ("Hiatus Notice", "The Announcement", "公告"):
                with self.subTest(n=n, name=name):
                    v = classify(Series(english="S"), Blocker(n, {"Manganato": name}),
                                 [*have([x for x in range(1, 30) if x != n], 30), Chapter(n, "failed")])
                    self.assertEqual((v.kind, v.headline), ("unknown", "Unknown: no clear sign either way"))
                    self.assertIn(f'Manganato names it "{name}", which reads like a notice, but notice words are in '
                                  f'chapter titles too, so that never decides it.', v.evidence)

    def test_a_title_with_notice_words_is_a_title(self):
        rows = [*have(range(1, 30), 30), Chapter(12.5, "failed")]
        v = classify(Series(english="S"), Blocker(12.5, {"Manganato": "Chapter 12.5: The Engagement Announcement"}),
                     rows)
        self.assertEqual((v.kind, v.skippable), ("unknown", False))
        self.assertEqual(v.headline, "Unknown: it has a title of its own")
        self.assertIn('Manganato names it "Chapter 12.5: The Engagement Announcement": a title of its own, so '
                      'probably a chapter of its own.', v.evidence)

    def test_a_notice_with_a_chapter_s_page_count_is_a_chapter(self):
        rows = [*have(range(1, 30), 30), Chapter(12.5, "failed")]
        for title in ("The Engagement Announcement", "Hiatus Notice"):
            with self.subTest(title):
                md = md_list(MdChapter(12.0, "Twelve", 30), MdChapter(12.5, title, 31), following=13.0)
                v = classify(Series(english="S"), Blocker(12.5, {}), rows, md)
                self.assertEqual((v.kind, v.headline), ("unknown", "Unknown: MangaDex lists 12.5 as a chapter of its own"))
                self.assertIn(f'MangaDex lists 12.5 as "{title}" (31 pages): a chapter of its own there.', v.evidence)
        v = classify(Series(english="S"), Blocker(12.5, {"Manganato": "Hiatus Notice"}),
                     [*rows[:-1], Chapter(12.5, "failed", pages=24)])
        self.assertEqual(v.kind, "unknown")                 # the source's copy has 24 pages

    def test_a_chapter_you_lack_is_not_a_notice_by_its_words(self):
        rows = [*self.ROWS, Chapter(7.2, "failed")]
        for title, pages, kind in (("Notice Me, Senpai", 28, "unknown"), ("Hiatus Notice", 28, "unknown"),
                                   ("The Duel", 3, "unknown"), ("Hiatus Notice", 2, "covered"),
                                   ("Hiatus Notice", None, "covered"), (None, 3, "covered")):
            with self.subTest(title=title, pages=pages):
                md = md_list(MdChapter(7.0, "Seven", 30), MdChapter(7.1, title, pages), following=8.0)
                v = classify(Series(english="S"), Blocker(7.2, {}), rows, md)
                self.assertEqual(v.kind, kind)
                if kind == "unknown":
                    self.assertEqual(v.headline, "Unknown: MangaDex lists 7.1, which you do not have")
                else:                               # it may still be a chapter: never certain
                    self.assertEqual(v.confidence, LOW)
                    self.assertIn("MangaDex's 7.1, which you do not have, could be a chapter rather than a notice.",
                                  v.evidence)

    def test_side_story_words_decide_only_off_the_story_s_numbering(self):
        s = Series(english="S", status="RELEASING")
        for name in ("Chapter 12: Special Chapter", "Extra", "Side Story 1", "Gaiden", "Epilogue"):
            with self.subTest(name):
                v = classify(s, Blocker(12, {"Manganato": name}), [*have(range(1, 30), 30)], ChapterList(None))
                self.assertEqual((v.kind, v.skippable), ("unknown", False))
                self.assertIn(f'Manganato names it "{name}", which may mark a side story or extra, but 12 is '
                              f'numbered as a chapter of the story, so that alone does not decide it.'
                              if name != "Epilogue" else
                              'Manganato names it "Epilogue", but 12 is numbered as a chapter of the story, so that '
                              'alone does not decide it.', v.evidence)
        md = md_list(MdChapter(12.0, "Special Chapter", 30), following=13.0)
        self.assertEqual(classify(s, Blocker(12, {}), have(range(1, 30), 30), md).kind, "unknown")
        v = classify(s, Blocker(12.5, {"Manganato": "Chapter 12.5: Something Special, Part 2"}),
                     [*have(range(1, 30), 30), Chapter(12.5, "failed")])
        self.assertEqual((v.kind, v.skippable), ("rest_of_chapter", False))

    def test_the_last_whole_chapter_is_never_after_the_end(self):
        # a finished series' last chapter called Extra, Epilogue or Omake is
        # its ending, whatever AniList's count says (none, or fewer)
        for chapters in (None, 29, 20):
            for name in ("Extra", "Epilogue", "Omake"):
                with self.subTest(chapters=chapters, name=name):
                    fin = Series(english="S", status="FINISHED", chapters=chapters)
                    v = classify(fin, Blocker(30, {"M": name}), [*have(range(1, 30), 30), Chapter(30, "failed")])
                    self.assertEqual(v.kind, "unknown")
                    self.assertFalse(any("comes after it" in e for e in v.evidence), v.evidence)

    def test_covered_needs_the_whole_length_of_a_usual_chapter(self):
        # MangaDex's chapter 7 is hosted elsewhere (no page count): your 7 is
        # compared with the usual chapter, as strictly as with MangaDex's copy
        md = md_list(MdChapter(7.0, "Seven", None), following=8.0, count=29)
        base = [*have([n for n in range(1, 30) if n != 7], 30), Chapter(7.2, "failed", "Chapter 7.2", "Manganato")]
        for pieces, kind in (([Chapter(7.0, "have", None, "WC", 20)], "unknown"),
                             ([Chapter(7.0, "have", None, "WC", 10), Chapter(7.1, "have", None, "WC", 10)], "unknown"),
                             ([Chapter(7.0, "have", None, "WC", 36)], "unknown"),
                             ([Chapter(7.0, "have", None, "WC", 27)], "covered")):
            with self.subTest(pieces):
                v = classify(Series(english="S"), Blocker(7.2, {"Manganato": "Chapter 7.2"}), [*base, *pieces], md)
                self.assertEqual(v.kind, kind)
        self.assertIn("Your chapter 7 has 27 pages, a usual length for this series (about 30).", v.evidence)
        self.assertEqual(v.confidence, LOW)                 # not against MangaDex's own copy
        self.assertIn("MangaDex has no page count for its chapter 7, so yours is only compared with this series' "
                      "usual chapter.", v.evidence)
        v = classify(Series(english="S"), Blocker(7.2, {}), [*base, Chapter(7.0, "have", None, "WC", 20)], md)
        self.assertEqual(v.evidence, ["MangaDex's English chapter list has chapter 7 but no 7.2; you have it.",
                                      "It goes on with chapter 8, so it does not simply end before 7.2.",
                                      'It has no title of its own, only "Chapter 7.2".',
                                      "Your chapter 7 has 20 pages while this series' chapters usually have about 30: "
                                      "not clearly the whole chapter."])

    def test_covered_needs_a_known_normal_length(self):
        md = md_list(MdChapter(7.0, "Seven", None), following=8.0, count=50)
        rows = [*have(range(1, 4), None), Chapter(7.0, "have"), Chapter(7.5, "failed")]
        v = classify(Series(english="S"), Blocker(7.5, {}), rows, md)
        self.assertEqual(v.kind, "unknown")
        self.assertIn("The page count of your chapter 7 is not known, so its length cannot be compared.", v.evidence)
        v = classify(Series(english="S"), Blocker(7.5, {}), [*have(range(1, 5), 30), Chapter(7.0, "have", pages=30),
                                                              Chapter(7.5, "failed")], md)
        self.assertIn("Too few chapters of this series have a known page count to tell whether your chapter 7 is "
                      "complete.", v.evidence)
        self.assertEqual(v.kind, "unknown")

    def test_a_short_copy_of_mangadex_s_chapter_is_the_rest(self):
        md = md_list(MdChapter(7.0, "Seven", 30), following=8.0, count=50)
        v = classify(Series(english="S"), Blocker(7.5, {}), self.SHORT_7, md)
        self.assertEqual(v.kind, "rest_of_chapter")
        self.assertIn("Your chapter 7 has 12 pages but MangaDex's chapter 7 has 30: yours is probably only part of it.",
                      v.evidence)

    def test_pieces_you_have_count_toward_the_length(self):
        rows = [*self.SHORT_7, Chapter(7.1, "have", None, "WC", 14), Chapter(7.2, "failed")]
        md = md_list(MdChapter(7.0, "Seven", 27), MdChapter(7.1, "Seven, second part", 12), following=8.0)
        v = classify(Series(english="S"), Blocker(7.2, {}), rows, md)
        self.assertEqual((v.kind, v.confidence), ("covered", HIGH))
        self.assertIn("Your chapters 7 and 7.1 have 26 pages together, like MangaDex's chapter 7 (27).", v.evidence)

    def test_mangadex_lists_a_chapter_you_lack(self):
        md = md_list(MdChapter(7.0, "Seven", 30), MdChapter(7.1, "The Duel", 25), following=8.0)
        v = classify(Series(english="S"), Blocker(7.2, {}), [*self.ROWS, Chapter(7.2, "failed")], md)
        self.assertEqual(v.kind, "unknown")
        self.assertIn("You do not have 7.1 from that list: 7.2 may be the same chapter under another number.",
                      v.evidence)

    def test_mangadex_extras_you_lack_do_not_matter(self):
        rows = [*self.ROWS, Chapter(7.2, "failed")]
        md = md_list(MdChapter(7.0, "Seven", 30), MdChapter(7.1, "Omake", 25), following=8.0)
        v = classify(Series(english="S"), Blocker(7.2, {}), rows, md)
        self.assertEqual((v.kind, v.confidence), ("covered", HIGH))
        self.assertIn("MangaDex's English chapter list has 7 and 7.1 but no 7.2; you have 7.", v.evidence)
        self.assertIn('MangaDex lists 7.1 as "Omake" (25 pages), an extra.', v.evidence)
        md.near[7.3] = MdChapter(7.3, None, 2)
        v = classify(Series(english="S"), Blocker(7.2, {}), rows, md)
        self.assertEqual((v.kind, v.confidence), ("covered", LOW))    # few pages: probably a notice, not certainly
        self.assertIn("MangaDex lists 7.3 (2 pages), perhaps a notice.", v.evidence)

    def test_mangadex_lists_the_blocker(self):
        s, rows = Series(english="S"), self.ROWS
        cases = [(MdChapter(7.5, "The Duel", 24), "unknown", "Unknown: MangaDex lists 7.5 as a chapter of its own"),
                 (MdChapter(7.5, "The Duel", 3), "unknown", "Unknown: MangaDex lists 7.5 as a chapter of its own"),
                 (MdChapter(7.5, "Volume 2 Extra", 6), "side_story", "Probably a side story"),
                 (MdChapter(7.5, None, 2), "unknown", "Unknown: no clear sign either way"),
                 (MdChapter(7.5, "Part 2", 20), "rest_of_chapter", "Probably the rest of chapter 7 (skipping leaves a gap)"),
                 (MdChapter(7.5, None, 20), "unknown", "Unknown: MangaDex lists 7.5 as a chapter of its own"),
                 (MdChapter(7.5, "Official", None), "unknown", "Unknown: MangaDex lists 7.5 as a chapter of its own")]
        for c, kind, headline in cases:
            with self.subTest(c):
                v = classify(s, Blocker(7.5, {}), rows, md_list(MdChapter(7.0), c, following=8.0))
                self.assertEqual((v.kind, v.headline, v.confidence), (kind, headline, LOW))
        v = classify(s, Blocker(7.5, {"Manganato": "Extra"}), rows, md_list(MdChapter(7.5, "The Duel", 24)))
        self.assertEqual(v.headline, "Unknown: the signs disagree")

    def test_position_never_decides(self):
        # the last chapter was long, and the site split it into 50 and 50.1
        s = Series(english="F", status="FINISHED", chapters=50)
        rows = [*have(range(1, 50), 20), Chapter(50.0, "have", None, "WC", 22),
                Chapter(50.1, "failed", "Chapter 50.1", "Manganato")]
        for md in (None, ChapterList(None)):
            with self.subTest(md=md):
                v = classify(s, Blocker(50.1, {"Manganato": "Chapter 50.1"}), rows, md)
                self.assertEqual((v.kind, v.skippable), ("unknown", False))
                self.assertIn("The series is finished and its last chapter is 50; 50.1 comes after it, where "
                              "epilogues and extras go.", v.evidence)
        s = Series(english="S", status="FINISHED", chapters=20)
        rows = [*have(range(1, 21), None), Chapter(20.1, "failed")]
        self.assertEqual(classify(s, Blocker(20.1, {}), rows).kind, "unknown")         # length of 20 unknown
        self.assertEqual(classify(s, Blocker(20.5, {}), [*rows, Chapter(20.5, "failed")]).kind, "unknown")
        self.assertEqual(classify(s, Blocker(21, {}), [*rows, Chapter(21, "failed")]).kind, "unknown")  # a whole one
        rows = [*have(range(1, 21), 30), Chapter(20.1, "failed")]
        self.assertEqual(classify(s, Blocker(20.1, {}), rows).kind, "unknown")
        v = classify(s, Blocker(20.1, {"M": "Epilogue"}), rows)
        self.assertEqual((v.kind, v.confidence), ("side_story", LOW))
        self.assertEqual(v.evidence[0], 'M names it "Epilogue": an epilogue after the last chapter.')
        self.assertIn("An epilogue can be the story's own ending, so that is not certain.", v.evidence)
        releasing = Series(english="S", status="RELEASING")
        self.assertEqual(classify(releasing, Blocker(20.1, {}), rows).kind, "unknown")
        self.assertEqual(classify(releasing, Blocker(12.1, {"M": "Epilogue"}), rows).kind, "unknown")

    def test_a_later_whole_chapter_means_it_is_not_after_the_end(self):
        s = Series(english="S", status="FINISHED", chapters=20)
        rows = [*have(range(1, 22), 30), Chapter(20.1, "failed")]
        self.assertEqual(classify(s, Blocker(20.1, {}), rows).kind, "unknown")

    def test_names_from_the_chapter_row_when_the_blocker_has_none(self):
        v = classify(Series(english="S"), Blocker(7.5, {}), [*self.ROWS[:-1], Chapter(7.5, "failed", "Omake", "Bato")])
        self.assertEqual(v.evidence[0], 'Bato names it "Omake", which marks a side story or extra.')
        self.assertEqual(v.confidence, HIGH)

    def test_every_kind_is_a_known_one(self):
        for v in (classify(DANGERS, Blocker(7.2), DANGERS_ROWS, DANGERS_MD), classify(WRONG, Blocker(15.5), [])):
            self.assertIn(v.kind, verdict.KINDS)
            self.assertIn(v.confidence, (HIGH, LOW))

    def test_chapter_from_a_database_row(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        db.migrate(con)
        sid = db.upsert_series(con, TAMON)
        con.execute("INSERT INTO chapter (series_id, number, status, source_name, pages, name, updated_at)"
                    " VALUES (?, 1, 'have', 'Weeb Central', 53, 'SWITCH 1', ?), (?, 1.1, 'failed', 'Manganato', NULL,"
                    " 'Chapter 1.1', ?)", (sid, db.now(), sid, db.now()))
        rows = [Chapter.from_row(r) for r in db.chapters(con, sid)]
        con.close()
        self.assertEqual(rows, [Chapter(1.0, "have", "SWITCH 1", "Weeb Central", 53),
                                Chapter(1.1, "failed", "Chapter 1.1", "Manganato", None)])

    def test_manual_series_wording(self):
        v = classify(Series(english="Typed By Hand"), Blocker(7.5, {}), self.ROWS, None)
        self.assertIn("This series was added by hand, without an AniList or MangaDex entry, so MangaDex's chapter "
                      "list cannot be looked up.", v.evidence)
        self.assertNotIn("MangaDex's chapter list was not checked (not reachable, or not looked up yet).", v.evidence)


# -- confidence: what may be skipped automatically -----------------------------

class ConfidenceTest(unittest.TestCase):
    S = Series(english="S", status="RELEASING")
    ROWS = [*have(range(1, 30), 30), Chapter(7.5, "failed", None, "Manganato")]

    def test_the_verdict_s_properties(self):
        self.assertFalse(Verdict("side_story", "", []).auto_skip)             # LOW unless said otherwise
        self.assertTrue(Verdict("covered", "", [], HIGH).auto_skip)
        for kind in ("unknown", "rest_of_chapter"):
            self.assertFalse(Verdict(kind, "", [], HIGH).auto_skip)

    def test_a_side_story_is_certain_only_by_a_listing_s_own_words(self):
        seven = MdChapter(7.0, "Seven", 30)
        cases = [({"Manganato": "Side Story 1"}, None, HIGH, None),
                 ({"Manganato": "Chapter 7.5: Extra"}, None, HIGH, None),
                 ({"Manganato": "Side Story 1", "Bato": "Chapter 7.5"}, None, HIGH, None),
                 ({"Manganato": "Side Story 1"}, md_list(seven, MdChapter(7.5, None, 20), following=8.0), HIGH, None),
                 ({"Manganato": "Side Story 1"}, md_list(seven, MdChapter(7.5, "Omake 2", 20), following=8.0), HIGH,
                  None),
                 ({"Manganato": "Christmas Special"}, None, LOW,
                  '"Bonus" and "special" name ordinary chapters too, so that is not certain.'),
                 ({}, md_list(seven, MdChapter(7.5, "Omake", 20), following=8.0), LOW,
                  "Only MangaDex's name for it marks it so, not a site that lists it, so that is not certain."),
                 ({"Manganato": "Side Story 1", "Bato": "Hiatus Notice"}, None, LOW,
                  "Bato's notice-like name for it could be a chapter's own title."),
                 ({"Manganato": "Side Story 1"}, md_list(seven, MdChapter(7.5, "Announcement", 3), following=8.0), LOW,
                  "MangaDex's notice-like name for it could be a chapter's own title.")]
        for names, md, confidence, doubt in cases:
            with self.subTest(names=names, md=md):
                v = classify(self.S, Blocker(7.5, names), self.ROWS, md)
                self.assertEqual((v.kind, v.confidence), ("side_story", confidence))
                if doubt:
                    self.assertIn(doubt, v.evidence)

    def test_covered_is_certain_only_against_mangadex_s_copy_and_numbering(self):
        md = md_list(MdChapter(7.0, "Seven", 30), following=8.0)
        blocker = Blocker(7.2, {"Manganato": "Chapter 7.2"})
        rows = [*have(range(1, 30), 30), Chapter(7.2, "failed", "Chapter 7.2", "Manganato")]
        v = classify(self.S, blocker, rows, md)
        self.assertEqual((v.kind, v.confidence), ("covered", HIGH))
        self.assertIn("Weeb Central, where your chapter 8 is from, does not list 7.2.", v.evidence)
        without_8 = [r for r in rows if r.number != 8]
        for chapter_8, doubt in (
                (Chapter(8.0, "wanted", None, "Weeb Central"), None),       # where it will come from counts too
                (Chapter(8.0, "have", None, "Manganato", 30),
                 "Your chapter 8 is from Manganato, which lists 7.2 too: if 7.2 is a chapter of its own there, its "
                 "numbering after 7 is one ahead of MangaDex's."),
                (Chapter(8.0, "wanted", None, "Manganato"),
                 "Your chapter 8 will come from Manganato, which lists 7.2 too"),
                (Chapter(8.0, "have", None, None, 30),
                 "Where your chapter 8 is from is not known, so whether your numbering after 7 is MangaDex's cannot "
                 "be checked."),
                (Chapter(8.0, "unavailable", None, "Weeb Central"),
                 "You have no chapter 8 to check that your numbering after 7 is MangaDex's."),
                (None, "You have no chapter 8 to check that your numbering after 7 is MangaDex's.")):
            with self.subTest(chapter_8):
                v = classify(self.S, blocker, [*without_8, *([chapter_8] if chapter_8 else [])], md)
                self.assertEqual((v.kind, v.confidence), ("covered", LOW if doubt else HIGH))
                if doubt:
                    self.assertTrue(any(e.startswith(doubt) for e in v.evidence), v.evidence)
        v = classify(self.S, blocker, [*without_8, Chapter(8.0, "wanted", None, "Weeb Central")], md)
        self.assertIn("Weeb Central, where your chapter 8 will come from, does not list 7.2.", v.evidence)
        v = classify(self.S, Blocker(7.2, {"Manganato": "Chapter 7.2", "Weeb Central": "Chapter 7.2"}), rows, md)
        self.assertEqual((v.kind, v.confidence), ("covered", LOW))    # the source of 8 lists 7.2 too
        v = classify(self.S, Blocker(7.2, {"Manganato": "Announcement"}), rows, md)
        self.assertEqual((v.kind, v.confidence), ("covered", LOW))
        self.assertIn("Manganato's notice-like name for it could be a chapter's own title.", v.evidence)

    def test_a_name_like_chapter_n_s_is_no_title_of_its_own(self):
        md = md_list(MdChapter(7.0, "I Mixed It Around", 30), following=8.0)
        rows = [*[r for r in have(range(1, 30), 30) if r.number != 7], Chapter(7.0, "have", "Ch. 7: I Mixed It Around",
                                                                                 "Weeb Central", 30)]
        v = classify(self.S, Blocker(7.2, {"Manganato": "Chapter 7.2 - I mixed it around!"}), rows, md)
        self.assertEqual((v.kind, v.confidence), ("covered", HIGH))
        self.assertIn('Manganato names it "Chapter 7.2 - I mixed it around!", as your chapter 7 is named.', v.evidence)
        v = classify(self.S, Blocker(7.2, {"Manganato": "Chapter 7.2 - The Duel"}), rows, md)
        self.assertEqual((v.kind, v.headline), ("unknown", "Unknown: the signs disagree"))


# -- the adversarial check's false positives -----------------------------------
# Real story chapters that the verdict of branch `verdict` (e51072a) called a
# side story or covered: none of them may come out a HIGH-confidence one.

R = Series(anilist_id=424242, english="Some Series", status="RELEASING")


def fin(chapters=None):
    return Series(anilist_id=424243, english="Some Series", status="FINISHED", chapters=chapters)


def story(top=29, pages=30, source="Weeb Central", but=(), extra=()):
    """have 1..top at `pages` pages from `source`, except the numbers in `but`, plus `extra` rows."""
    return [*have([n for n in range(1, top + 1) if n not in but], pages, source), *extra]


def failed(n, name=None, source="Manganato"):
    return Chapter(float(n), "failed", name, source)


def md_at(*chapters, following=None, highest=None):
    return md_list(*(MdChapter(*c) for c in chapters), following=following, highest=highest)


FALSE_POSITIVES = [
    # A. notice words decided, even on whole chapters and against MangaDex's normal copy
    ("A1", R, Blocker(12, {"Manganato": "The Announcement"}), story(but=[12], extra=[failed(12)]), None, "unknown"),
    ("A2", R, Blocker(12, {"Manganato": "Hiatus"}), story(but=[12], extra=[failed(12)]), None, "unknown"),
    ("A3", R, Blocker(12, {"Manganato": "Recruitment"}), story(but=[12], extra=[failed(12)]), None, "unknown"),
    ("A4", R, Blocker(12, {"Manganato": "The Schedule"}), story(but=[12], extra=[failed(12)]), None, "unknown"),
    ("A5", R, Blocker(12, {"Manganato": "公告栏的秘密"}), story(but=[12], extra=[failed(12)]), None, "unknown"),
    ("A6", R, Blocker(12, {"Manganato": "Hiatus Notice"}), story(but=[12], extra=[failed(12)]),
     md_at((12.0, None, 30), following=13.0), "unknown"),
    ("A7", R, Blocker(12.5, {"Manganato": "Hiatus Notice"}), story(extra=[failed(12.5)]), None, "unknown"),
    # B. sources disagree, and one odd name decided
    ("B1", R, Blocker(12, {"Manganato": "Hiatus Notice", "Weeb Central": "Chapter 12: The Duel"}),
     story(but=[12], extra=[failed(12)]), None, "unknown"),
    ("B2", R, Blocker(12.5, {"Manganato": "Side Story", "Weeb Central": "Chapter 12.5: The Duel"}),
     story(extra=[failed(12.5)]), None, "unknown"),
    ("B3", R, Blocker(12.5, {"Manganato": "Extra", "Bato": "The Duel"}), story(extra=[failed(12.5)]), None, "unknown"),
    ("B4", R, Blocker(12.5, {"Manganato": "Chapter 12.5: Special", "Weeb Central": "Chapter 12.5: Reunion"}),
     story(extra=[failed(12.5)]), None, "unknown"),
    ("B5", R, Blocker(7.5, {"Manganato": "Twitter Extra"}), story(extra=[failed(7.5)]),
     md_at((7.0, "Seven", 30), (7.5, "The Reunion", 3), following=8.0), "unknown"),
    # C. a MangaDex copy under min_pages pages (a webtoon's stitched strips) taken for a notice despite its title
    ("C1", R, Blocker(7.5, {}), story(extra=[failed(7.5)]), md_at((7.0, "Seven", 5), (7.5, "The Duel", 3),
                                                                   following=8.0), "unknown"),
    ("C2", R, Blocker(7.5, {"Manganato": "Chapter 7.5"}), story(extra=[failed(7.5)]),
     md_at((7.5, "The Beach", 1), following=8.0), "unknown"),
    ("C3", R, Blocker(7.2, {"Manganato": "Chapter 7.2"}), story(extra=[failed(7.2)]),
     md_at((7.0, "Seven", 30), (7.1, "Confession", 4), following=8.0), "unknown"),
    ("C4", R, Blocker(12, {"Manganato": "Chapter 12"}), story(but=[12], extra=[failed(12)]),
     md_at((12.0, "Night Market", 5), following=13.0), "unknown"),
    # D. a finished series' last whole chapter called Epilogue or Special taken as "after the end"
    ("D1", fin(), Blocker(50, {"Manganato": "Epilogue"}), story(49, extra=[failed(50)]), None, "unknown"),
    ("D2", fin(45), Blocker(50, {"Manganato": "Special"}), story(49, extra=[failed(50)]), None, "unknown"),
    ("D3", fin(), Blocker(50, {"Manganato": "Special Chapter"}), story(49, extra=[failed(50)]), None, "unknown"),
    ("D4", fin(40), Blocker(50, {"Manganato": "Epilogue"}),
     story(49, extra=[failed(50), Chapter(50.5, "wanted", "Afterword", "Manganato")]), None, "unknown"),
    ("D5", fin(), Blocker(50, {"Manganato": "Epilogue: Spring"}), story(49, extra=[failed(50)]),
     md_at((50.0, "Epilogue: Spring", 30)), "unknown"),
    # E. "covered" had no upper length limit
    ("E1", R, Blocker(10.2, {"Manganato": "Chapter 10.2"}),
     story(pages=40, extra=[Chapter(10.1, "have", None, "Weeb Central", 40), failed(10.2)]),
     md_at((10.0, "Ten", 40), following=11.0), "unknown"),
    ("E2", R, Blocker(10.2, {"Manganato": "Chapter 10.2"}),
     story(pages=40, extra=[Chapter(10.1, "have", None, "Weeb Central", 40), failed(10.2)]),
     md_at((10.0, "Ten", None), following=11.0), "unknown"),
    ("E3", R, Blocker(10.1, {"Manganato": "Chapter 10.1"}),
     story(pages=40, but=[10], extra=[Chapter(10.0, "have", None, "Weeb Central", 60), failed(10.1)]),
     md_at((10.0, "Ten", 40), following=11.0), "unknown"),
    ("E4", R, Blocker(10.5, {"Manganato": "Chapter 10.5"}),
     story(pages=40, but=[10], extra=[Chapter(10.0, "have", None, "Weeb Central", 47), failed(10.5)]),
     md_at((10.0, "Ten", 40), following=11.0), "unknown"),
    # F. a double-length finale split three ways looked "usual" to the median
    ("F1", fin(50), Blocker(50.2, {"Manganato": "Chapter 50.2"}),
     story(49, extra=[Chapter(50.0, "have", None, "Weeb Central", 20), Chapter(50.1, "have", None, "Weeb Central", 20),
                      failed(50.2)]), md_at((50.0, "Fifty", None), highest=50.0), "unknown"),
    ("F2", fin(50), Blocker(50.2, {"Manganato": "Chapter 50.2"}),
     story(49, extra=[Chapter(50.0, "have", None, "Weeb Central", 30), Chapter(50.1, "have", None, "Weeb Central", 30),
                      failed(50.2)]), md_at((50.0, "Fifty", None), following=51.0), "unknown"),
    ("F3", fin(50), Blocker(50.2, {"Manganato": "Chapter 50.2"}),
     story(49, extra=[Chapter(50.0, "have", None, "Weeb Central", 25), Chapter(50.1, "have", None, "Weeb Central", 20),
                      failed(50.2)]), md_at((50.0, "Fifty", 30), highest=50.0), "unknown"),
    ("F4", fin(50), Blocker(50.1, {"Manganato": "Chapter 50.1"}),
     story(49, extra=[Chapter(50.0, "have", None, "Weeb Central", 30), failed(50.1)]),
     md_at((50.0, "Fifty", None), highest=50.0), "unknown"),
    # G. a MangaDex list that ends (or skips ahead) at the blocker counted as evidence
    ("G1", R, Blocker(7.2, {"Manganato": "Chapter 7.2"}), story(extra=[failed(7.2)]),
     md_at((7.0, "Seven", 30), highest=7.0), "unknown"),
    ("G2", R, Blocker(7.2, {"Manganato": "Chapter 7.2"}), story(extra=[failed(7.2)]),
     md_at((7.0, "Seven", 30), (7.5, "Extra", 3), highest=7.5), "unknown"),
    ("G3", R, Blocker(5.1, {"Manganato": "Chapter 5.1"}), story(extra=[failed(5.1)]),
     md_at((5.0, "Five", 30), following=50.0, highest=80.0), "unknown"),
    ("G4", fin(30), Blocker(30.1, {"Manganato": "Chapter 30.1"}), story(30, extra=[failed(30.1)]),
     md_at((30.0, "Thirty", 30), highest=30.0), "unknown"),
    # H. numbering offsets: the site's 7.1 is MangaDex's 8
    ("H1", R, Blocker(7.1, {"Manganato": "Chapter 7.1"}), story(source="Manganato", extra=[failed(7.1)]),
     md_at((7.0, "Seven", 30), following=8.0), "covered"),
    ("H2", R, Blocker(7.1, {"Manganato": "Chapter 7.1: The Duel"}), story(extra=[failed(7.1)]),
     md_at((7.0, "Seven", 30), following=8.0), "unknown"),
    ("H3", R, Blocker(7.1, {"Manganato": "Chapter 7.1"}), story(7, extra=[failed(7.1)]),
     md_at((7.0, "Seven", 30), following=8.0), "covered"),
    ("H4", R, Blocker(7.1, {"Manganato": "Chapter 7.1"}),
     story(7, extra=[failed(7.1), *[Chapter(float(n), "wanted", None, "Manganato") for n in range(8, 30)]]),
     md_at((7.0, "Seven", 30), following=8.0), "covered"),
    ("H5", R, Blocker(7.1, {"Manganato": "Chapter 7.1", "Weeb Central": "Chapter 7.1"}), story(extra=[failed(7.1)]),
     md_at((7.0, "Seven", 30), following=8.0), "covered"),
    # I. spin-off names on whole chapters
    ("I1", R, Blocker(12, {"Manganato": "Gaiden"}), story(but=[12], extra=[failed(12)]), None, "unknown"),
    ("I2", R, Blocker(12, {"Manganato": "Side Story: The Knight's Past"}), story(but=[12], extra=[failed(12)]),
     None, "unknown"),
    ("I3", fin(44), Blocker(45, {"Manganato": "Spin-off"}), story(44, extra=[failed(45)]), None, "unknown"),
    ("I4", fin(29), Blocker(30, {"Manganato": "Omake"}), story(extra=[failed(30)]), None, "unknown"),
]


class FalsePositivesTest(unittest.TestCase):
    def test_the_42_are_all_still_there(self):
        self.assertEqual(len(FALSE_POSITIVES), 42)
        self.assertEqual({c[0][0] for c in FALSE_POSITIVES}, set("ABCDEFGHI"))

    def test_none_is_a_certain_side_story_or_covered(self):
        for label, series, blocker, rows, md, kind in FALSE_POSITIVES:
            with self.subTest(label):
                v = classify(series, blocker, rows, md)
                self.assertFalse(v.auto_skip, (v.kind, v.confidence, v.evidence))
                self.assertEqual(v.kind, kind, v.evidence)
                self.assertEqual(v.confidence, LOW)

    def test_the_same_with_the_answers_judge_finds_in_the_cache(self):
        for label, series, blocker, rows, md, kind in FALSE_POSITIVES:
            with self.subTest(label), mock.patch.object(mangadex, "english_chapters", return_value=md) as look:
                v = verdict.judge(series, blocker, rows, fetch=False)
                look.assert_called_once_with(series, float(blocker.number), fetch=False)
                self.assertEqual((v.kind, v.auto_skip), (kind, False))


# -- numbers that are not numbers -------------------------------------------------

class NotANumberTest(unittest.TestCase):
    def test_the_blocker_s_number(self):
        for n in (float("nan"), float("inf"), float("-inf"), -1.5, 1e300, None, "7.2", True):
            with self.subTest(n):
                v = classify(R, Blocker(n, {"Manganato": "Side Story 1"}), story(), DANGERS_MD)
                self.assertEqual((v.kind, v.confidence), ("unknown", LOW))
                self.assertEqual(v.headline, "Unknown: its number is not a plain chapter number")
                with mock.patch.object(mangadex, "_request", side_effect=AssertionError("network")):
                    self.assertEqual(verdict.judge(R, Blocker(n), story()).kind, "unknown")

    def test_rows_mangadex_and_series_with_odd_numbers(self):
        nan, inf = float("nan"), float("inf")
        rows = [*story(but=[7, 8]), Chapter(nan, "have", None, "WC", 30), Chapter(inf, "have", None, "WC", 30),
                Chapter(7.0, "have", None, "WC", nan), Chapter(8.0, "have", None, "WC", inf),
                Chapter(9.5, "have", None, "WC", -3), Chapter(9.6, "have", None, "WC", True), failed(7.2)]
        md = ChapterList("u", 40, {nan: MdChapter(nan, "X", 30), 7.0: MdChapter(7.0, "Seven", inf),
                                   inf: MdChapter(inf, None, 1)}, following=nan, highest=inf)
        odd = Series(anilist_id=424244, english="Some Series", status="FINISHED", chapters=nan)
        for series in (R, odd):
            for m in (md, ChapterList("u", 40, {7.0: MdChapter(7.0, ["x"], nan)}, following=8.0)):
                with self.subTest(series=series.chapters, md=m):
                    v = classify(series, Blocker(7.2, {"Manganato": "Chapter 7.2"}), rows, m)
                    self.assertEqual(v.kind, "unknown")
                    self.assertTrue(all(isinstance(e, str) and "nan" not in e and "inf" not in e for e in v.evidence),
                                    v.evidence)
        v = classify(R, Blocker(7.2, {}), [*story(but=[7]), Chapter(7.0, "have", None, "WC", 30.0), failed(7.2)],
                     md_at((7.0, "Seven", 30.0), following=8.0))
        self.assertEqual((v.kind, v.confidence), ("covered", HIGH))         # whole floats are fine


# -- MangaDex's English chapter list ------------------------------------------

D_UUID = "3df1a9a3-a1be-47a3-9e90-9b3e55b1d0ac"
C7, C75, C6, C61, C8 = ("c71bec56-f6cf-4769-8e03-5bd659375050", "8638cc4b-e627-4bba-abc6-b3e10cb6079f",
                        "f1ca785c-6445-47b8-80f3-89230e8aa546", "764b3388-a70c-4f6a-9725-b89b02526a31",
                        "8c0dbf69-fb9f-4007-9a0d-c90fc7da7bb5")
SEARCH = {"result": "ok", "data": [
    {"id": "6274fcab-df09-4ac5-b516-14152bd031c7",      # the same title words, another series
     "attributes": {"title": {"ja-ro": "Boku no Kokoro no Yabai Yatsu: Love Comedy ga Hajimaranai"},
                    "links": {"al": "194079"}}},
    {"id": "0ca5bda2-0567-4529-963f-4136a098d4aa", "attributes": {"title": {"en": "x"}, "links": []}},
    {"id": "not-a-uuid", "attributes": {"links": {"al": "101557"}}},
    {"id": D_UUID, "attributes": {"title": {"ja-ro": "Boku no Kokoro no Yabai Yatsu"}, "links": {"al": "101557"}}}]}
AGGREGATE = {"result": "ok", "volumes": {
    "none": {"volume": "none", "count": 1, "chapters": {"199": {"chapter": "199", "id": "bbb7d8ea-df4c-4cab-9460-c55034d23862"}}},
    "1": {"volume": "1", "count": 5, "chapters": {
        "8": {"chapter": "8", "id": C8, "others": [], "count": 1},
        "7.5": {"chapter": "7.5", "id": C75, "others": [], "count": 1},
        "7": {"chapter": "7", "id": C7, "others": [], "count": 1},
        "6.1": {"chapter": "6.1", "id": C61, "others": [], "count": 1},
        "6": {"chapter": "6", "id": C6, "others": [], "count": 1},
        "none": {"chapter": "none", "id": "11111111-1111-1111-1111-111111111111"}}}}}
CHAPTERS = {"result": "ok", "response": "collection", "data": [
    {"id": C7, "type": "chapter", "attributes": {"chapter": "7", "title": "I Mixed It Around", "pages": 11,
                                                 "externalUrl": None, "translatedLanguage": "en"}},
    {"id": C75, "type": "chapter", "attributes": {"chapter": "7.5", "title": "Twitter Extra - Update Schedule 📅",
                                                  "pages": 2, "externalUrl": None, "translatedLanguage": "en"}}]}


class FakeMangaDex:
    """mangadex._request with canned answers; `calls` records (path, params,
    timeout, whether the pacing lock was held)."""

    def __init__(self, answers=None, fail=None):
        self.answers = answers or {"/manga": SEARCH, f"/manga/{D_UUID}/aggregate": AGGREGATE, "/chapter": CHAPTERS}
        self.fail, self.calls = fail, []

    def __call__(self, path, params, timeout):
        self.calls.append((path, params, timeout, mangadex._pace_lock.locked()))
        if self.fail:
            raise self.fail
        return self.answers.get(path, {})


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class EnglishChaptersTest(unittest.TestCase):
    def setUp(self):
        mangadex._cache.clear()
        self.clock, self.sleeps = Clock(), []
        patches = [mock.patch.object(mangadex.time, "monotonic", self.clock),
                   mock.patch.object(mangadex.time, "sleep", self.sleep),
                   mock.patch.object(mangadex, "_next_turn", 0.0),
                   mock.patch.object(mangadex.urllib.request, "urlopen", side_effect=AssertionError("network"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(mangadex._cache.clear)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock.now += seconds

    def lookup(self, fake, s=DANGERS, n=7.2, **kw):
        with mock.patch.object(mangadex, "_request", fake):
            return mangadex.english_chapters(s, n, **kw)

    def test_dangers_7_2(self):
        fake = FakeMangaDex()
        md = self.lookup(fake)
        self.assertEqual(md.manga_id, D_UUID)
        self.assertEqual((md.count, md.following, md.highest), (6, 8.0, 199.0))
        self.assertEqual(md.near, {7.0: MdChapter(7.0, "I Mixed It Around", 11),
                                   7.5: MdChapter(7.5, "Twitter Extra - Update Schedule 📅", 2)})
        paths = [c[0] for c in fake.calls]
        self.assertEqual(paths, ["/manga", f"/manga/{D_UUID}/aggregate", "/chapter"])
        self.assertIn(("translatedLanguage[]", "en"), fake.calls[1][1])
        self.assertEqual(sorted(v for k, v in fake.calls[2][1] if k == "ids[]"), sorted([C7, C75]))
        self.assertTrue(all(c[2:] == (mangadex.CHAPTERS_TIMEOUT, False) for c in fake.calls))  # lock not held
        with mock.patch.object(verdict.mangadex, "_request", fake):
            v = verdict.judge(DANGERS, Blocker(7.2, {"Manganato": "Chapter 7.2"}), DANGERS_ROWS)
        self.assertEqual((v.kind, v.confidence), ("covered", HIGH))
        self.assertEqual(len(fake.calls), 3)                    # the second look came from the cache

    def test_requests_are_paced(self):
        self.lookup(FakeMangaDex())
        self.assertEqual(self.sleeps, [mangadex.CHAPTERS_GAP, mangadex.CHAPTERS_GAP])

    def test_known_mangadex_id_skips_the_search(self):
        fake = FakeMangaDex()
        md = self.lookup(fake, Series(mangadex_id=D_UUID, english="The Dangers in My Heart"), 6.1)
        self.assertEqual([c[0] for c in fake.calls], [f"/manga/{D_UUID}/aggregate", "/chapter"])
        self.assertEqual((sorted(md.near), md.following), ([6.0, 6.1], 7.0))

    def test_no_title_match_without_the_anilist_link(self):
        fake = FakeMangaDex()
        md = self.lookup(fake, Series(anilist_id=5, romaji="Boku no Kokoro no Yabai Yatsu", english="Other"))
        self.assertEqual(md, ChapterList(None))
        self.assertEqual([c[1][0] for c in fake.calls], [("title", "Boku no Kokoro no Yabai Yatsu"), ("title", "Other")])
        self.assertIsNone(self.lookup(fake, Series(english="Manual"), 1.5))
        self.assertEqual(len(fake.calls), 2)

    def test_hosted_elsewhere_has_no_page_count(self):
        agg = {"volumes": {"none": {"chapters": {"1.5": {"chapter": "1.5", "id": C75}, "2": {"chapter": "2", "id": C8}}}}}
        chs = {"data": [{"id": C75, "attributes": {"chapter": "1.5", "title": "First Issue", "pages": 0,
                                                   "externalUrl": "https://global.manga-up.com/manga/125/15518"}}]}
        fake = FakeMangaDex({f"/manga/{D_UUID}/aggregate": agg, "/chapter": chs})
        md = self.lookup(fake, Series(mangadex_id=D_UUID, english="N"), 1.1)
        self.assertEqual((md.count, md.near, md.following), (2, {1.5: MdChapter(1.5, "First Issue", None)}, 2.0))

    def test_no_english_chapters(self):
        fake = FakeMangaDex({f"/manga/{D_UUID}/aggregate": {"result": "ok", "volumes": []}})
        md = self.lookup(fake, Series(mangadex_id=D_UUID, english="N"), 3)
        self.assertEqual(md, ChapterList(D_UUID, 0, {}))
        self.assertEqual(len(fake.calls), 1)

    def test_odd_answers_do_not_raise(self):
        for agg, chs in (({"volumes": "x"}, {}), ({"volumes": [None, {"chapters": ["x", {"chapter": "7", "id": 5}]}]}, {}),
                         (AGGREGATE, {"data": "x"}), (AGGREGATE, {"data": [None, {"id": C7, "attributes": None}]}),
                         (AGGREGATE, {"data": [{"id": C7, "attributes": {"title": ["x"], "pages": True}}]}),
                         ([1, 2], None)):
            with self.subTest(agg=agg, chs=chs):
                mangadex._cache.clear()
                fake = FakeMangaDex({f"/manga/{D_UUID}/aggregate": agg, "/chapter": chs})
                md = self.lookup(fake, Series(mangadex_id=D_UUID, english="N"))
                self.assertIsInstance(md, ChapterList)
                self.assertTrue(all(c.title is None and c.pages is None for c in md.near.values()))

    def test_failures_mean_no_evidence_and_wait_an_hour(self):
        fake = FakeMangaDex(fail=TimeoutError("timed out"))
        with self.assertLogs("mangarr.mangadex", "INFO") as logs:
            self.assertIsNone(self.lookup(fake))
        self.assertIn("MangaDex unreachable: timed out", logs.output[0])
        self.assertIsNone(self.lookup(fake))
        self.assertEqual(len(fake.calls), 2)                    # two tries, then the hour
        self.clock.now += mangadex.FAILED_TTL + 1
        fake.fail = None
        self.assertEqual(self.lookup(fake).manga_id, D_UUID)

    def test_kept_for_a_week(self):
        fake = FakeMangaDex()
        self.lookup(fake)
        self.clock.now += mangadex.CHAPTERS_TTL - 60
        self.lookup(fake)
        self.assertEqual(len(fake.calls), 3)
        self.clock.now += 120
        self.lookup(fake)
        self.assertEqual(len(fake.calls), 6)

    def test_fetch_false_never_waits_on_the_network(self):
        fake = FakeMangaDex()
        self.assertIsNone(self.lookup(fake, fetch=False))
        with mock.patch.object(verdict.mangadex, "_request", fake):
            v = verdict.judge(DANGERS, Blocker(7.2), DANGERS_ROWS, fetch=False)
        self.assertEqual(v.kind, "unknown")
        self.assertIn("MangaDex's chapter list was not checked (not reachable, or not looked up yet).", v.evidence)
        self.assertEqual(fake.calls, [])
        self.lookup(fake)
        self.assertEqual(self.lookup(fake, fetch=False).manga_id, D_UUID)

    def test_cache_is_bounded(self):
        c = mangadex._Cache(2)
        for k in "abc":
            c.put(k, k, 60)
        self.assertEqual([c.get(k) for k in "abc"], [(False, None), (True, "b"), (True, "c")])
        c.get("b")
        c.put("d", "d", 60)
        self.assertEqual([c.get(k)[0] for k in "bcd"], [True, False, True])

    def test_a_timeout_is_tried_once_more_without_a_long_wait(self):
        tries = []

        def urlopen(req, timeout):
            tries.append((timeout, mangadex._pace_lock.locked()))
            raise TimeoutError("timed out")

        with mock.patch.object(mangadex.urllib.request, "urlopen", urlopen), self.assertLogs("mangarr.mangadex"):
            self.assertIsNone(mangadex.english_chapters(Series(mangadex_id=D_UUID, english="N"), 3))
        self.assertEqual(tries, [(mangadex.CHAPTERS_TIMEOUT, False)] * 2)
        self.assertEqual(self.sleeps, [mangadex.CHAPTERS_GAP])  # only the gap before the second try

    def test_a_rate_limit_ends_the_lookup_and_holds_the_others_off(self):
        h = email.message.Message()
        h["Retry-After"] = "120"
        urlopen = mock.Mock(side_effect=urllib.error.HTTPError("https://x", 429, "Too Many Requests", h, None))
        with mock.patch.object(mangadex.urllib.request, "urlopen", urlopen), self.assertLogs("mangarr.mangadex"):
            self.assertIsNone(mangadex.english_chapters(Series(mangadex_id=D_UUID, english="N"), 3))
            self.assertEqual(urlopen.call_count, 1)
            self.assertEqual(self.sleeps, [])                   # not waited out, not with the lock held
            self.assertIsNone(mangadex.english_chapters(Series(mangadex_id=D_UUID, english="N"), 9))
            self.assertEqual(urlopen.call_count, 1)             # the others give up before asking again
            self.assertEqual(self.sleeps, [])
            self.clock.now += 121
            mangadex.english_chapters(Series(mangadex_id=D_UUID, english="N"), 12)
            self.assertEqual(urlopen.call_count, 2)

    def test_error_answers_are_closed(self):
        answers = []

        def urlopen(req, timeout):
            h = email.message.Message()
            h["Retry-After"] = "1"
            answers.append(io.BytesIO(b"{}"))
            raise urllib.error.HTTPError(req.full_url, codes.pop(0), "No", h, answers[-1])

        for sequence in ([500, 500], [404], [429]):
            with self.subTest(sequence):
                mangadex._cache.clear()
                self.clock.now += 3600
                codes, answers = list(sequence), []
                with mock.patch.object(mangadex.urllib.request, "urlopen", urlopen):
                    if sequence == [404]:
                        self.assertEqual(mangadex.english_chapters(Series(mangadex_id=D_UUID, english="N"), 3),
                                         ChapterList(D_UUID))
                    else:
                        with self.assertLogs("mangarr.mangadex", "INFO"):
                            self.assertIsNone(mangadex.english_chapters(Series(mangadex_id=D_UUID, english="N"), 3))
                self.assertEqual(len(answers), len(sequence))
                self.assertTrue(all(a.closed for a in answers))

    def test_a_long_queue_gives_up_instead_of_waiting(self):
        mangadex._next_turn = self.clock.now + mangadex.CHAPTERS_MAX_WAIT + 1
        fake = FakeMangaDex()
        with self.assertLogs("mangarr.mangadex", "INFO") as logs:
            self.assertIsNone(self.lookup(fake))
        self.assertIn("MangaDex busy", logs.output[0])
        self.assertEqual((fake.calls, self.sleeps), ([], []))

    def test_real_requests_carry_the_user_agent_and_a_short_timeout(self):
        seen = []

        def urlopen(req, timeout):
            seen.append((req.full_url, req.get_header("User-agent"), timeout))
            return io.BytesIO(json.dumps({"result": "ok", "volumes": []}).encode())

        with mock.patch.object(mangadex.urllib.request, "urlopen", urlopen):
            mangadex.english_chapters(Series(mangadex_id=D_UUID, english="N"), 3)
        self.assertEqual(seen, [(f"https://api.mangadex.org/manga/{D_UUID}/aggregate?translatedLanguage%5B%5D=en",
                                 mangadex.config.USER_AGENT, mangadex.CHAPTERS_TIMEOUT)])


if __name__ == "__main__":
    unittest.main()

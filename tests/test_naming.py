"""Naming formats (naming.py). With the default options, render() must give
exactly the names mang-arr 0.2.3 gave: the golden test holds it, through
library.chapter_filename() and unique_folder(), to the old code kept in
naming_reference.py, over a generated set of numbers, titles and taken
folder names. The rest covers the grammar, the options, validation and
the live preview."""
import os
import random
import re
import sys
import unicodedata
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import naming_reference as ref  # noqa: E402

from mangarr import library, naming  # noqa: E402
from mangarr.naming import ChapterInfo, FormatError, Options, SeriesNames, render  # noqa: E402

NFD = "NFD"

NUMBERS = [0, 0.0, 0.1, 0.25, 0.5, 0.99, 1, 1.5, 3.125, 5, 5.25, 7.75, 12, 12.001, 12.05, 12.25, 12.5, 12.95,
           12.99, 12.999, 16.5, 64.5, 72.1, 99, 99.5, 100, 100.25, 131, 999, 999.5, 999.99, 1000, 1000.5, 1234.25,
           2026, 10000, 123456.5, -1, -0.5, 1e6 + 0.5, 1e9, 1e15, 1e20, 1e100, 1e200]
_rng = random.Random(20260928)
NUMBERS += [round(_rng.uniform(0, 3000), _rng.choice((0, 1, 2, 3))) for _ in range(150)]

TITLES = [
    None, "", " ", "Chapter 12", "Ch.12", "Ch. 012:", "Episode 12:", "#12", "Chapter 12.5", "Chapter 12: The Storm",
    "Chapter 12 - The Storm", "Chapter 13", "12 - Foo", "12.5: Foo", "1.5 - Side", "The Storm", "S2 - Episode 5",
    "Extra: The Daily Life of Two People", 'Wind Breaker: The "Real" One?', 'a/b\\c*d?e"f<g>h|i', ':?/*"', "...",
    ".hack//Link", "Ends with dot.", "Vol.3 chapter 13", "Vol. 1 Ch. 4", "tab\tand\nnewline\x00nul\x1fus",
    "  Chapter 12:   The   Storm  ", "many     spaces", unicodedata.normalize(NFD, "Pokémon: Café?"),
    unicodedata.normalize(NFD, "한국어 제목"), "é" * 150, "x" * 300, "第" * 100, "😀" * 75,
    "a" * 79 + " b", "a" * 79 + ".b", "a" * 81, "第" * 85, "第" * 79, "Chapter " + "0" * 50 + "12 - x",
    "Title ~0123abcd", "Vol.1 chapter 12", unicodedata.normalize(NFD, "é" * 60), "Chapter 12 - " + "x" * 90,
]
_ALPHABET = "aZq09 :?/*\"<>|.\\-–_#\t" + "éü第😀" + "́"
RANDOM_TITLES = ["".join(_rng.choice(_ALPHABET) for _ in range(_rng.randrange(0, 120))) for _ in range(300)]

FOLDER_TITLES = [t for t in TITLES if t is not None] + [
    None, "Re:Zero", "Re_Zero", "Berserk", "?", "The " + "Very " * 60 + "Long Title", "進撃" * 60,
    unicodedata.normalize(NFD, "Pokémon"), "Wind Breaker", "Yakuza Fiancé: Raise wa Tanin ga Ii"]
SUFFIXES = ["anilist:1", "manual:Re_Zero", "mangadex:0b5d8e3a-1c2d-4e5f-8a9b-0c1d2e3f4a5b", ""]


def first_differences(pairs, limit=5):
    return [p for p in pairs if p[1] != p[2]][:limit]


class GoldenTest(unittest.TestCase):
    """The default options against the old code, case by case."""

    def test_chapter_file_names(self):
        cases = [(n, t) for n in NUMBERS for t in TITLES]
        cases += [(_rng.choice(NUMBERS), t) for t in RANDOM_TITLES]
        got = []
        for n, t in cases:
            want = ref.chapter_filename(n, t)
            got.append(((n, t), library.chapter_filename(n, t), want))
            got.append(((n, t, "render"), render(None, ChapterInfo(n, t), Options()), want))
            got.append(((n, t, "label"), library.chapter_label(n, t), ref.chapter_label(n, t)))
        self.assertGreater(len(cases), 8000)
        self.assertEqual(first_differences(got), [])

    def test_names_are_the_old_ones_for_the_documented_cases(self):
        self.assertEqual(library.chapter_filename(12.0), "Chapter 012.0.cbz")
        self.assertEqual(library.chapter_filename(64.5, "Chapter 64.5: Extra: The Daily Life of Two People"),
                         "Chapter 064.5 - Extra_ The Daily Life of Two People.cbz")
        self.assertEqual(library.chapter_filename(0.99), "Chapter 000.99.cbz")
        self.assertEqual(library.chapter_filename(2026), "Chapter 2026.0.cbz")
        self.assertEqual(library.chapter_filename(1000.5), "Chapter 1000.5.cbz")
        nfd = unicodedata.normalize(NFD, "Café")
        self.assertEqual(library.chapter_filename(1, nfd), f"Chapter 001.0 - {nfd}.cbz")   # kept as the source gave it

    def test_folder_names(self):
        got = []
        for title in FOLDER_TITLES:
            for suffix in SUFFIXES:
                first = ref.unique_folder(title, set(), suffix)
                second = ref.unique_folder(title, {first}, suffix)
                for taken in (set(), {first}, {first, second}, {first.upper()}, {first.casefold()},
                              {unicodedata.normalize(NFD, first)}, {"", None, "Other"}):
                    want = ref.unique_folder(title, taken, suffix)
                    got.append(((title, suffix, tuple(sorted(map(str, taken)))),
                                library.unique_folder(title, taken, suffix), want))
                    got.append(((title, suffix, "render"),
                                render(SeriesNames(title), None, Options(), taken=taken, suffix=suffix), want))
            got.append(((title, "plain"), render(SeriesNames(title)), ref.unique_folder(title, set(), "x")))
        self.assertGreater(len(got), 1000)
        self.assertEqual(first_differences(got), [])

    def test_the_one_difference_a_name_too_long_without_a_title(self):
        # 0.2.3 left an untitled name longer than NAME_MAX as it was (a chapter number of ~240 digits; none
        # is anywhere near): it is cut to fit now, like every other name. With a title it is as it was.
        old = ref.chapter_filename(1e300)
        self.assertGreater(len(old.encode()), naming.NAME_MAX)
        new = library.chapter_filename(1e300)
        self.assertLessEqual(len(new.encode()), naming.NAME_MAX)
        self.assertRegex(new, r"^Chapter 1000+\d*~[0-9a-f]{8}\.cbz$")
        self.assertEqual(library.chapter_filename(1e300, "The Storm"), ref.chapter_filename(1e300, "The Storm"))

    def test_a_suffix_only_when_the_name_is_taken(self):
        self.assertEqual(library.unique_folder("Berserk", set(), ""), ref.unique_folder("Berserk", set(), ""))
        self.assertEqual(library.unique_folder("Berserk", {"berserk"}, ""), "Berserk ()")      # as 0.2.3
        self.assertEqual(render(SeriesNames("Berserk"), None, taken={"Other"}), "Berserk")      # no suffix needed
        with self.assertRaises(ValueError):
            render(SeriesNames("Berserk"), None, taken={"BERSERK"})

    def test_no_free_folder_name_either_way(self):
        taken = {"Berserk", "Berserk (anilist_1)"} | {f"Berserk (anilist_1) {i}" for i in range(2, 1000)}
        with self.assertRaises(ValueError):
            ref.unique_folder("Berserk", taken, "anilist:1")
        with self.assertRaises(ValueError):
            library.unique_folder("Berserk", taken, "anilist:1")

    def test_safe_title_is_the_default_clean(self):
        for t in FOLDER_TITLES + RANDOM_TITLES:
            self.assertEqual(naming.clean(t), ref.safe_title(t), repr(t))
            self.assertEqual(library.safe_title(t), ref.safe_title(t), repr(t))

    def test_fit_name_unchanged(self):
        for t in TITLES[1:] + RANDOM_TITLES:
            for keep in ("", ".cbz"):
                self.assertEqual(library.fit_name(t * 3, keep=keep), ref.fit_name(t * 3, keep=keep))

    def test_every_old_name_reads_back_to_itself(self):
        """What Preview Rename does with the default formats: the title is
        read from the current name and rendered again. Every name the old
        code made (long names cut with a hash tag, titles cut at 80 ending in
        a space, NFD) comes out the same."""
        got = []
        for n in NUMBERS:
            for t in TITLES + RANDOM_TITLES[:60]:
                name = ref.chapter_filename(n, t)
                found, title = naming.title_from_name(name, None, n)
                self.assertTrue(found, name)
                got.append(((n, t), render(None, ChapterInfo(n, file_title=title or "")), name))
        self.assertEqual(first_differences(got), [])

    def test_a_name_of_another_shape_is_not_read(self):
        self.assertEqual(naming.title_from_name("Chapter 012.0 - x.cbz", None, 13.0), (False, None))
        self.assertEqual(naming.title_from_name("Official_Chapter 12.cbz", None, 12.0), (False, None))
        self.assertEqual(naming.title_from_name("Chapter 012.0 - x.zip", None, 12.0), (False, None))
        self.assertEqual(naming.title_from_name("Chapter 012.0.cbz", None, 12.0), (True, None))
        self.assertEqual(naming.title_from_name("Chapter 012.0 - The Storm.cbz", None, 12.0), (True, "The Storm"))


SERIES = SeriesNames("Yakuza Fiancé: Raise wa Tanin ga Ii", romaji="Raise wa Tanin ga Ii",
                     english="Yakuza Fiancé: Raise wa Tanin ga Ii", native="来世は他人がいい", year=2017)


def chapter(fmt: str, number: float, name: str | None = None, **kw) -> str:
    return render(SERIES, ChapterInfo(number, name), Options(chapter_file_format=fmt, **kw))


def folder(fmt: str, series: SeriesNames = SERIES, **kw) -> str:
    return render(series, None, Options(series_folder_format=fmt, **kw))


class GrammarTest(unittest.TestCase):
    def test_parse(self):
        parts = naming.parse("Chapter {Chapter:000.0}{ - Chapter Title}")
        self.assertEqual(parts, ("Chapter ", naming.Token("Chapter", width=3, decimals=1),
                                 naming.Token("Chapter Title", prefix=" - ")))
        self.assertEqual(naming.parse("{ (Year)}"), (naming.Token("Year", " (", ")"),))
        self.assertEqual(naming.parse("[{Series Title}]"), ("[", naming.Token("Series Title"), "]"))

    def test_token_names_ignore_case(self):
        self.assertEqual(naming.parse("{series title}{CHAPTER:00}"),
                         (naming.Token("Series Title"), naming.Token("Chapter", width=2)))
        self.assertEqual(naming.parse("{Series Title Romaji}")[0].name, "Series Romaji")    # the long spelling

    def test_chapter_number_formats(self):
        table = {"{Chapter:000.0}": ["005.0", "012.0", "012.5", "012.25", "100.0", "1000.0"],
                 "{Chapter:000}": ["005", "012", "012.5", "012.25", "100", "1000"],
                 "{Chapter:00}": ["05", "12", "12.5", "12.25", "100", "1000"],
                 "{Chapter}": ["5", "12", "12.5", "12.25", "100", "1000"],
                 "{Chapter:0.00}": ["5.00", "12.00", "12.50", "12.25", "100.00", "1000.00"]}
        for fmt, want in table.items():
            got = [chapter(f"Chapter {fmt}", n)[len("Chapter "):-4] for n in (5, 12, 12.5, 12.25, 100, 1000)]
            self.assertEqual(got, want, fmt)

    def test_optional_text_goes_with_an_empty_token(self):
        fmt = "Chapter {Chapter:000.0}{ - Chapter Title}"
        self.assertEqual(chapter(fmt, 12, "Chapter 12"), "Chapter 012.0.cbz")
        self.assertEqual(chapter(fmt, 12, "Chapter 12: The Storm"), "Chapter 012.0 - The Storm.cbz")
        self.assertEqual(folder("{Series Romaji}{ (Year)}"), "Raise wa Tanin ga Ii (2017)")
        self.assertEqual(folder("{Series Romaji}{ (Year)}", SeriesNames("Berserk")), "Berserk")
        self.assertEqual(folder("{Series Title} [{Year}]", SeriesNames("Berserk")), "Berserk []")    # literal stays

    def test_series_tokens(self):
        self.assertEqual(folder("{Series Title}"), "Yakuza Fiancé_ Raise wa Tanin ga Ii")
        self.assertEqual(folder("{Series English}"), "Yakuza Fiancé_ Raise wa Tanin ga Ii")
        self.assertEqual(folder("{Series Native}"), "来世は他人がいい")
        self.assertEqual(folder("{Series Native}", SeriesNames("Berserk")), "Berserk")         # falls back to the title
        self.assertEqual(folder("{Series Romaji}", SeriesNames("Berserk", english="Berserk")), "Berserk")
        self.assertEqual(chapter("{Series Romaji} - Chapter {Chapter:000.0}", 12), "Raise wa Tanin ga Ii - Chapter 012.0.cbz")
        self.assertEqual(chapter("{Series Title} {Year} - {Chapter}", 3), "Yakuza Fiancé_ Raise wa Tanin ga Ii 2017 - 3.cbz")

    def test_normalisation(self):
        nfd = SeriesNames(unicodedata.normalize(NFD, "Pokémon"))
        self.assertEqual(folder("{Series Title}", nfd), "Pokémon")                            # folders are NFC
        self.assertEqual(render(nfd, ChapterInfo(1), Options(chapter_file_format="{Series Title} {Chapter}")),
                         "Pokémon 1.cbz")
        title = unicodedata.normalize(NFD, "Café")
        self.assertEqual(chapter("Chapter {Chapter}{ - Chapter Title}", 1, title), f"Chapter 1 - {title}.cbz")

    def test_folder_uniqueness_with_any_format(self):
        opts = Options(series_folder_format="{Series Romaji}{ (Year)}")
        taken = {"raise wa tanin ga ii (2017)"}
        self.assertEqual(render(SERIES, None, opts, taken=taken, suffix="anilist:97994"),
                         "Raise wa Tanin ga Ii (2017) (anilist_97994)")
        with self.assertRaises(ValueError):
            render(SERIES, None, opts, taken=taken)                               # no suffix to add

    def test_folder_matches(self):
        taken = {"Wind Breaker"}
        second = library.unique_folder("Wind Breaker", taken, "anilist:2")
        self.assertEqual(second, "Wind Breaker (anilist_2)")
        wb = SeriesNames("Wind Breaker")
        self.assertTrue(naming.folder_matches(second, wb, "anilist:2"))     # kept, even once "Wind Breaker" is free
        self.assertTrue(naming.folder_matches("Wind Breaker", wb, "anilist:2"))
        self.assertTrue(naming.folder_matches("Wind Breaker (anilist_2) 7", wb, "anilist:2"))
        self.assertFalse(naming.folder_matches("Wind Breaker (anilist_3)", wb, "anilist:2"))
        self.assertFalse(naming.folder_matches("Wind Breaker", wb, "anilist:2",
                                               Options(series_folder_format="{Series Title} ({Year})")))

    def test_names_fit_whatever_the_format(self):
        long = SeriesNames("進撃" * 80, romaji="Very " * 80, year=2017)
        for fmt in ("{Series Title} - Chapter {Chapter:000.0}{ - Chapter Title}",
                    "{Series Romaji} {Chapter:000000.000}{ - Chapter Title} [{Year}]"):
            for n, t in ((12, None), (12.25, "第" * 200), (123456.5, "😀" * 100)):
                name = render(long, ChapterInfo(n, t), Options(chapter_file_format=fmt, chapter_title_max_chars=255))
                self.assertLessEqual(len(name.encode()), naming.NAME_MAX, name)
                self.assertTrue(name.endswith(".cbz"))
        self.assertLessEqual(len(folder("{Series Title} {Series Romaji} ({Year})", long).encode()), naming.NAME_MAX)

    def test_never_a_hidden_file(self):
        self.assertEqual(render(SeriesNames(".hack"), ChapterInfo(1),
                                Options(chapter_file_format="{Series Title} {Chapter}")), "hack 1.cbz")
        self.assertEqual(render(SeriesNames("x"), ChapterInfo(1, "..."),
                                Options(chapter_file_format="{Chapter Title}{ - Chapter}")), "untitled - 1.cbz")


class CharacterTest(unittest.TestCase):
    TITLE = "Yakuza Fiancé: Raise wa Tanin ga Ii"

    def test_colon_replacement(self):
        want = {"underscore": "Yakuza Fiancé_ Raise wa Tanin ga Ii", "delete": "Yakuza Fiancé Raise wa Tanin ga Ii",
                "dash": "Yakuza Fiancé- Raise wa Tanin ga Ii", "space_dash": "Yakuza Fiancé - Raise wa Tanin ga Ii",
                "smart": "Yakuza Fiancé - Raise wa Tanin ga Ii"}
        for mode, name in want.items():
            self.assertEqual(folder("{Series Title}", SeriesNames(self.TITLE), colon_replacement=mode), name)
        self.assertEqual(naming.clean("Re:Zero", Options(colon_replacement="smart")), "Re-Zero")
        self.assertEqual(naming.clean("Re:Zero", Options(colon_replacement="space_dash")), "Re -Zero")
        self.assertEqual(chapter("Chapter {Chapter}{ - Chapter Title}", 64.5, "Extra: The Daily Life",
                                 colon_replacement="smart"), "Chapter 64.5 - Extra - The Daily Life.cbz")

    def test_illegal_characters(self):
        self.assertEqual(naming.clean("... Deattemasu ka?"), "Deattemasu ka_")
        self.assertEqual(naming.clean('... Deattemasu ka?', Options(replace_illegal_characters=False)), "Deattemasu ka")
        off = Options(replace_illegal_characters=False, colon_replacement="delete")
        self.assertEqual(naming.clean('a*b?c"d<e>f|g', off), "abcdefg")
        # separators, NUL and control characters are always replaced
        self.assertEqual(naming.clean("a/b\\c\x00d\x1fe", off), "a_b_c_d_e")

    def test_a_title_from_a_file_name_cannot_hold_a_separator(self):
        # file_title is used as it is, but for the characters no name may hold, whatever it came from
        self.assertEqual(render(None, ChapterInfo(1, file_title="../../../etc/cron.d/x")),
                         "Chapter 001.0 - .._.._.._etc_cron.d_x.cbz")
        self.assertEqual(render(None, ChapterInfo(1, file_title="a\x00b\nc\\d\x1fe")), "Chapter 001.0 - a_b_c_d_e.cbz")
        off = Options(colon_replacement="delete", replace_illegal_characters=False)
        self.assertEqual(render(None, ChapterInfo(1, file_title='a:b?c/d'), off), "Chapter 001.0 - abc_d.cbz")
        self.assertEqual(render(None, ChapterInfo(1, file_title="../x"), Options(chapter_file_format="{Chapter Title} {Chapter}")),
                         "_x 1.cbz")                                   # and never a hidden name
        # a clean title - what a format put in a name - comes out as it went in, whatever the options
        modes = [Options(colon_replacement=m, replace_illegal_characters=i) for m in naming.COLON_MODES for i in (True, False)]
        for t in TITLES[1:] + RANDOM_TITLES:
            for made in modes:
                title = naming.clean(t, made)[:60]
                for now in modes:
                    self.assertEqual(naming.title_value(ChapterInfo(1, file_title=title), now), title, (t, made, now))

    def test_literal_text_is_cleaned_too(self):
        self.assertEqual(chapter("Chapter {Chapter:000}: part", 5), "Chapter 005_ part.cbz")
        self.assertEqual(chapter("Chapter {Chapter:000}: part", 5, colon_replacement="delete"), "Chapter 005 part.cbz")
        self.assertEqual(folder("{Series Title}?", SeriesNames("Why")), "Why_")


class TitleTest(unittest.TestCase):
    def test_title_length(self):
        fmt = "Chapter {Chapter:000.0}{ - Chapter Title}"
        self.assertEqual(chapter(fmt, 64.5, "Extra: The Daily Life", chapter_title_max_chars=10),
                         "Chapter 064.5 - Extra_ The.cbz")
        name = chapter(fmt, 1, "第" * 200, chapter_title_max_chars=255)
        self.assertLessEqual(len(name.encode()), 255)
        self.assertRegex(name, r"^Chapter 001\.0 - 第+~[0-9a-f]{8}\.cbz$")

    def test_long_titles_are_cut_not_the_number(self):
        # 80 characters of a title can be 320 bytes: the title is cut to what is left, so the chapter number
        # and the format's own text stay, and the name still reads back to itself
        s = SeriesNames("Berserk", year=2017)
        cases = {"{Chapter Title} - Chapter {Chapter:000.0}": " - Chapter 012.5.cbz",
                 "Chapter {Chapter:000.0}{ - Chapter Title} [{Year}]": " [2017].cbz",
                 "{Series Title} {Chapter Title} {Chapter:000.0} {Year}": " 012.5 2017.cbz",
                 "Chapter {Chapter:000.0}{ (Chapter Title)}": ").cbz",
                 "Chapter {Chapter:000.0}{ - Chapter Title}": ".cbz"}
        for fmt, end in cases.items():
            o = Options(chapter_file_format=fmt)
            for t in ("第" * 80, "😀" * 80, "é" * 20 + "😀" * 60):      # 240 to 320 bytes
                with self.subTest(fmt=fmt, title=t[:3]):
                    name = render(s, ChapterInfo(12.5, t), o)
                    self.assertLessEqual(len(name.encode()), naming.NAME_MAX)
                    self.assertRegex(name, r"~[0-9a-f]{8}" + re.escape(end) + "$")
                    self.assertIn("012.5", name)
                    found, title = naming.title_from_name(name, s, 12.5, o)
                    self.assertTrue(found, name)
                    self.assertEqual(render(s, ChapterInfo(12.5, file_title=title), o), name)
        self.assertEqual(render(s, ChapterInfo(12.5, "第" * 80), Options(chapter_file_format="{Chapter Title} - Chapter "
                                                                          "{Chapter:000.0}")),
                         "第" * 75 + "~59f8afb0 - Chapter 012.5.cbz")

    def test_long_series_titles_are_cut_not_the_number(self):
        long = SeriesNames("進撃" * 80, romaji="Very " * 80)
        name = render(long, ChapterInfo(12, "第" * 80),
                      Options(chapter_file_format="{Series Title} - Chapter {Chapter:000}{ - Chapter Title}"))
        self.assertLessEqual(len(name.encode()), naming.NAME_MAX)
        self.assertRegex(name, r"^進撃進撃.*- Chapter 012 -~[0-9a-f]{8}\.cbz$")          # no room for a title at all
        name = render(long, ChapterInfo(12), Options(chapter_file_format="{Series Romaji} {Series Title} {Chapter}"))
        self.assertRegex(name, r"^Very Very .*~[0-9a-f]{8} 12\.cbz$")          # the last series title is cut first

    def test_number_only_titles(self):
        for t in ("Vol.3 chapter 13", "Vol. 1 Ch. 4", "#13", "13", "Volume 2 Episode 5", "Vol.3", "Chapter 13:",
                  "vol.01 ch.004.5"):
            self.assertTrue(naming.number_only(t), t)
            self.assertEqual(chapter("Chapter {Chapter:000.0}{ - Chapter Title}", 12, t, drop_number_only_titles=True),
                             "Chapter 012.0.cbz", t)
            self.assertNotEqual(chapter("Chapter {Chapter:000.0}{ - Chapter Title}", 12, t), "Chapter 012.0.cbz", t)
        for t in ("S2 - Episode 5", "Vol.3 chapter 13 - The Storm", "Extra", "The 13th", "", "-", "1" * 70):
            self.assertFalse(naming.number_only(t), t)

    def test_a_title_from_a_file_name_is_kept_as_it_is(self):
        kept = "a" * 79 + " "                                           # the old cut at 80 left the space
        self.assertEqual(naming.title_value(ChapterInfo(1, file_title=kept)), kept)
        self.assertEqual(naming.title_value(ChapterInfo(1, file_title=kept), Options(chapter_title_max_chars=5)),
                         "aaaaa")
        tagged = "第" * 75 + "~0123abcd"                                  # cut by fit_name: 84 characters
        self.assertEqual(naming.title_value(ChapterInfo(1, file_title=tagged)), tagged)
        self.assertEqual(naming.title_value(ChapterInfo(1, file_title=tagged), Options(chapter_title_max_chars=10)),
                         "第" * 10)
        self.assertEqual(naming.title_value(ChapterInfo(1, file_title="Vol.3 chapter 13"),
                                            Options(drop_number_only_titles=True)), "")
        self.assertEqual(naming.title_value(ChapterInfo(1, "Chapter 1: Ignored", file_title="")), "")


class ValidateTest(unittest.TestCase):
    def errors(self, fmt, kind="chapter"):
        return " ".join(naming.validate(fmt, kind))

    def test_defaults_are_valid(self):
        self.assertEqual(naming.check_options(naming.DEFAULTS), [])
        self.assertEqual(naming.validate("{Series Romaji}{ (Year)}", "folder"), [])
        self.assertEqual(naming.validate("{Series Title} - Chapter {Chapter:000}{ - Chapter Title}", "chapter"), [])

    def test_refused(self):
        self.assertIn("must contain {Chapter}", self.errors("Chapter {Chapter Title}"))
        self.assertIn("must contain a series title", self.errors("{Year}", "folder"))
        self.assertIn("only be used in the chapter file format", self.errors("{Series Title} {Chapter}", "folder"))
        self.assertIn("cannot contain / or \\", self.errors("{Series Title}/Chapter {Chapter}"))
        self.assertIn("cannot contain / or \\", self.errors("Chapter\\{Chapter}"))
        self.assertIn("Unknown token {Volume}. Valid tokens: {Series Title}", self.errors("{Volume} {Chapter}"))
        self.assertIn("Unknown token {Series.Title}", self.errors("{Series.Title}", "folder"))
        self.assertIn("zeros with an optional dot", self.errors("Chapter {Chapter:0x0}"))
        self.assertIn("at most 6 zeros", self.errors("Chapter {Chapter:0000000}"))
        self.assertIn("only {Chapter} takes a number format", self.errors("{Year:0000} {Chapter}"))
        self.assertIn("has no closing }", self.errors("Chapter {Chapter"))
        self.assertIn("has no opening {", self.errors("Chapter Chapter}"))
        self.assertIn("the limit is 200", self.errors("Chapter {Chapter}" + "x" * 200))
        self.assertIn("cannot start with a space or a dot", self.errors(".Chapter {Chapter}"))
        self.assertEqual(naming.validate("", "folder"), ["The format is empty."])
        self.assertEqual(naming.validate(None, "chapter"), ["The format is empty."])
        with self.assertRaises(ValueError):
            naming.validate("{Series Title}", "file")

    def test_check_options(self):
        bad = Options(colon_replacement="colon", chapter_title_max_chars=0, replace_illegal_characters="yes",
                      drop_number_only_titles=1)
        errors = " ".join(naming.check_options(bad))
        for text in ("Colon Replacement must be one of: underscore", "Chapter Title Length",
                     "Replace Illegal Characters must be on or off", "Number-only Titles must be on or off"):
            self.assertIn(text, errors)
        for n in (0, 256, True, "80", 8.5):
            self.assertTrue(naming.check_options(Options(chapter_title_max_chars=n)), n)
        self.assertEqual(naming.check_options(Options(chapter_title_max_chars=255)), [])
        self.assertTrue(naming.check_options(Options(colon_replacement=["dash"])))
        self.assertIn("Series Folder Format: The series folder format must contain",
                      naming.check_options(Options(series_folder_format="{Year}"))[0])

    def test_render_refuses_an_invalid_format(self):
        with self.assertRaises(FormatError):
            render(SERIES, None, Options(series_folder_format="{Series Title} {Chapter}"))
        with self.assertRaises(FormatError):
            render(SERIES, ChapterInfo(1), Options(chapter_file_format="{Series Title}"))
        with self.assertRaises(FormatError) as e:
            render(SERIES, ChapterInfo(1), Options(chapter_file_format="{Nope} {Chapter}"))
        self.assertIn("Unknown token {Nope}", e.exception.messages[0])

    def test_as_options(self):
        self.assertIs(naming.as_options(None), naming.DEFAULTS)
        o = naming.as_options({"chapter_file_format": "Ch {Chapter}", "komga_url": "ignored"})
        self.assertEqual(o, Options(chapter_file_format="Ch {Chapter}"))


class ExamplesTest(unittest.TestCase):
    def test_defaults(self):
        ex = naming.examples()
        self.assertEqual(ex["folder"], "Yakuza Fiancé_ Raise wa Tanin ga Ii")
        self.assertEqual(ex["chapters"][:2], ["Chapter 012.0.cbz",
                                              "Chapter 064.5 - Extra_ The Daily Life of Two People.cbz"])
        self.assertEqual((ex["errors"], ex["warnings"]), ([], []))

    def test_sort_warnings(self):
        ex = naming.examples(Options(chapter_file_format="Chapter {Chapter:000}{ - Chapter Title}"))
        self.assertEqual(ex["warnings"], ["Readers and file browsers that sort by plain file name put Chapter 012.5 "
                                          "before Chapter 012."])
        self.assertEqual(naming.examples(Options(chapter_file_format="Chapter {Chapter:00}"), highest=150)["warnings"][1],
                         "Readers and file browsers that sort by plain file name put Chapter 100 before Chapter 99.")
        self.assertEqual(naming.sort_warnings(highest=1200),       # true of today's names too (two such chapters here)
                         ["Readers and file browsers that sort by plain file name put Chapter 1000.0 before "
                          "Chapter 999.0."])
        self.assertEqual(naming.sort_warnings(highest=999), [])
        # something after the number that sorts before '.' keeps the order
        self.assertEqual(naming.sort_warnings(Options(chapter_file_format="Chapter {Chapter:000} end")), [])

    def test_invalid_options_give_errors_only(self):
        ex = naming.examples({"chapter_file_format": "{Series Title}"})
        self.assertEqual((ex["folder"], ex["chapters"]), (None, []))
        self.assertIn("must contain {Chapter}", ex["errors"][0])

    def test_real_chapters(self):
        rows = [(1.0, "Chapter 1"), (1.5, "Chapter 1.5: Extra"), (2.0, "Chapter 2: " + "Long " * 10),
                (3.0, "Chapter 3: Short")]
        picked = naming.pick_examples(rows)
        self.assertEqual([c.number for c in picked], [1.0, 1.5, 2.0])
        ex = naming.examples(naming.DEFAULTS, SeriesNames("Berserk"), rows)
        self.assertEqual(ex["folder"], "Berserk")
        self.assertEqual(ex["chapters"], ["Chapter 001.0.cbz", "Chapter 001.5 - Extra.cbz",
                                          "Chapter 002.0 - " + ("Long " * 10).strip() + ".cbz"])
        self.assertEqual([c.number for c in naming.pick_examples([(1.0, None)])], [1.0])
        self.assertEqual(naming.pick_examples([]), [])

    def test_sort_inversions(self):
        names = [(12.5, "Chapter 012.5.cbz"), (12.0, "Chapter 012.cbz"), (100.0, "Chapter 100.cbz")]
        self.assertEqual(naming.sort_inversions(names), [((12.0, "Chapter 012.cbz"), (12.5, "Chapter 012.5.cbz"))])
        self.assertEqual(naming.sort_inversions([(1, "a")]), [])


if __name__ == "__main__":
    unittest.main()

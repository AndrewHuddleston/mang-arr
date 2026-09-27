"""What a source lists is scraped: a huge or garbage chapter number, a flood
of chapters, a pile of aliases or a hostile title must not blow up memory,
the plan or the logs."""
import math
import time
import tracemalloc
import unittest
from unittest import mock

from mangarr import core, resolver
from mangarr.model import Series
from mangarr.resolver import Plan, Rejected, SourceMatch, plausible_chapters, resolve
from mangarr.suwayomi import Chapter, Source

SRC = Source("1", "Src", "en")


def chapters(numbers):
    return [Chapter(i + 1, float(n), None, None, False) for i, n in enumerate(numbers)]


def plan_of(numbers):
    m = SourceMatch(SRC, 1, "T", None, 0, "T", 1, chapters(numbers))
    return Plan(Series(english="T"), [m], [], [], {float(n): m for n in numbers})


class GapsTest(unittest.TestCase):
    def test_spans(self):
        p = plan_of([1, 2, 5, 6.5, 9, 10])
        self.assertEqual(p.gaps(), [(3, 4), (7, 8)])
        self.assertEqual(p.gap_text(), "3-4, 7-8")
        self.assertEqual(plan_of([2, 4]).gaps(), [(1, 1), (3, 3)])
        self.assertEqual(plan_of([1, 2, 3]).gaps(), [])
        self.assertEqual(plan_of([]).gap_text(), "")

    def test_huge_top_number_costs_nothing(self):
        p = plan_of([1, 2, 3, 999_999_999, 3.4e38])
        tracemalloc.start()
        t0 = time.perf_counter()
        spans = p.gaps()
        text = p.gap_text()
        took = time.perf_counter() - t0
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        self.assertEqual(spans[0], (4, 999_999_998))
        self.assertLess(took, 0.1)
        self.assertLess(peak, 1_000_000)
        self.assertTrue(text.startswith("4-999999998"))

    def test_non_finite_numbers_are_ignored(self):
        p = plan_of([1, 3])
        p.assignment[math.inf] = p.assignment[1.0]
        p.assignment[math.nan] = p.assignment[1.0]
        self.assertEqual(p.gaps(), [(2, 2)])

    def test_span_count_is_capped(self):
        p = plan_of(range(1, 1000, 2))           # 499 one-chapter gaps
        self.assertEqual(len(p.gaps()), resolver.MAX_GAP_SPANS)
        self.assertTrue(p.gap_text().endswith(", ..."))

    def test_resolve_summary(self):
        s = core._resolve_summary(plan_of([1, 2, 5, 20_240_115]))
        self.assertIn("gaps nobody lists: 3-4, 6-20240114", s)


class PlausibleTest(unittest.TestCase):
    def test_garbage_and_dates_are_dropped(self):
        with self.assertLogs("mangarr.resolver", "WARNING") as cm:
            ok = plausible_chapters("Evil", chapters([1, 2, 3, 999_999_999, 20_240_115, math.inf, math.nan, -1]))
        self.assertEqual([c.number for c in ok], [1, 2, 3])
        self.assertIn("Evil", cm.output[0])

    def test_outlier_far_above_the_rest_is_dropped(self):
        with self.assertLogs("mangarr.resolver", "WARNING"):
            ok = plausible_chapters("S", chapters([1, 2, 3, 4, 5, 50_000]))
        self.assertEqual([c.number for c in ok], [1, 2, 3, 4, 5])

    def test_first_and_latest_only_is_kept(self):
        # some sources list chapter 0/1 and the latest chapter only: too few
        # numbers to call the top one an outlier
        for nums in ([1, 1500], [0, 1, 1500], [0, 1, 2, 3, 2500]):
            self.assertEqual([c.number for c in plausible_chapters("S", chapters(nums))], nums)

    def test_real_lists_are_kept(self):
        for nums in ([1, 2, 3, 150, 151, 152], [0, 1, 1.5, 2], list(range(1, 4001)), [1, 500], [5000]):
            self.assertEqual(len(plausible_chapters("S", chapters(nums))), len(nums), nums[:5])

    def test_long_names_are_cut(self):
        ch = chapters([1])
        ch[0].name = "x" * 100_000
        self.assertEqual(len(plausible_chapters("S", ch)[0].name), resolver.MAX_CHAPTER_NAME)


class FakeClient:
    def __init__(self, chapter_numbers, hit_title="Title"):
        self.numbers = chapter_numbers
        self.hit_title = hit_title
        self.searches = []

    def search(self, src, q):
        self.searches.append(q)
        return [{"id": 5, "title": self.hit_title, "author": None}]

    def manga(self, manga_id):
        return {"title": "Title", "author": None}, chapters(self.numbers)

    def page_count(self, chapter_id):
        return 20


class ResolveTest(unittest.TestCase):
    def setUp(self):
        resolver._unreachable.clear()
        p = mock.patch("mangarr.settings.get", lambda k: 3)
        p.start()
        self.addCleanup(p.stop)

    def test_ongoing_single_source_with_garbage_number(self):
        series = Series(anilist_id=1, english="Title", status="RELEASING")
        with self.assertLogs("mangarr.resolver", "WARNING"):
            plan = resolve(FakeClient([1, 2, 3, 999_999_999]), series, sources=[SRC])
        self.assertEqual(plan.chapters, [1.0, 2.0, 3.0])
        self.assertEqual(plan.gaps(), [])

    def test_source_listing_too_many_chapters_is_not_trusted(self):
        series = Series(anilist_id=1, english="Title", status="RELEASING")
        with mock.patch.object(resolver, "MAX_CHAPTERS_PER_SOURCE", 100), \
                self.assertLogs("mangarr.resolver", "WARNING"):
            plan = resolve(FakeClient([n / 10 for n in range(10, 2000)]), series, sources=[SRC])
        self.assertEqual(plan.assignment, {})
        self.assertIn("more than any real series", plan.matches[0].note)

    def test_search_titles_are_capped(self):
        aliases = [f"Alias {i} " + "y" * 250 for i in range(5000)]
        series = Series(anilist_id=1, english="Nothing Matches " + "z" * 1000, synonyms=aliases)
        client = FakeClient([1], hit_title="Other")
        plan = resolve(client, series, sources=[SRC])
        self.assertEqual(len(client.searches), resolver.MAX_SEARCH_TITLES)
        self.assertTrue(all(len(q) <= resolver.MAX_TITLE for q in client.searches))
        self.assertEqual(plan.matches, [])

    def test_native_title_survives_the_cap(self):
        # Latin titles come first in search_titles; with many of them the
        # native one (all some Korean/Chinese/Japanese sources index) must
        # still be searched
        series = Series(anilist_id=1, romaji="Na Honjaman Level Up", english="Solo Leveling", native="나 혼자만 레벨업",
                        synonyms=[f"Solo Leveling alias {i}" for i in range(10)])
        titles = resolver.capped_search_titles(series)
        self.assertEqual(len(titles), resolver.MAX_SEARCH_TITLES)
        self.assertEqual(titles[-1], "나 혼자만 레벨업")
        self.assertEqual(titles[:2], ["Na Honjaman Level Up", "Solo Leveling"])
        client = FakeClient([1], hit_title="Other")
        resolve(client, series, sources=[SRC])
        self.assertIn("나 혼자만 레벨업", client.searches)
        few = Series(anilist_id=2, english="A", native="あ")
        self.assertEqual(resolver.capped_search_titles(few), ["A", "あ"])


class ReviewSummaryTest(unittest.TestCase):
    def test_scraped_titles_are_one_line_and_capped(self):
        forged = "x\n2026-09-27 10:00:00 ERROR   mangarr.web.app: login: admin from 10.0.0.5"
        rejected = [Rejected(SRC, forged, "q", "title differs")] + \
                   [Rejected(SRC, f"{i}" + "z" * 1_000_000, "q", "title differs") for i in range(10)]
        plan = Plan(Series(english="T"), [], rejected, [], {})
        s = core._review_summary(plan)
        self.assertNotIn("\n", s)
        self.assertLessEqual(len(s), 900)
        self.assertIn("login: admin", s)          # still readable, just not a log line of its own


if __name__ == "__main__":
    unittest.main()

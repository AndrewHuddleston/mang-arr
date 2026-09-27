"""What a source lists is scraped: a huge or garbage chapter number, a flood
of chapters, a pile of aliases or a hostile title must not blow up memory,
the plan or the logs."""
import math
import os
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest import mock

from mangarr import core, db, limits, resolver
from mangarr.model import Series
from mangarr.resolver import Plan, Rejected, SourceMatch, plausible_chapters, primary, resolve
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
        for p in (mock.patch("mangarr.settings.get", lambda k: 3),
                  mock.patch.object(resolver, "SEARCHES", limits.Spacer(pause=lambda s, c=None: False))):
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


def ranked(name, sid, numbers, throttled=False, page_warm=False, reliability=0.5):
    m = SourceMatch(Source(sid, name, "en", throttled=throttled, page_warm=page_warm), int(sid), "T", None, 0, "T", 1,
                    chapters(numbers))
    m.reliability = reliability
    return m


class RankTest(unittest.TestCase):
    """Normal sources first, then rate-limited, then page-by-page ones: a
    page-by-page source is used only for chapters nobody else lists."""

    def test_order_by_tier_before_reliability(self):
        comick = ranked("Comick", "1", range(1, 21), page_warm=True, reliability=1.0)
        slow = ranked("Slow", "2", range(1, 11), throttled=True, reliability=0.9)
        weeb = ranked("Weeb", "3", range(1, 6), reliability=0.2)
        cands = resolver._assign([comick, slow, weeb])
        self.assertEqual([m.source.name for m in cands[3.0]], ["Weeb", "Slow", "Comick"])
        self.assertEqual([m.source.name for m in cands[8.0]], ["Slow", "Comick"])
        self.assertEqual([m.source.name for m in cands[15.0]], ["Comick"])     # only it lists 15: still taken
        plan = Plan(Series(english="T"), [comick, slow, weeb], [], [], {n: c[0] for n, c in cands.items()},
                    candidates=cands)
        self.assertEqual(primary(plan).source.name, "Weeb")                    # though Comick reaches furthest
        self.assertEqual(primary(Plan(Series(english="T"), [comick, slow], [], [], {})).source.name, "Slow")
        self.assertEqual(primary(Plan(Series(english="T"), [comick], [], [], {})).source.name, "Comick")

    def test_waiting_reason_says_page_by_page(self):
        comick = ranked("Comick", "1", [1], page_warm=True)
        plan = Plan(Series(english="T"), [comick], [], [], {1.0: comick}, candidates={1.0: [comick]})
        with tempfile.TemporaryDirectory() as tmp, db.connect(os.path.join(tmp, "t.db")) as con:
            sid = db.upsert_series(con, Series(anilist_id=1, english="T"))
            db.save_plan(con, sid, plan, 1)
            reason = db.chapters(con, sid)[0]["reason"]
        self.assertIn("available on Comick (images fetched page by page: slow)", reason)


class SearchPacingTest(unittest.TestCase):
    """Searches on one source start SEARCH_GAP_SECS apart (GENTLE_SEARCH_GAP_SECS
    on a page-by-page one), on a fake clock; other sources do not wait."""

    def setUp(self):
        resolver._unreachable.clear()
        self.t = [0.0]
        self.starts = []

        def pause(secs, should_cancel=None):
            self.t[0] += max(secs, 0.0)
            return bool(should_cancel and should_cancel())
        for p in (mock.patch("mangarr.settings.get", lambda k: 3),
                  mock.patch.object(resolver, "SEARCHES", limits.Spacer(clock=lambda: self.t[0], pause=pause))):
            p.start()
            self.addCleanup(p.stop)
        test = self

        class Client(FakeClient):
            def search(self, src, q):
                test.starts.append((src.name, test.t[0]))
                test.t[0] += 0.2                            # the search itself takes a while
                return super().search(src, q)
        self.client = Client([1], hit_title="Other")

    def gaps(self, name):
        times = [t for n, t in self.starts if n == name]
        return [round(b - a, 6) for a, b in zip(times, times[1:], strict=False)]

    def test_same_source_is_spaced(self):
        series = Series(anilist_id=1, english="A", romaji="B", synonyms=["C", "D"])
        resolve(self.client, series, sources=[Source("1", "Weeb", "en")])
        self.assertEqual(self.gaps("Weeb"), [1.0, 1.0, 1.0])
        self.starts.clear()
        resolve(self.client, series, sources=[Source("2", "Comick", "en", page_warm=True)])
        self.assertEqual(self.gaps("Comick"), [3.0, 3.0])       # and only 3 titles

    def test_the_en_and_all_variants_of_one_site_share_the_spacing(self):
        series = Series(anilist_id=1, english="A", romaji="B", synonyms=["C"])
        resolve(self.client, series, sources=[Source("111", "Comick (Unoriginal) (ALL)", "all", page_warm=True),
                                              Source("222", "Comick (Unoriginal) (EN)", "en", page_warm=True)])
        times = sorted(t for _, t in self.starts)
        self.assertEqual(len(times), 6)
        self.assertEqual([round(b - a, 6) for a, b in zip(times, times[1:], strict=False)], [3.0] * 5)

    def test_other_sources_do_not_wait(self):
        series = Series(anilist_id=1, english="A")
        resolve(self.client, series, sources=[Source(str(i), f"S{i}", "en") for i in range(1, 5)])
        self.assertEqual([round(t, 6) for _, t in self.starts], [0.0, 0.2, 0.4, 0.6])

    def test_threads_share_the_spacing(self):
        spacer, starts, lock = limits.Spacer(), [], threading.Lock()
        with mock.patch.object(resolver, "SEARCHES", spacer), mock.patch.object(resolver, "SEARCH_GAP_SECS", 0.05):
            class Stamp(FakeClient):
                def search(self, src, q):
                    with lock:
                        starts.append(time.monotonic())
                    return super().search(src, q)
            threads = [threading.Thread(target=resolve, args=(Stamp([1], hit_title="Other"), Series(english="A")),
                                        kwargs={"sources": [Source("9", "Weeb", "en")]}) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        starts.sort()
        self.assertEqual(len(starts), 4)
        self.assertGreaterEqual(min(b - a for a, b in zip(starts, starts[1:], strict=False)), 0.04)

    def test_cancel_while_waiting(self):
        with self.assertRaises(limits.Cancelled):          # cancelled once the first search went out
            resolve(self.client, Series(anilist_id=1, english="A", romaji="B"), sources=[Source("1", "Weeb", "en")],
                    should_cancel=lambda: bool(self.starts))
        self.assertEqual(len(self.starts), 1)              # the second title is never searched


class GentleTitlesTest(unittest.TestCase):
    def test_at_most_three_and_the_native_one_kept(self):
        series = Series(anilist_id=1, romaji="Na Honjaman Level Up", english="Solo Leveling", native="나 혼자만 레벨업",
                        synonyms=[f"Solo Leveling alias {i}" for i in range(10)])
        titles = resolver.gentle_titles(series, resolver.capped_search_titles(series))
        self.assertEqual(titles, ["Na Honjaman Level Up", "Solo Leveling", "나 혼자만 레벨업"])
        few = Series(anilist_id=2, english="A", native="あ")
        self.assertEqual(resolver.gentle_titles(few, ["A", "あ"]), ["A", "あ"])
        latin = Series(anilist_id=3, english="A", synonyms=["B", "C", "D"])
        self.assertEqual(resolver.gentle_titles(latin, latin.search_titles), latin.search_titles[:3])


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

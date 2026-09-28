"""Page counts of fractional chapters are kept between passes (pagecounts.py, migration 14). Every refresh used to
count the pages of every fractional chapter not downloaded yet, junk found long ago included: one fetchChapterPages
per chapter, per series, per pass. A count is now kept, and taken again only when the chapter may have changed (and a
junk count soon after it is new). No network, no real Suwayomi: a fake source site counts what it is asked."""
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from mangarr import config, core, db, limits, pagecounts, resolver, settings
from mangarr.model import Series
from mangarr.pagecounts import PageCounts
from mangarr.suwayomi import Chapter, Client, Source, SuwayomiError, SuwayomiUnreachable

NOTICES = (5.5, 10.5, 15.5, 20.5, 25.5, 30.5, 35.5, 40.5)     # 2 pages each: "next chapter on Friday"
EXTRAS = (12.5, 24.5, 36.5, 48.5)                              # real side chapters, 20 pages
DOWNLOADED = 50.5                                              # in Suwayomi already: never counted


def cid(manga_id: int, n: float) -> int:
    return manga_id * 10_000 + int(n * 10)


def ago(days: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))


class Site:
    """A Suwayomi with one series on three sources: Alpha and Bravo (an aggregator copy) list chapters 1-60 and
    the same fractional ones, Charlie 1-55. Every page count asked for is recorded in `asked`; `pages` sets a
    chapter's answer (a number, None: the source will not say, or an exception to raise)."""

    def __init__(self):
        self.src = [Source("1", "Alpha", "en"), Source("2", "Bravo", "en"), Source("3", "Charlie", "en")]
        fractions = [*NOTICES, *EXTRAS, DOWNLOADED]
        self.lists = {1: [*range(1, 61), *fractions], 2: [*range(1, 61), *fractions], 3: list(range(1, 56))}
        self.names: dict[int, str] = {}         # chapter id -> name other than "Chapter N"
        self.scanlators: dict[int, str] = {}
        self.dates: dict[int, str] = {}
        self.pages: dict[int, object] = {cid(m, n): 2 for m in (1, 2) for n in NOTICES}
        self.asked: list[int] = []

    def sources(self):
        return list(self.src)

    def search(self, src, q):
        return [{"id": int(src.id), "title": "Title", "author": None}]

    def manga(self, manga_id):
        out = []
        for n in self.lists[manga_id]:
            i = cid(manga_id, n)
            out.append(Chapter(i, float(n), self.names.get(i, f"Chapter {n:g}"), self.scanlators.get(i, "Group"),
                               n == DOWNLOADED, self.dates.get(i, "2024-01-01")))
        return {"title": "Title", "author": None}, out

    def page_count(self, chapter_id):
        self.asked.append(chapter_id)
        v = self.pages.get(chapter_id, 20)
        if isinstance(v, BaseException):
            raise v
        return v

    def set_in_library(self, *a, **k):
        pass


class Base(unittest.TestCase):
    """A temporary database, searches not spaced, no notifications."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "t.db")
        for p in (mock.patch.object(config, "DB_PATH", self.path),
                  mock.patch.object(config, "STAGING_ROOT", os.path.join(tmp.name, "staging")),
                  mock.patch.object(config, "LIBRARY_ROOT", os.path.join(tmp.name, "library")),
                  mock.patch("mangarr.notify.send", return_value=None),
                  mock.patch("mangarr.notify.send_detailed", return_value={}),
                  mock.patch.object(resolver, "SEARCHES", limits.Spacer(pause=lambda s, c=None: False))):
            p.start()
            self.addCleanup(p.stop)
        resolver._unreachable.clear()
        settings._cache.clear()
        self.addCleanup(settings._cache.clear)
        self.site = Site()
        self.sid = None

    def refresh(self, progress=None) -> list[int]:
        """One pass over the series (resolve and save, no download); returns the chapter ids counted in it."""
        before = len(self.site.asked)
        with db.connect() as con:
            out = core.add_series(con, self.site, Series(english="Title", status="RELEASING"), download=False,
                                  do_import=False, series_id=self.sid, progress=progress)
            self.sid = out.series_id
        return self.site.asked[before:]

    def statuses(self) -> dict[float, str]:
        with db.connect() as con:
            return {r["number"]: r["status"] for r in db.chapters(con, self.sid)}

    def rows(self) -> dict[int, sqlite3.Row]:
        with db.connect() as con:
            return {r["chapter_id"]: r for r in con.execute("SELECT * FROM page_probe")}

    def set_row(self, chapter_id: int, **cols) -> None:
        with db.connect() as con:
            sets = ", ".join(f"{k}=?" for k in cols)
            con.execute(f"UPDATE page_probe SET {sets} WHERE chapter_id=?", (*cols.values(), chapter_id))

    def age(self, hours: float) -> None:
        """Let `hours` go by for every kept count and retry."""
        shift = f"-{int(hours * 3600)} seconds"
        with db.connect() as con:
            con.execute("UPDATE page_probe SET counted_at=datetime(counted_at, ?), next_try=datetime(next_try, ?)",
                        (shift, shift))


class KeptCountTest(Base):
    def test_second_pass_counts_nothing_again(self):
        said: list[str] = []
        first = self.refresh(said.append)
        wanted_counts = sorted(cid(1, n) for n in (*NOTICES, *EXTRAS))
        self.assertEqual(sorted(first), wanted_counts)              # Alpha provides them; the downloaded one is skipped
        self.assertIn(f"counting the pages of fractional chapters (12 of {len(first)})", said)
        after_first = self.statuses()
        self.assertEqual({n for n, s in after_first.items() if s == "junk"}, set(NOTICES))
        self.assertEqual({after_first[n] for n in EXTRAS}, {"wanted"})

        said.clear()
        with self.assertLogs("mangarr.resolver", "INFO") as logs:
            second = self.refresh(said.append)
        self.assertEqual(second, [])                                # 12 probes the pass does not make
        self.assertFalse([m for m in said if "counting the pages" in m])
        self.assertTrue(any("probing 0 fractional chapter(s) for junk (< 8 pages); 12 more counted in an earlier "
                            "pass" in line for line in logs.output), logs.output)
        self.assertEqual(self.statuses(), after_first)              # the same decisions
        self.assertEqual(self.refresh(), [])

    def test_the_minimum_applies_to_kept_counts_at_once(self):
        self.refresh()
        with db.connect() as con:
            settings.set_many(con, {"min_pages": 25})
        self.assertEqual(self.refresh(), [])                        # no recount for a new minimum ...
        status = self.statuses()
        self.assertEqual({status[n] for n in (*NOTICES, *EXTRAS)}, {"junk"})   # ... and 20 pages are below it

    def test_a_chapter_listed_differently_is_counted_again(self):
        self.refresh()
        a, b, c = cid(1, 5.5), cid(1, 12.5), cid(1, 30.5)
        self.site.names[a] = "Chapter 5.5: Side Story"              # the notice became a chapter
        self.site.pages[a] = 24
        self.site.dates[b] = "2024-06-01"
        self.site.scanlators[c] = "Other Group"
        self.assertEqual(sorted(self.refresh()), sorted([a, b, c]))
        status = self.statuses()
        self.assertEqual((status[5.5], status[12.5], status[30.5]), ("wanted", "wanted", "junk"))
        self.assertEqual(self.rows()[a]["name"], "Chapter 5.5: Side Story")
        self.assertEqual(self.refresh(), [])

    def test_old_counts_are_counted_again(self):
        self.refresh()
        extra, notice, old_notice = cid(1, 12.5), cid(1, 5.5), cid(1, 10.5)
        settled = len(pagecounts.JUNK_RECOUNT_DAYS)                 # junk that came out the same that often ...
        self.set_row(extra, counted_at=ago(pagecounts.RECOUNT_DAYS + 1))
        self.set_row(notice, agreed=settled, counted_at=ago(pagecounts.RECOUNT_DAYS + 1))    # ... is trusted longer
        self.set_row(old_notice, agreed=settled, counted_at=ago(pagecounts.JUNK_RECOUNT_DAYS[-1] + 1))
        self.assertEqual(sorted(self.refresh()), sorted([extra, old_notice]))
        rows = self.rows()
        self.assertGreater(rows[extra]["counted_at"], ago(1))
        self.assertEqual(rows[old_notice]["agreed"], settled + 1)
        self.assertLess(rows[notice]["counted_at"], ago(pagecounts.RECOUNT_DAYS))
        self.set_row(extra, counted_at=ago(-30))                    # a clock that went back: not trusted
        self.assertEqual(self.refresh(), [extra])

    def test_failed_count_is_retried_on_a_backoff(self):
        extra = cid(1, 24.5)
        self.site.pages[extra] = None                               # the source will not list its pages
        self.assertIn(extra, self.refresh())
        self.assertEqual(self.statuses()[24.5], "wanted")          # kept, as when a count fails
        row = self.rows()[extra]
        self.assertEqual((row["pages"], row["tries"]), (None, 1))
        self.assertEqual(self.refresh(), [extra])                   # the next pass tries once more (a hiccup) ...
        self.assertEqual(self.rows()[extra]["tries"], 2)
        self.assertNotIn(extra, self.refresh())                     # ... then not every pass ...
        self.assertEqual(self.statuses()[24.5], "wanted")
        self.set_row(extra, next_try=ago(0.01))                     # ... but once its time has come
        self.assertEqual(self.refresh(), [extra])
        row = self.rows()[extra]
        self.assertEqual(row["tries"], 3)
        self.assertGreater(row["next_try"], ago(-(pagecounts.RETRY_HOURS[2] - 1) / 24))   # further out each time
        self.set_row(extra, next_try=ago(0.01))
        self.site.pages[extra] = 22
        self.assertEqual(self.refresh(), [extra])
        row = self.rows()[extra]
        self.assertEqual((row["pages"], row["tries"], row["next_try"]), (22, 0, None))
        self.assertEqual(self.refresh(), [])

    def test_a_failed_recount_keeps_the_chapter(self):
        self.refresh()
        notice = cid(1, 15.5)
        self.set_row(notice, agreed=4, counted_at=ago(pagecounts.JUNK_RECOUNT_DAYS[-1] + 1))
        self.site.pages[notice] = None                              # the source will not say this time
        self.assertEqual(self.refresh(), [notice])
        self.assertEqual(self.statuses()[15.5], "wanted")          # kept, as whenever a count fails: the old
        row = self.rows()[notice]                                   # count is not judged by ...
        self.assertEqual((row["pages"], row["agreed"], row["tries"]), (2, 4, 1))
        self.assertEqual(self.refresh(), [notice])
        self.assertEqual(self.refresh(), [])                        # ... nor while the retry is not due
        self.assertEqual(self.statuses()[15.5], "wanted")
        self.site.pages[notice] = 2
        self.set_row(notice, next_try=ago(0.01))
        self.assertEqual(self.refresh(), [notice])
        self.assertEqual(self.statuses()[15.5], "junk")
        row = self.rows()[notice]                                   # agrees with the count before the failures
        self.assertEqual((row["pages"], row["agreed"], row["tries"], row["next_try"]), (2, 5, 0, None))
        self.set_row(notice, counted_at=ago(pagecounts.JUNK_RECOUNT_DAYS[-1] + 1))
        self.site.pages[notice] = None
        self.site.names[notice] = "Chapter 15.5 (new)"             # listed differently: that count is void
        self.assertEqual(self.refresh(), [notice])
        self.assertEqual(self.statuses()[15.5], "wanted")
        row = self.rows()[notice]
        self.assertEqual((row["pages"], row["agreed"], row["tries"]), (None, 0, 1))

    def test_a_new_junk_count_is_checked_again_soon(self):
        side = cid(1, 12.5)
        self.site.pages[side] = 0                                   # a real side chapter answers empty once
        self.refresh()
        self.assertEqual(self.statuses()[12.5], "junk")
        del self.site.pages[side]
        self.assertEqual(self.refresh(), [])                        # not the next pass ...
        self.set_row(side, counted_at=ago(pagecounts.JUNK_RECOUNT_DAYS[0] + 0.01))
        self.assertEqual(self.refresh(), [side])                    # ... but a day later
        self.assertEqual(self.statuses()[12.5], "wanted")

    def test_junk_is_trusted_longer_each_time_it_comes_out_the_same(self):
        self.refresh()
        notice = cid(1, 5.5)
        for i, days in enumerate(pagecounts.JUNK_RECOUNT_DAYS, 1):  # a day, a week, a month, then 90 days
            self.assertEqual(self.rows()[notice]["agreed"], i)
            self.set_row(notice, counted_at=ago(days - 0.1))
            self.assertEqual(self.refresh(), [])
            self.set_row(notice, counted_at=ago(days + 0.1))
            self.assertEqual(self.refresh(), [notice])
            self.assertEqual(self.statuses()[5.5], "junk")
        self.set_row(notice, counted_at=ago(pagecounts.JUNK_RECOUNT_DAYS[-1] - 0.1))
        self.assertEqual(self.refresh(), [])                        # 90 days from then on
        self.site.pages[notice] = 3                                 # another count: new again
        self.set_row(notice, counted_at=ago(pagecounts.JUNK_RECOUNT_DAYS[-1] + 0.1))
        self.assertEqual(self.refresh(), [notice])
        self.assertEqual(self.rows()[notice]["agreed"], 1)
        self.site.pages[notice] = 24                                # a placeholder the site fixed in place
        self.set_row(notice, counted_at=ago(pagecounts.JUNK_RECOUNT_DAYS[0] + 0.1))
        self.assertEqual(self.refresh(), [notice])
        self.assertEqual(self.statuses()[5.5], "wanted")

    def test_ten_days_of_passes(self):
        """A pass every 6 hours for 10 days: 12 counts in the first pass, none in the second (all 12 saved), then
        only the 8 junk counts again, a day and a week later. Counting every pass, as before, is 12 a pass."""
        asked = []
        for _ in range(4 * 10):
            asked.append(len(self.refresh()))
            self.age(6)
        self.assertEqual(asked[:2], [12, 0])
        self.assertEqual(sorted(a for a in asked if a), [8, 8, 12])
        self.assertEqual((sum(asked), 12 * len(asked)), (28, 480))

    def test_suwayomi_not_answering_is_not_remembered(self):
        extra = cid(1, 36.5)
        self.site.pages[extra] = SuwayomiUnreachable("Suwayomi at http://fake unreachable: connection refused")
        self.assertIn(extra, self.refresh())
        self.assertEqual(self.statuses()[36.5], "wanted")
        self.assertNotIn(extra, self.rows())                        # says nothing about the chapter
        del self.site.pages[extra]
        self.assertEqual(self.refresh(), [extra])                   # so the next pass counts it
        self.assertEqual(self.rows()[extra]["pages"], 20)

    def test_chapters_and_entries_that_are_gone_are_forgotten(self):
        self.refresh()
        for m in (1, 2):
            self.site.lists[m].remove(40.5)                         # the notice was taken down
        self.assertEqual(self.refresh(), [])
        self.assertNotIn(cid(1, 40.5), self.rows())
        self.assertEqual(len(self.rows()), 11)
        self.site.src = self.site.src[1:]                           # Alpha is gone altogether: Bravo provides them
        self.assertEqual(len(self.refresh()), 11)
        self.assertEqual(sorted(i // 10_000 for i in self.rows()), [2] * 11)
        self.assertTrue(self.rows())
        with db.connect() as con:                                   # a deleted series: gone with the next save
            db.delete_series(con, self.sid)
            PageCounts(con).save(con, resolver.Plan(Series(english="Other"), [], [], [], {}))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM page_probe").fetchone()[0], 0)

    def test_stored_rows_it_cannot_trust_are_counted_again(self):
        self.refresh()
        a, b, c, d = (cid(1, n) for n in (5.5, 10.5, 12.5, 15.5))
        self.set_row(a, pages="two")
        self.set_row(b, pages=-3)
        self.set_row(c, counted_at=None)
        self.set_row(d, tries=1, next_try="9999-01-01 00:00:00")   # further out than any retry
        self.set_row(e := cid(1, 20.5), agreed=-1)
        self.assertEqual(sorted(self.refresh()), sorted([a, b, c, d, e]))
        self.assertEqual(self.refresh(), [])

    def test_without_kept_counts_every_chapter_is_counted(self):
        first = resolver.resolve(self.site, Series(english="Title"))
        n = len(self.site.asked)
        resolver.resolve(self.site, Series(english="Title"))          # the CLI's dry run, as before
        self.assertEqual(len(self.site.asked), 2 * n)
        self.assertEqual(set(first.junk), set(NOTICES))


class MigrationTest(Base):
    def test_a_0_2_2_database_upgrades_and_counts_once(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        db.migrate(con, target=13)                                  # the schema of release 0.2.2
        con.execute("INSERT INTO series (ref, title, added_at, folder) VALUES ('manual:Title', 'Title',"
                    " '2026-01-01 00:00:00', 'Title')")
        sid = con.execute("SELECT id FROM series").fetchone()[0]
        con.execute("INSERT INTO series_source (series_id, manga_id, source_name, title, match_level, author_level,"
                    " chapter_count, max_chapter, is_primary, seen_at) VALUES (?, 1, 'Alpha', 'Title', 0, 1, 73, 60,"
                    " 1, '2026-01-01 00:00:00')", (sid,))
        con.execute("INSERT INTO chapter (series_id, number, status, manga_id, source_name, pages, reason, updated_at)"
                    " VALUES (?, 5.5, 'junk', 1, 'Alpha', 2, 'old', '2026-01-01 00:00:00')", (sid,))
        con.commit()
        con.close()
        with db.connect() as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM page_probe").fetchone()[0], 0)
            self.assertEqual(db.chapters(con, sid)[0]["status"], "junk")
        self.sid = sid
        self.assertEqual(len(self.refresh()), 12)                   # counted once after the upgrade ...
        self.assertEqual(self.refresh(), [])                        # ... and kept from then on
        self.assertEqual({n for n, s in self.statuses().items() if s == "junk"}, set(NOTICES))

    def test_a_schema_14_without_the_table_still_resolves(self):
        con = sqlite3.connect(self.path)
        db.migrate(con, target=13)
        con.execute("CREATE TABLE other_branch (x INTEGER)")       # another branch's migration 14
        con.execute("PRAGMA user_version = 14")
        con.commit()
        con.close()
        with mock.patch.object(pagecounts, "_warned", False), \
                self.assertLogs("mangarr.pagecounts", "WARNING") as logs:
            self.assertEqual(len(self.refresh()), 12)
            self.assertEqual(len(self.refresh()), 12)               # nothing kept: counted every pass, as before
        self.assertEqual(len(logs.output), 1, logs.output)          # said once, not per series and pass
        self.assertIn("page_probe", logs.output[0])
        self.assertEqual({n for n, s in self.statuses().items() if s == "junk"}, set(NOTICES))

    def test_the_migration_runs_over_a_table_that_is_there(self):
        con = sqlite3.connect(self.path)
        db.migrate(con, target=13)
        con.executescript(db.MIGRATIONS[13])                        # made under another number before a renumber
        con.close()
        with db.connect() as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
        self.assertEqual(len(self.refresh()), 12)
        self.assertEqual(self.refresh(), [])


class ClientTest(unittest.TestCase):
    def test_page_count_tells_suwayomi_down_from_a_source_that_will_not_say(self):
        class Answers(Client):
            def __init__(self, e):
                super().__init__("http://suwayomi.invalid")
                self.e = e

            def gq(self, *a, **k):
                raise self.e
        self.assertIsNone(Answers(SuwayomiError("No pages found")).page_count(1))
        with self.assertRaises(SuwayomiUnreachable):
            Answers(SuwayomiUnreachable("connection refused")).page_count(1)


if __name__ == "__main__":
    unittest.main()

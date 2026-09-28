"""The page counts of fractional chapters, kept between passes.

A resolve counts the pages of every fractional chapter that is not
downloaded yet, to drop the notices and ads among them
(resolver._prune_junk). Each count asks the source for the chapter's page
list, so the counts are kept (the page_probe table), per source entry and
chapter as Suwayomi numbers them, and a chapter is counted again only when
it may have changed:

- its source lists it differently: another number, name, scanlator or
  upload date;
- the count is older than RECOUNT_DAYS. A count below the minimum is the
  one that costs a chapter when it is wrong (an empty answer, an upload
  still in progress, a placeholder the site fixes in place), so it is
  counted again sooner: after the days in JUNK_RECOUNT_DAYS, further out
  each time it comes out the same, up to every 90 days (junk is most of
  what is counted);
- the last count failed: it is tried again after RETRY_HOURS, further out
  after each failure, not every pass. Until then the chapter is kept, as
  when a count fails, whatever an older count said: a count that was due
  again and failed is not judged by.

Suwayomi itself not answering says nothing about a chapter: nothing is kept,
and the next resolve counts it. The junk decision itself (fewer pages than
min_pages) is made fresh each time from the count, so a change of the
setting needs no recount. A database without the table (a migration of
another branch took its number) keeps nothing: every chapter is counted
each pass, as before the table.
"""
import logging
import sqlite3
import time
from dataclasses import dataclass

from .suwayomi import Chapter

log = logging.getLogger(__name__)

RECOUNT_DAYS = 30
JUNK_RECOUNT_DAYS = (1, 7, 30, 90)   # a count below the minimum: a day, a week, a month, then every 90 days
RETRY_HOURS = (0, 24, 72, 168)      # after the 1st failed count: the next pass; then 1 day, 3 days, a week (cap)
COLUMNS = ("manga_id", "chapter_id", "number", "name", "scanlator", "uploaded", "pages", "counted_at", "agreed",
           "tries", "next_try")

_warned = False                     # the missing table was logged (once, until it is back)


def _stamp(offset_secs: float = 0.0) -> str:
    """A time as the database keeps them (db.now()), offset from now."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + offset_secs))


def listing(ch: Chapter) -> tuple:
    """How the source lists a chapter, as far as a recount depends on it."""
    return float(ch.number), ch.name or None, ch.scanlator or None, ch.uploaded or None


@dataclass
class Count:
    listing: tuple              # (number, name, scanlator, uploaded) when last counted or tried
    pages: int | None           # the last count for that listing; None: none yet
    counted_at: str | None
    agreed: int = 1             # counts in a row of that listing that gave these pages
    tries: int = 0              # failed counts since the last one that worked; while > 0 pages is not judged by
    next_try: str | None = None


def _from_row(r) -> Count | None:
    """A stored row, or None for one this version cannot trust (a restored
    backup is someone else's file): that chapter is simply counted again."""
    pages, agreed, tries = r["pages"], r["agreed"], r["tries"]
    stamps = (r["counted_at"], r["next_try"], r["name"], r["scanlator"], r["uploaded"])
    if not isinstance(r["number"], (int, float)) or not all(v is None or isinstance(v, str) for v in stamps):
        return None
    if pages is not None and (not isinstance(pages, int) or pages < 0):
        return None
    if not all(isinstance(v, int) and v >= 0 for v in (agreed, tries)) or (pages is not None and not r["counted_at"]):
        return None
    return Count((float(r["number"]), r["name"] or None, r["scanlator"] or None, r["uploaded"] or None),
                 pages, r["counted_at"], agreed, tries, r["next_try"])


class PageCounts:
    """The kept counts for one resolve. lookup() says whether a chapter's
    count can be used as it is; record() keeps a new count (or a failed
    one); save() writes them with the plan. Without a connection nothing is
    known beforehand (every chapter is counted) and save() is not called.
    Rows are read per source entry on first use, on the thread that
    resolves (the connection's own)."""

    def __init__(self, con=None):
        self.con = con
        self.known: dict[tuple[int, int], Count] = {}
        self.loaded: set[int] = set()
        self.changed: set[tuple[int, int]] = set()
        self.reused = self.counted = 0
        self.table: bool | None = None          # page_probe is there as this version knows it; None: not looked

    def _table(self, con) -> bool:
        """Whether the database has the page_probe table. Without it (its
        migration number taken by another one) nothing is kept, and every
        chapter is counted as before the table, instead of every resolve
        that reaches a fractional chapter failing."""
        global _warned
        if self.table is None:
            try:
                con.execute(f"SELECT {', '.join(COLUMNS)} FROM page_probe LIMIT 0")
                self.table = True
                _warned = False
            except sqlite3.OperationalError as e:
                if "no such" not in str(e):
                    raise
                self.table = False
                if not _warned:
                    log.warning("page counts of fractional chapters are not kept (%s): the database lacks the "
                                "page_probe table of schema 14, so every one is counted each pass", e)
                    _warned = True
        return self.table

    def _load(self, manga_id: int) -> None:
        if manga_id in self.loaded:
            return
        self.loaded.add(manga_id)
        if self.con is None or not self._table(self.con):
            return
        for r in self.con.execute("SELECT * FROM page_probe WHERE manga_id=?", (manga_id,)):
            c = _from_row(r)
            if c is not None and isinstance(r["chapter_id"], int):
                self.known.setdefault((manga_id, r["chapter_id"]), c)

    def lookup(self, manga_id: int, ch: Chapter, min_pages: int) -> tuple[bool, int | None]:
        """(True, the count to judge by) when the kept count holds, (False,
        None) when the chapter is to be counted now. The count is None for a
        chapter whose last count failed and is not due again: it is kept."""
        self._load(manga_id)
        c = self.known.get((manga_id, ch.id))
        if c is None or c.listing != listing(ch):
            return False, None                          # never counted, or listed differently now
        if c.tries:                                     # the last count failed
            # (a time further out than any retry is a clock that went back, or a restored file: due now)
            if c.next_try and _stamp() < c.next_try <= _stamp(RETRY_HOURS[-1] * 3600):
                self.reused += 1
                return True, None                       # not yet tried again: kept, as when a count fails
            return False, None
        if c.pages is None or not c.counted_at:
            return False, None
        if c.pages < min_pages:                         # checked again sooner while it is new
            days = JUNK_RECOUNT_DAYS[min(max(c.agreed, 1), len(JUNK_RECOUNT_DAYS)) - 1]
        else:
            days = RECOUNT_DAYS
        if not _stamp(-days * 86400) < c.counted_at <= _stamp(86400):
            return False, None
        self.reused += 1
        return True, c.pages

    def record(self, manga_id: int, ch: Chapter, pages: int | None) -> int | None:
        """Keep what counting the chapter gave: its page count, or None when
        the source would not say. Returns the count to judge by: this one,
        or None after a failure (the chapter is kept, whatever an older
        count said; that count stays in the row only so that a later count
        that agrees with it is trusted longer)."""
        self._load(manga_id)
        key, now_listing = (manga_id, ch.id), listing(ch)
        self.counted += 1
        self.changed.add(key)
        prev = self.known.get(key)
        same = prev is not None and prev.listing == now_listing
        if pages is not None:
            agreed = prev.agreed + 1 if same and prev.pages == pages else 1
            self.known[key] = Count(now_listing, pages, _stamp(), agreed)
            return pages
        tries = (prev.tries if same else 0) + 1
        hours = RETRY_HOURS[min(tries, len(RETRY_HOURS)) - 1]
        last = (prev.pages, prev.counted_at, prev.agreed) if same else (None, None, 0)
        self.known[key] = Count(now_listing, *last, tries, _stamp(hours * 3600))
        return None

    def save(self, con, plan) -> None:
        """Write the counts taken in this resolve, and forget the chapters
        its source entries no longer list and the entries that no series has
        any more. In the caller's transaction, after db.save_plan (which
        writes the series' entries)."""
        if not self._table(con):
            return
        rows = []
        for key in self.changed:
            c = self.known[key]
            rows.append((*key, *c.listing, c.pages, c.counted_at, c.agreed, c.tries, c.next_try))
        updates = ", ".join(f"{c}=excluded.{c}" for c in COLUMNS[2:])
        con.executemany(f"INSERT INTO page_probe ({', '.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})"
                        f" ON CONFLICT(manga_id, chapter_id) DO UPDATE SET {updates}", rows)
        gone: list[tuple[int, int]] = []
        for m in plan.matches:
            listed = {c.id for c in m.chapters}
            gone += [(m.manga_id, r[0]) for r in con.execute("SELECT chapter_id FROM page_probe WHERE manga_id=?",
                                                             (m.manga_id,)) if r[0] not in listed]
        con.executemany("DELETE FROM page_probe WHERE manga_id=? AND chapter_id=?", gone)
        orphans = con.execute("DELETE FROM page_probe WHERE manga_id NOT IN (SELECT manga_id FROM series_source)"
                              ).rowcount
        if self.counted or gone or orphans:
            log.debug("page counts: %d taken, %d reused, %d forgotten (no longer listed), %d of entries no series"
                      " has", self.counted, self.reused, len(gone), orphans)

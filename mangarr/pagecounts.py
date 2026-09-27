"""The page counts of fractional chapters, kept between passes.

A resolve counts the pages of every fractional chapter that is not
downloaded yet, to drop the notices and ads among them
(resolver._prune_junk). Each count asks the source for the chapter's page
list, so the counts are kept (the page_probe table), per source entry and
chapter as Suwayomi numbers them, and a chapter is counted again only when
it may have changed:

- its source lists it differently: another number, name, scanlator or
  upload date;
- the count is older than RECOUNT_DAYS, or JUNK_RECOUNT_DAYS for one below
  the minimum (a notice seldom turns into a chapter without its name or
  date changing, and junk is most of what is counted);
- the last count failed: it is tried again after RETRY_HOURS, further out
  after each failure, not every pass. Until then the last count of the same
  listing stands, or none, as when a count fails (the chapter is kept).

Suwayomi itself not answering says nothing about a chapter: nothing is kept,
and the next resolve counts it. The junk decision itself (fewer pages than
min_pages) is made fresh each time from the count, so a change of the
setting needs no recount.
"""
import logging
import time
from dataclasses import dataclass

from .suwayomi import Chapter

log = logging.getLogger(__name__)

RECOUNT_DAYS = 30
JUNK_RECOUNT_DAYS = 90
RETRY_HOURS = (12, 24, 72, 168)      # after the 1st failed count: half a day; then 1 day, 3 days, a week (cap)


def _stamp(offset_secs: float = 0.0) -> str:
    """A time as the database keeps them (db.now()), offset from now."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + offset_secs))


def listing(ch: Chapter) -> tuple:
    """How the source lists a chapter, as far as a recount depends on it."""
    return float(ch.number), ch.name or None, ch.scanlator or None, ch.uploaded or None


@dataclass
class Count:
    listing: tuple              # (number, name, scanlator, uploaded) when last counted or tried
    pages: int | None           # the count for that listing; None: none yet
    counted_at: str | None
    tries: int = 0              # failed counts since the last one that worked
    next_try: str | None = None


def _from_row(r) -> Count | None:
    """A stored row, or None for one this version cannot trust (a restored
    backup is someone else's file): that chapter is simply counted again."""
    pages, tries = r["pages"], r["tries"]
    stamps = (r["counted_at"], r["next_try"], r["name"], r["scanlator"], r["uploaded"])
    if not isinstance(r["number"], (int, float)) or not all(v is None or isinstance(v, str) for v in stamps):
        return None
    if pages is not None and (not isinstance(pages, int) or pages < 0):
        return None
    if not isinstance(tries, int) or tries < 0 or (pages is not None and not r["counted_at"]):
        return None
    return Count((float(r["number"]), r["name"] or None, r["scanlator"] or None, r["uploaded"] or None),
                 pages, r["counted_at"], tries, r["next_try"])


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

    def _load(self, manga_id: int) -> None:
        if manga_id in self.loaded:
            return
        self.loaded.add(manga_id)
        if self.con is None:
            return
        for r in self.con.execute("SELECT * FROM page_probe WHERE manga_id=?", (manga_id,)):
            c = _from_row(r)
            if c is not None and isinstance(r["chapter_id"], int):
                self.known.setdefault((manga_id, r["chapter_id"]), c)

    def lookup(self, manga_id: int, ch: Chapter, min_pages: int) -> tuple[bool, int | None]:
        """(True, the count to judge by) when the kept count holds, (False,
        None) when the chapter is to be counted now."""
        self._load(manga_id)
        c = self.known.get((manga_id, ch.id))
        if c is None or c.listing != listing(ch):
            return False, None                          # never counted, or listed differently now
        if c.tries:                                     # the last count failed
            # (a time further out than any retry is a clock that went back, or a restored file: due now)
            if c.next_try and _stamp() < c.next_try <= _stamp(RETRY_HOURS[-1] * 3600):
                self.reused += 1
                return True, c.pages                    # not yet tried again: the last count of this listing, if any
            return False, None
        days = JUNK_RECOUNT_DAYS if c.pages is not None and c.pages < min_pages else RECOUNT_DAYS
        if c.pages is None or not c.counted_at or not _stamp(-days * 86400) < c.counted_at <= _stamp(86400):
            return False, None
        self.reused += 1
        return True, c.pages

    def record(self, manga_id: int, ch: Chapter, pages: int | None) -> int | None:
        """Keep what counting the chapter gave: its page count, or None when
        the source would not say. Returns the count to judge by: this one,
        or after a failure the last count of the same listing (None when
        there is none)."""
        self._load(manga_id)
        key, now_listing = (manga_id, ch.id), listing(ch)
        self.counted += 1
        self.changed.add(key)
        if pages is not None:
            self.known[key] = Count(now_listing, pages, _stamp())
            return pages
        prev = self.known.get(key)
        same = prev is not None and prev.listing == now_listing
        tries = (prev.tries if same else 0) + 1
        hours = RETRY_HOURS[min(tries, len(RETRY_HOURS)) - 1]
        kept, at = (prev.pages, prev.counted_at) if same else (None, None)
        self.known[key] = Count(now_listing, kept, at, tries, _stamp(hours * 3600))
        return kept

    def save(self, con, plan) -> None:
        """Write the counts taken in this resolve, and forget the chapters
        its source entries no longer list and the entries that no series has
        any more. In the caller's transaction, after db.save_plan (which
        writes the series' entries)."""
        rows = []
        for key in self.changed:
            c = self.known[key]
            rows.append((*key, *c.listing, c.pages, c.counted_at, c.tries, c.next_try))
        con.executemany(
            "INSERT INTO page_probe (manga_id, chapter_id, number, name, scanlator, uploaded, pages, counted_at,"
            " tries, next_try) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(manga_id, chapter_id) DO UPDATE SET number=excluded.number, name=excluded.name,"
            " scanlator=excluded.scanlator, uploaded=excluded.uploaded, pages=excluded.pages,"
            " counted_at=excluded.counted_at, tries=excluded.tries, next_try=excluded.next_try", rows)
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

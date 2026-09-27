"""One series, tracked once.

A tracked series is known by one reference, anilist:123 or mangadex:uuid,
but most series are in both databases, so the same one could be tracked
twice. "It's Mine" was: as anilist:118601 from the Add page, and as a
MangaDex series when Library Import looked up its folder "It’s Mine", so
the same 159 chapters were tracked, downloaded and linked twice.

A MangaDex record names its AniList entry (attributes.links.al); the series
keeps that id as anilist_link, read again with every metadata refresh, and
every refresh pass reads it for the MangaDex series that have none yet
(read_links: those tracked before links were kept, and the ones a pass
skips). Two series with the same AniList id, their own or a linked one, are
the same series. Add (page and API), import lists and Library Import ask
tracked_as() before a series is added, and the health check names the
pairs tracked before this check existed, so the extra one can be deleted.
"""
import logging
import sqlite3

from . import db, mangadex
from .matching import oneline
from .model import Series

log = logging.getLogger(__name__)

MAX_PAIRS_SHOWN = 5      # pairs named in the health check; the rest are counted


class AlreadyTracked(ValueError):
    """The series is tracked already, under another reference."""


def anilist_of(s: Series) -> int | None:
    """The AniList id the series has, or its MangaDex record links to."""
    return s.anilist_id if s.anilist_id is not None else s.anilist_link


def identity(s: Series) -> str:
    """One key for a series under either reference: anilist:<id> when it has
    an AniList id or its MangaDex record links to one, else its reference."""
    aid = anilist_of(s)
    return f"anilist:{aid}" if aid is not None else s.ref


def add_key(s: Series) -> str:
    """The key of the job that adds `s` (jobs.Runner.submit): one queued add
    per series, whichever reference it came under (two import lists due
    together, or one list naming both references)."""
    return f"add {identity(s)}"


def tracked_as(con, s: Series) -> sqlite3.Row | None:
    """The tracked series that is `s`: the one with its reference, else one
    with the same AniList id (its own or a linked one), an AniList series
    before a MangaDex one. None when `s` is not tracked."""
    row = db.get_series_by_ref(con, s.ref)
    aid = anilist_of(s)
    if row is not None or aid is None:
        return row
    return con.execute("SELECT * FROM series WHERE anilist_id=? OR anilist_link=? ORDER BY anilist_id IS NULL, id"
                       " LIMIT 1", (aid, aid)).fetchone()


def refusal(con, s: Series) -> str | None:
    """Why `s` is not added: "already tracked", or "already tracked as
    <title>" when it is tracked under another reference. None: add it."""
    row = tracked_as(con, s)
    if row is None:
        return None
    return "already tracked" if row["ref"] == s.ref else f"already tracked as {row['title']}"


def check_new(con, s: Series) -> None:
    """Raise AlreadyTracked when `s` is tracked under another reference (two
    adds of one series under both references, queued before either ran)."""
    row = tracked_as(con, s)
    if row is not None and row["ref"] != s.ref:
        raise AlreadyTracked(f"{s.title}: already tracked as {row['title']}")


def tracked_ids(con, candidates) -> dict[str, int]:
    """{candidate ref: id of the tracked series it is} for the candidates
    tracked already, under their own reference or another (the Add page
    links those to the series instead of offering to add them)."""
    out = {}
    for c in candidates:
        row = tracked_as(con, c)
        if row is not None:
            out[c.ref] = row["id"]
    return out


def mark_tracked(con, items) -> None:
    """Library Import: flag the scanned folders (core.AdoptItem) whose series
    is tracked already. One tracked under another reference is proposed as
    that series, so its folder is never adopted as a second series. Folders
    this scan found under both references of one new series are proposed as
    one series, the AniList one (core.apply_adopt merges them all the same)."""
    first: dict[str, Series] = {}          # identity -> the series proposed for it
    for it in items:
        row = tracked_as(con, it.series) if it.series else None
        it.tracked = row is not None
        if row is not None and row["ref"] != it.series.ref:
            log.info("adopt: %s is %s, already tracked as %s (%s)", oneline(it.folder_name, 80), it.series.ref,
                     oneline(row["title"], 80), row["ref"])
            it.series = db.series_to_model(row)
        elif row is None and it.series:
            one = first.get(identity(it.series))
            if one is None or (one.anilist_id is None and it.series.anilist_id is not None):
                first[identity(it.series)] = it.series
    for it in items:
        one = first.get(identity(it.series)) if it.series and not it.tracked else None
        if one is not None and one.ref != it.series.ref:
            log.info("adopt: %s is %s, the same series as %s (%s) in this scan", oneline(it.folder_name, 80),
                     it.series.ref, oneline(one.title, 80), one.ref)
            it.series = one


def read_links(con) -> int:
    """Read the AniList link of every tracked MangaDex series that has none:
    those tracked before links were kept (migration 13 leaves them empty),
    and records that had none when last read. A refresh pass does this
    first, so series the pass skips (unmonitored, finished) are read too:
    one MangaDex request per 100 series. Returns how many links were found;
    an error is logged, never raised, and the next pass tries again."""
    try:
        rows = con.execute("SELECT id, title, mangadex_id FROM series WHERE ref LIKE 'mangadex:%'"
                           " AND mangadex_id IS NOT NULL AND anilist_link IS NULL").fetchall()
        if not rows:
            return 0
        links = mangadex.anilist_links([r["mangadex_id"] for r in rows])
        found = 0
        for r in rows:
            aid = links.get(r["mangadex_id"])
            if aid is not None:
                con.execute("UPDATE series SET anilist_link=? WHERE id=? AND anilist_link IS NULL", (aid, r["id"]))
                found += 1
                log.info("%s (mangadex:%s): MangaDex links it to anilist:%d", oneline(r["title"], 80),
                         r["mangadex_id"], aid)
        con.commit()
    except Exception as e:                 # never ends the pass that asked
        log.warning("could not read the AniList links of MangaDex series (the next refresh pass tries again):"
                    " %s: %s", type(e).__name__, e)
        return 0
    log.debug("AniList links read for %d MangaDex series: %d found", len(rows), found)
    return found


def pairs(con) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    """Tracked series that are one series (the same AniList id, their own or
    a linked one), as (first, second) pairs: the AniList series, else the
    older one, first. Linear in the tracked series."""
    rows = con.execute("SELECT id, ref, title, anilist_id, anilist_link FROM series"
                       " WHERE anilist_id IS NOT NULL OR anilist_link IS NOT NULL"
                       " ORDER BY anilist_id IS NULL, id").fetchall()
    first: dict[int, sqlite3.Row] = {}
    out = []
    for r in rows:
        aid = r["anilist_id"] if r["anilist_id"] is not None else r["anilist_link"]
        if aid in first:
            out.append((first[aid], r))
        else:
            first[aid] = r
    return out


def health_detail() -> str | None:
    """What the health check says about series tracked twice, or None when
    there are none."""
    try:
        with db.connect() as con:
            found = pairs(con)
    except sqlite3.Error as e:
        log.warning("health: could not look for series tracked twice: %s", e)
        return f"could not be checked: {e}"
    if not found:
        return None
    shown = "; ".join(f"{oneline(a['title'], 80)} ({a['ref']}) and {oneline(b['title'], 80)} ({b['ref']})"
                      for a, b in found[:MAX_PAIRS_SHOWN])
    more = f"; and {len(found) - MAX_PAIRS_SHOWN} more" if len(found) > MAX_PAIRS_SHOWN else ""
    return (f"{len(found)} series tracked twice, as the same AniList series (MangaDex links its record to it): "
            f"{shown}{more}. Both of a pair download and link the same chapters; delete the second one with its "
            f"library files (the first keeps its own).")

"""The operations, independent of how they are invoked (CLI, web, worker).

    add      identity -> resolve -> save -> download -> import
    refresh  refresh metadata, re-resolve a tracked series, pick up new chapters
    import   link what is on disk into the library
    adopt    register everything Suwayomi already downloaded
    delete   stop tracking, optionally remove the library folder
"""
import hashlib
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from . import (
    conversions,
    db,
    downloader,
    duplicates,
    inflight,
    komga,
    library,
    limits,
    metadata,
    metrics,
    naming,
    serieslock,
    settings,
    stuck,
)
from .matching import oneline
from .model import Series
from .pagecounts import PageCounts
from .resolver import Plan, primary, resolve
from .suwayomi import Client, SuwayomiError, with_cancel

log = logging.getLogger(__name__)


class Gone(RuntimeError):
    """The series was deleted while its job was running."""


@dataclass
class Outcome:
    series_id: int
    plan: Plan
    results: dict = field(default_factory=dict)   # chapter -> ok | failed
    imported: int = 0                             # chapters newly linked into the library
    before: frozenset | None = None               # what its download does not wait for (downloads_due)

    @property
    def downloaded(self) -> int:
        return sum(1 for r in self.results.values() if r == "ok")

    @property
    def failed(self) -> int:
        return len(self.results) - self.downloaded


# -- add / refresh ------------------------------------------------------------

def add_series(con, client: Client, series: Series, download: bool = True, do_import: bool = True,
               series_id: int | None = None, should_cancel: Callable[[], bool] | None = None,
               progress: Callable[[str], None] | None = None, quick: bool = False) -> Outcome:
    """Track a series: resolve it, remember the plan, fetch what is missing,
    link the results into the library. With series_id (a refresh) the row
    must still exist afterwards, or the work is abandoned. A cancel cuts the
    Suwayomi call in flight short: while sources are searched it raises
    limits.Cancelled with nothing saved, afterwards what was saved stays (a
    download returns what arrived). A new series tracked already under its
    other reference raises duplicates.AlreadyTracked before any search.

    quick (a refresh pass): when every source was searched for the series
    less than full_search_days ago, only the chapter lists of the entries
    stored for it are read again (known_entries), which finds new chapters
    without a title search on every site. A search of every source still
    happens when that is due, when an entry could not be read, and whenever
    quick is off (adding a series, Refresh on its page)."""
    if series_id is None:
        duplicates.check_new(con, series)
    counts = PageCounts(con)             # fractional chapters counted in an earlier pass
    settled = set() if series_id is None else {r[0] for r in con.execute(
        "SELECT number FROM chapter WHERE series_id=? AND status IN ('have','ignored')", (series_id,))}
    known = known_entries(con, series_id) if quick and series_id is not None else None
    plan = resolve(client, series, reliability=db.reliability(con), should_cancel=should_cancel, progress=progress,
                   counts=counts, settled=settled, known=known)
    lookups = with_cancel(client, should_cancel)
    if series_id is not None and not db.get_series(con, series_id):
        raise Gone(f"{series.title} was deleted during the refresh")
    series_id = db.upsert_series(con, series)
    p = primary(plan)
    blocked = stuck.blockers(con, series_id)        # as the last pass left it: save_plan makes these wanted again
    expired = db.save_plan(con, series_id, plan, p.manga_id if p else None)
    counts.save(con, plan)
    if known is None:
        con.execute("UPDATE series SET last_searched=? WHERE id=?", (db.now(), series_id))
    elif plan.unreachable or not p:                 # an entry could not be read, or none is usable: search again
        con.execute("UPDATE series SET last_searched=NULL WHERE id=?", (series_id,))
    hold_in_order(con, series_id, plan)             # save_plan's reasons for the chapters that still wait
    if expired:
        log.warning("%s: %s could not be searched for over %d days; its entry is dropped and the chapters only it "
                    "listed are given up once their grace is over", series.title, ", ".join(expired),
                    db.UNREACHABLE_KEEP_DAYS)
    # entries kept for a source that could not be searched this time (see save_plan)
    stale = [r["manga_id"] for r in db.sources(con, series_id)
             if r["manga_id"] not in {m.manga_id for m in plan.matches}]
    db.event(con, "resolved", _resolve_summary(plan), series_id)
    if not p:
        db.event(con, "review", _review_summary(plan), series_id)
        con.execute("UPDATE series SET last_error=? WHERE id=?", ("no usable source has this series", series_id))
        log.warning("%s: no usable source. %s", series.title, _review_summary(plan))
    con.commit()
    out = Outcome(series_id, plan)
    if p:
        _set_library_entries(lookups, plan, p.manga_id, stale)
        stuck.update(con, lookups, series_id, series, plan, blocked)    # may skip one: before the chapters due
    if do_import:
        out.imported += import_series(con, series_id, lookups, downloaded=suwayomi_downloaded(plan))
    if download and p:
        out.results = download_wanted(con, client, series_id, plan, should_cancel, progress)
        if do_import:
            finish_download(con, lookups, out)          # deleted while it downloaded: links nothing, raises Gone
        if out.results:
            stuck.update(con, lookups, series_id, series, plan)     # the chapter this download stopped at, if any
    return out


def known_entries(con, series_id: int) -> dict[str, dict] | None:
    """The source entries stored for a series, by source name, when a quick
    check of them is enough: every source was searched for it less than
    full_search_days ago and it has entries. None when the search of every
    source is due (never done, too long ago, the setting is 0)."""
    days = limits.setting("full_search_days")
    row = con.execute("SELECT last_searched FROM series WHERE id=?", (series_id,)).fetchone()
    if not days or row is None or not row["last_searched"] or row["last_searched"] < db.ago(days):
        return None
    rows = db.sources(con, series_id)
    return {r["source_name"]: dict(r) for r in rows} or None


def refresh_series(con, client: Client, series_id: int, download: bool = True,
                   should_cancel: Callable[[], bool] | None = None,
                   progress: Callable[[str], None] | None = None, quick: bool = False) -> Outcome:
    """Re-check a tracked series. Metadata is refreshed first (status,
    chapter count, new synonyms) and falls back to the stored row when the
    provider is unavailable. A cancel cuts a slow provider lookup short."""
    row = db.get_series(con, series_id)
    if not row:
        raise Gone(f"series #{series_id} does not exist")
    series = db.series_to_model(row)
    if not series.manual:
        try:
            fresh = limits.interruptible(lambda: metadata.by_ref(row["ref"]), should_cancel)
            if fresh:
                series = fresh
                log.debug("%s: metadata refreshed (%s, %s ch)", series.title, series.status, series.chapters)
        except (metadata.LookupError_, ValueError) as e:
            log.warning("%s: metadata refresh failed, using stored record: %s", row["title"], e)
    return add_series(con, client, series, download=download, series_id=series_id, should_cancel=should_cancel,
                      progress=progress, quick=quick)


def refresh_metadata(con, series_id: int) -> str:
    """Re-fetch the series record (titles, status, chapter count, description,
    genres, year ...) from its provider without touching the sources."""
    row = db.get_series(con, series_id)
    if not row:
        raise Gone(f"series #{series_id} does not exist")
    series = db.series_to_model(row)
    if series.manual:
        return "manual series: no metadata provider"
    fresh = metadata.by_ref(row["ref"])
    if not fresh:
        raise metadata.LookupError_(f"{row['ref']} not found any more")
    db.upsert_series(con, fresh)
    con.commit()
    log.info("%s: metadata refreshed (%s, %s ch, %d genre(s), %s)", fresh.title, fresh.status, fresh.chapters,
             len(fresh.genres), fresh.year)
    return f"{fresh.title}: {fresh.status or '?'}, {len(fresh.genres)} genre(s), year {fresh.year or '?'}"


def describe_outcome(con, series_id: int, o: Outcome) -> tuple[str, str]:
    """(state, text) for the Activity page: what happened to this series in a
    pass, in words a user can act on."""
    plan = o.plan
    if not primary(plan):
        return "nomatch", "no match: " + _review_summary(plan)
    rows = db.chapters(con, series_id)
    now_ = db.now()
    later = [r for r in rows if r["status"] == "failed" and r["next_try"] and r["next_try"] > now_]
    unavailable = sum(1 for r in rows if r["status"] == "unavailable")
    unstarted = sum(1 for r in rows if r["status"] == "wanted" and r["reason"] == downloader.UNSTARTED_REASON)
    # left for the next pass without being queued: the source's queue was busy with other downloads
    busy = [b for r in rows if r["status"] == "wanted" and (b := downloader.busy_source(r["reason"]))]
    parts = []
    state = "done"
    if o.results:
        parts.append(f"{o.downloaded} downloaded")
        if o.failed:
            state = "failed" if not o.downloaded else "done"
            failed_rows = [r for r in rows if r["status"] == "failed" and r["reason"]]
            why = failed_rows[0]["reason"] if failed_rows else "download failed"
            parts.append(f"{o.failed} failed ({why})")
    else:
        wanted_now = sum(1 for r in rows if r["status"] == "wanted")
        if not wanted_now and not later:
            parts.append("complete: nothing missing")
    if unstarted:
        # Suwayomi's queue did not get to them: nothing arrived, which the user must hear about
        parts.append(f"{unstarted} not started (Suwayomi's download queue was busy with other downloads)")
    if busy:
        parts.append(f"{len(busy)} not attempted ({', '.join(sorted(set(busy))[:3])} busy with other downloads; "
                     "tried again next pass)")
    if (unstarted or busy) and not o.downloaded:
        state = "failed"
    if later:
        nxt = min(r["next_try"] for r in later)[:16]
        parts.append(f"{len(later)} failed chapter(s) waiting for retry (next {nxt})")
    waits: dict[str, int] = {}
    for r in rows:
        if r["status"] == "wanted" and (token := downloader.waiting_for(r["reason"])):
            waits[token] = waits.get(token, 0) + 1
    for token, k in list(waits.items())[:3]:
        parts.append(f"{k} later chapter(s) wait for ch {token} (strict download in order)")
    if unavailable:
        parts.append(f"{unavailable} chapter(s) no source lists")
    if plan.unreachable:
        parts.append("unreachable: " + ", ".join(src.name for src, _ in plan.unreachable))
    return state, "; ".join(parts) or "nothing to do"


def _resolve_summary(plan: Plan) -> str:
    used = sorted({m.source.name for m in plan.assignment.values()})
    s = f"{len(plan.chapters)} chapters listed from {', '.join(used) or 'no source'}; {len(plan.wanted())} wanted"
    if plan.junk:
        s += f"; {len(plan.junk)} junk skipped"
    gaps = plan.gap_text()
    if gaps:
        s += f"; gaps nobody lists: {gaps}"
    if plan.unreachable:
        s += f"; unreachable: {', '.join(src.name for src, _ in plan.unreachable)}"
    return s


def _review_summary(plan: Plan) -> str:
    """Why nothing matched, for the log, the 'review' event and the Activity
    page. Source names, notes and rejected titles come from scraped sites, so
    each is flattened to one line and capped, and so is the whole message."""
    parts = []
    noted = [m for m in plan.matches if m.note]
    if noted:
        parts.append("not used: " + "; ".join(f"{oneline(m.source.name, 60)} ({oneline(m.note, 160)})"
                                              for m in noted[:4]))
    if plan.rejected:
        titles = []
        for r in plan.rejected:
            t = oneline(r.title, 120)
            if t not in titles:
                titles.append(t)
            if len(titles) >= 6:
                break
        parts.append("rejected titles: " + " | ".join(titles))
    if plan.unreachable:
        parts.append("unreachable: " + ", ".join(oneline(src.name, 60) for src, _ in plan.unreachable))
    return oneline("; ".join(parts), 900) or "no source returned anything"


def _set_library_entries(client: Client, plan: Plan, primary_manga_id: int, stale: list[int] | None = None) -> None:
    """Only the primary entry stays in Suwayomi's library, so its own update
    fetches new chapters from one source, not five copies. `stale` entries
    (kept for a source that could not be searched, e.g. a former primary)
    are taken out too: that is Suwayomi's own flag, the source site need
    not answer."""
    flags = [(m.manga_id, m.manga_id == primary_manga_id) for m in plan.matches]
    flags += [(mid, False) for mid in stale or () if mid != primary_manga_id]
    for manga_id, in_library in flags:
        try:
            client.set_in_library(manga_id, in_library, retries=1, timeout=30)
        except SuwayomiError as e:
            log.debug("could not set library flag on %d: %s", manga_id, e)


# statuses a background job may overwrite; anything else ('ignored', 'have')
# was set by the user or an import meanwhile and is left alone
_JOB_OWNED = ("wanted", "failed", "unavailable")
# statuses an import may turn 'failed' when the file it finds is bad: not
# 'ignored' (you skipped it: that stays as you left it) nor 'junk'
_IMPORT_OWNED = (*_JOB_OWNED, "have")


def hold_in_order(con, series_id: int, plan: Plan, rows: dict | None = None) -> float | None:
    """With strict download in order: the first chapter that holds the
    series (db.holds_in_order): one that failed and waits for its next
    attempt, as in the pass that failed it, or one no source listed in this
    resolve that is still waited for (db.past_grace: the source may only not
    have answered). Its later wanted chapters wait for it (their reason says
    so: downloader.waiting_reason, which stuck.blockers finds the series by),
    so no pass fills in the chapters after it and leaves a gap. Returns its
    number, or None (in order off, or none). Not committed."""
    if not settings.get("download_in_order"):
        return None
    rows = rows if rows is not None else {r["number"]: r for r in db.chapters(con, series_id)}
    now_, wanted = db.now(), plan.wanted()
    listed = set(plan.assignment) | set(plan.junk)
    later = [n for n, r in rows.items() if db.holds_in_order(r, n in listed, now_)]
    if not later:
        return None
    first = min(later)
    reason = downloader.waiting_reason(first) if rows[first]["status"] == "failed" else \
        downloader.waiting_unlisted_reason(first, db.unlisted_note(rows[first]))
    con.executemany("UPDATE chapter SET reason=?, updated_at=? WHERE series_id=? AND number=?",
                    [(reason, now_, series_id, n) for n in wanted
                     if n > first and (r := rows.get(n)) is not None and r["status"] == "wanted"
                     and r["reason"] != reason])
    return first


def _due(con, series_id: int, plan: Plan) -> list[float]:
    """The plan's wanted chapters that are due now: not on disk, not ignored,
    not a failed one waiting for its next attempt, and with strict download
    in order none after such a one, or after one no source listed this time
    that is still waited for (hold_in_order). Commits what it wrote."""
    rows = {r["number"]: r for r in db.chapters(con, series_id)}
    have_on_disk = {n for n, r in rows.items() if r["status"] == "have"}
    ignored = {n for n, r in rows.items() if r["status"] == "ignored"}
    later = {n for n, r in rows.items() if r["status"] == "failed" and r["next_try"] and r["next_try"] > db.now()}
    first = hold_in_order(con, series_id, plan, rows)
    wanted = [n for n in plan.wanted() if n not in have_on_disk and n not in later and n not in ignored]
    if first is not None:
        held = [n for n in wanted if n > first]
        wanted = [n for n in wanted if n < first]
        if con.in_transaction:
            con.commit()
        if held:
            log.info("%s: %d later chapter(s) wait for ch %g, which %s (strict download in order)", plan.series.title,
                     len(held), first, "failed on every source and is not due for another attempt yet"
                     if first in set(plan.assignment) else "no source listed this time; it is still waited for")
    if later:
        log.info("%s: %d failed chapter(s) not due for another attempt yet", plan.series.title, len(later))
    if ignored & set(plan.wanted()):
        log.info("%s: %d ignored chapter(s) not downloaded", plan.series.title, len(ignored & set(plan.wanted())))
    if not wanted:
        log.info("%s: nothing to download", plan.series.title)
    return wanted


def downloads_due(con, out: Outcome) -> list[float]:
    """After a refresh without download: the chapters to fetch for it now
    ([] when no source matched). Commits first, so no write is held across
    the downloads that follow. Sets out.before first (not_waited_for), for
    the take-backs of that download."""
    if not primary(out.plan):
        return []
    if con.in_transaction:
        con.commit()
    out.before = not_waited_for(con, out.series_id, out.plan)
    return _due(con, out.series_id, out.plan)


def dropped_chapters(con, series_id: int, wanted: set) -> set:
    """Chapters of `wanted` ignored (or linked) since the list was made:
    asked before each chunk."""
    return {r["number"] for r in con.execute(
        "SELECT number, status FROM chapter WHERE series_id=? AND status IN ('ignored','have')", (series_id,))
        if r["number"] in wanted}


def not_waited_for(con, series_id: int, plan: Plan) -> frozenset:
    """The chapters a download that starts now does not wait for, whatever
    becomes of them while it runs: the ones wanted or failed now (the
    chapters due are chosen with them as they are: _due goes past one only
    when strict order allows it) and the ones Suwayomi has downloaded
    (plan.have(): there is nothing to download, whether the import could
    link them or not). Taken before the chapters due are worked out, so a
    change made in between counts as made during the download
    (taken_back)."""
    return frozenset({r[0] for r in con.execute(
        "SELECT number FROM chapter WHERE series_id=? AND status IN ('wanted','failed')", (series_id,))}
        | plan.have())


def taken_back(con, series_id: int, wanted, before) -> float | None:
    """The first chapter below the last one of `wanted` (a download's
    chapters, due when it began) that is wanted or failed now without being
    one of them, and was neither when the download began nor downloaded by
    Suwayomi (`before`: not_waited_for): you un-skipped it or wanted it
    again while the download ran. With strict download in order the
    chapters of the download after it wait for it
    (downloader.SeriesSteps.wait_for). A chapter that was wanted all along
    without being due is not one: Suwayomi has it but the import could not
    link it (its reason says why: not_linked), and strict order went past
    it when the download began, as the next pass does. None when there is
    none. Asked before each chunk, like dropped_chapters."""
    if not wanted:
        return None
    wanted = set(wanted)
    for r in con.execute("SELECT number FROM chapter WHERE series_id=? AND number<? AND status IN ('wanted','failed')"
                         " ORDER BY number", (series_id, max(wanted))):
        if r["number"] not in wanted and r["number"] not in before:
            return r["number"]
    return None


def record_downloads(con, series_id: int, plan: Plan, wanted: list[float], results: dict, reasons: dict,
                     throttled: set, attempts: list | None = None) -> None:
    """Write what a download run did: sources that rate-limited us, source
    results, failed chapters and the ones not reached (never over a status
    the user set meanwhile), and the event. Committed. Source results come
    from `attempts` (downloader.SeriesSteps.attempts: the source that
    actually delivered or failed each chapter, a fallback or a free site
    taken instead of a busy one too); without it every result counts for
    the chapter's first choice."""
    for name in throttled:
        db.record_throttle(con, name)
        log.info("%s rate-limited us; it is paced automatically from now on", name)
    if attempts is None:
        attempts = [(m.source.name, n, r) for n, r in results.items() if (m := plan.assignment.get(n))]
    for name, _, r in attempts:
        metrics.record_download(name, r)
        db.record_source_result(con, name, "ok" if r == "ok" else "failed")
    tried = {n for _, n, _ in attempts}
    for n, r in results.items():
        if n not in tried:                        # failed before any source was asked (none lists it)
            metrics.record_download("?", r)
        if r != "ok" and not db.set_status(con, series_id, n, "failed", reasons.get(n, "download failed"),
                                           only_from=_JOB_OWNED):
            log.info("%s: ch %g failed, but its status was changed meanwhile; keeping that", plan.series.title, n)
    ok = sum(1 for r in results.values() if r == "ok")
    failed = sorted(n for n, r in results.items() if r != "ok")
    msg = f"{ok} chapter(s) downloaded, {len(failed)} failed"
    if failed:
        msg += ": " + "; ".join(f"ch {n:g}: {reasons.get(n, '?')}" for n in failed[:5])
        if len(failed) > 5:
            msg += f"; ... {len(failed) - 5} more"
    db.event(con, "downloaded", msg[:900], series_id)
    for n in wanted:
        if n not in results:                      # waiting for an earlier chapter, or the pass was cut short
            db.set_status(con, series_id, n, "wanted", reasons.get(n) or
                          "not attempted: the download pass was cancelled or interrupted before this chapter",
                          only_from=_JOB_OWNED)
    stuck.note_failures(con, series_id, results, attempts)     # the sites a chapter that holds its series failed on
    con.commit()


def finish_download(con, client: Client, out: Outcome) -> Outcome:
    """Link what a download run fetched into the library; Gone when the
    series was deleted while it downloaded (nothing is linked then)."""
    if not db.get_series(con, out.series_id):
        raise Gone(f"{out.plan.series.title} was deleted during the download")
    downloaded = {**{n: None for n, r in out.results.items() if r == "ok"}, **suwayomi_downloaded(out.plan)}
    out.imported += import_series(con, out.series_id, client, downloaded=downloaded)
    return out


def download_wanted(con, client: Client, series_id: int, plan: Plan,
                    should_cancel: Callable[[], bool] | None = None,
                    progress: Callable[[str], None] | None = None) -> dict:
    """Download the plan's wanted chapters. No write transaction is open while
    the downloader runs (it can take hours); the outcome is written after, and
    never over a status the user set in the meantime (e.g. 'ignored')."""
    before = not_waited_for(con, series_id, plan)
    wanted = _due(con, series_id, plan)
    if not wanted:
        return {}
    if con.in_transaction:                          # never hold a write across the downloads
        con.commit()
    wanted_set = set(wanted)
    reasons: dict = {}
    seen_throttle: set = set()
    attempts: list = []
    title = plan.series.title
    inflight.queue(series_id, title, wanted, None, "waiting for the download to start")
    try:
        with serieslock.hold(series_id, shared=True, should_cancel=should_cancel), \
                inflight.tracking(series_id, title):                                # let go before the import
            results = downloader.download(client, plan, only=wanted_set, should_cancel=should_cancel,
                                          reasons=reasons, progress=progress, throttled=seen_throttle,
                                          dropped=lambda: dropped_chapters(con, series_id, wanted_set),
                                          attempts=attempts,
                                          taken_back=lambda: taken_back(con, series_id, wanted_set, before))
    finally:
        inflight.clear(series_id)
    record_downloads(con, series_id, plan, wanted, results, reasons, seen_throttle, attempts)
    return results


def chapter_releases(con, client: Client, series_id: int, number: float) -> list[dict]:
    """Manual search: what every source entry of this series has for one
    chapter number, trusted or not, with why an entry is not used."""
    out = []
    for s in db.sources(con, series_id):
        try:
            chapters = client.chapters(s["manga_id"])
        except SuwayomiError as e:
            out.append({"source": s["source_name"], "mangaId": s["manga_id"], "title": s["title"],
                        "error": str(e), "note": s["note"]})
            continue
        ch = next((c for c in chapters if c.number == number), None)
        out.append({"source": s["source_name"], "mangaId": s["manga_id"], "title": s["title"], "note": s["note"],
                    "listed": ch is not None, "chapterId": ch.id if ch else None, "name": ch.name if ch else None,
                    "scanlator": ch.scanlator if ch else None, "uploaded": ch.uploaded if ch else None,
                    "downloaded": ch.downloaded if ch else False, "usable": not s["note"] and ch is not None})
    return out


def download_chapter(con, client: Client, series_id: int, number: float, manga_id: int | None = None,
                     should_cancel: Callable[[], bool] | None = None,
                     progress: Callable[[str], None] | None = None) -> str:
    """Fetch one chapter now: from the given source entry (manual search) or
    from the best trusted entry that lists it (automatic search). Returns a
    message; the chapter row is updated with the outcome. Every bookkeeping
    write is committed at once, so no write is held across a download."""
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"not a chapter number: {number!r}")
    row = db.get_series(con, series_id)
    if not row:
        raise Gone(f"series #{series_id} does not exist")
    title = row["title"]
    entries = [s for s in db.sources(con, series_id)
               if (manga_id is None and not s["note"]) or s["manga_id"] == manga_id]
    if not entries:
        raise ValueError("no such source entry for this series")
    if manga_id is None:
        entries = _search_order(con, series_id, number, entries)
    cancel = should_cancel or (lambda: False)
    ask = with_cancel(client, should_cancel)       # a cancel cuts a hung chapter list short
    tried = []
    for s in entries:
        if cancel():
            raise downloader.Cancelled()
        chapters = ask.chapters(s["manga_id"])
        ch = next((c for c in chapters if c.number == number), None)
        if ch is None:
            tried.append(f"{s['source_name']}: does not list it")
            continue
        if s["note"] and manga_id is None:
            continue
        if manga_id is None and (short := _counted_short(con, s["manga_id"], ch)) is not None:
            # the automatic choice never takes a copy a pass left out as a notice image; picking the
            # entry yourself (Manual) still overrides
            tried.append(f"{s['source_name']}: its copy has {short} page(s): a notice image, not the chapter")
            continue
        inflight.queue(series_id, title, [number], s["source_name"], "waiting for the download to start")
        try:
            with serieslock.hold(series_id, shared=True, should_cancel=cancel), \
                    inflight.tracking(series_id, title):                            # let go before the import
                ok, failed, why = downloader.download_one(client, s["manga_id"], ch, title, s["source_name"],
                                                          should_cancel=cancel, progress=progress)
        finally:
            inflight.finish(series_id, [number])
        metrics.record_download(s["source_name"], "ok" if ok else "failed")
        db.record_source_result(con, s["source_name"], "ok" if ok else "failed")
        con.commit()                                # before the next source's network calls
        if ok:
            db.set_status(con, series_id, number, "wanted", None)     # import_series flips it to have
            con.execute("UPDATE chapter SET manga_id=?, source_name=?, name=COALESCE(?, name),"
                        " uploaded=COALESCE(?, uploaded) WHERE series_id=? AND number=?",
                        (s["manga_id"], s["source_name"], ch.name, ch.uploaded, series_id, number))
            con.commit()
            linked = import_series(con, series_id, ask)
            msg = f"chapter {number:g} downloaded from {s['source_name']}" + (" and linked" if linked else "")
            db.event(con, "downloaded", msg, series_id)
            con.commit()
            log.info("%s: %s", title, msg)
            return msg
        tried.append(f"{s['source_name']}: {why.get(number, 'failed')}")
    reason = "; ".join(tried) or "no source lists this chapter"
    if not db.set_status(con, series_id, number, "failed", reason, only_from=_JOB_OWNED):
        log.info("%s: chapter %g failed, but its status was changed meanwhile; keeping that", title, number)
    db.event(con, "failed", f"chapter {number:g}: {reason}", series_id)
    con.commit()
    log.warning("%s: chapter %g: %s", title, number, reason)
    return f"chapter {number:g} failed: {reason}"


def _counted_short(con, manga_id: int, ch) -> int | None:
    """The page count of a source's copy of a fractional chapter when the
    last count that worked says it is too short to be the chapter (fewer
    than min_pages, as the source lists it now), else None: not fractional,
    never counted, listed differently since, or long enough."""
    if float(ch.number) == int(ch.number):
        return None
    try:
        r = con.execute("SELECT number, name, scanlator, uploaded, pages FROM page_probe WHERE manga_id=? AND"
                        " chapter_id=?", (manga_id, ch.id)).fetchone()
    except Exception as e:                  # a database without the table: nothing is known
        log.debug("page counts not read: %s", e)
        return None
    if r is None or not isinstance(r["pages"], int):
        return None
    from . import pagecounts
    if (float(r["number"]), r["name"] or None, r["scanlator"] or None, r["uploaded"] or None) \
            != pagecounts.listing(ch):
        return None
    return r["pages"] if r["pages"] < int(settings.get("min_pages")) else None


def _search_order(con, series_id: int, number: float, entries: list) -> list:
    """The order an automatic chapter search asks the source entries in, as
    a resolve ranks them: the entry the plan chose for the chapter first,
    then normal sources, rate-limited ones and page-by-page ones (slow)
    last; within each, the primary entry first, then by name."""
    row = con.execute("SELECT manga_id FROM chapter WHERE series_id=? AND number=?", (series_id, number)).fetchone()
    chosen = row["manga_id"] if row else None
    tier = _source_tier(con)
    return sorted(entries, key=lambda s: (s["manga_id"] != chosen, tier(s["source_name"]), not s["is_primary"]))


def _source_tier(con) -> Callable[[str], int]:
    """Source.tier by name, for entries stored without their Source: from
    Settings and the rate limits seen lately, as Client.sources() stamps
    them. A setting that cannot be read counts as not set."""
    from . import settings

    def listed(key: str) -> set:
        try:
            v = settings.get(key)
        except Exception as e:
            log.debug("could not read %s: %s: %s", key, type(e).__name__, e)
            return set()
        return set(v) if isinstance(v, list) else set()
    warm, throttled = listed("page_warm_sources"), listed("throttled_sources") | db.auto_throttled(con)

    def tier(name: str) -> int:
        key = name.lower().strip()
        return 2 if key in warm else 1 if key in throttled else 0
    return tier


DELETE_WAIT_SECS = 15       # a delete is a web request: it waits this long for a rename or import of the series


def delete_series(con, client: Client, series_id: int, delete_library: bool = False) -> None:
    """Stop tracking. Optionally remove the library folder (only the links
    this series recorded; Suwayomi's staging files are never touched). The
    Suwayomi entries are taken out of its library so it stops auto-updating
    them, except one another series uses too (the one kept when a series
    tracked twice is cleaned up); a Suwayomi outage does not block the delete.
    Komga is asked to scan once library files are gone, so the series leaves
    it now rather than at its next scheduled scan; the scan request runs on a
    thread of its own, so the delete does not wait for Komga's answer.
    serieslock.Busy when the series' files are being renamed or imported
    and that does not end in DELETE_WAIT_SECS: nothing is deleted then."""
    row = db.get_series(con, series_id)
    title, folder = row["title"], row["folder"]
    for s in db.sources(con, series_id):
        if con.execute("SELECT 1 FROM series_source WHERE manga_id=? AND series_id!=?",
                       (s["manga_id"], series_id)).fetchone():
            log.info("%s: %s entry left in Suwayomi's library: another series uses it", title, s["source_name"])
            continue
        try:
            client.set_in_library(s["manga_id"], False, retries=1, timeout=10)
        except SuwayomiError as e:
            log.warning("%s: could not unset library flag on %s entry: %s", title, s["source_name"], e)
    removed = False
    if con.in_transaction:
        con.commit()                    # never hold a write while waiting for the lock
    with serieslock.hold(series_id, wait_secs=DELETE_WAIT_SECS):   # not while its files are renamed or linked
        row = db.get_series(con, series_id)
        if row is not None:
            folder = row["folder"]      # a rename may have given it another one meanwhile
        if delete_library and folder:
            removed = _delete_library_files(con, series_id, title, folder)
            try:                        # its e-reader copies go with its library files
                conversions.remove_series_outputs(con, series_id)
            except Exception as e:
                log.warning("%s: its converted copies could not be removed: %s: %s", title, type(e).__name__, e)
        db.delete_series(con, series_id)
        db.event(con, "deleted", f"{title} removed" + (" with library files" if delete_library else ""))
        con.commit()
    log.info("%s: no longer tracked", title)
    if removed and komga.configured():
        # a delete is a web request: it does not wait for Komga's answer (up to 20 s)
        threading.Thread(target=komga.scan, name="mangarr-komga-scan", daemon=True).start()


def _delete_library_files(con, series_id: int, title: str, folder: str) -> bool:
    """Remove the library files this series recorded, then its folder if it is
    empty. Paths come from the database, which a restored backup can fill
    with anything, so only regular files inside this series' own library
    folder (itself inside LIBRARY_ROOT, symlinks resolved) are touched;
    anything else is skipped with a warning (series_library_dir,
    library_file). Returns whether a file or the folder was removed."""
    d = series_library_dir(title, folder)
    if d is None:
        return False
    removed = 0
    for c in db.chapters(con, series_id):
        p = c["library_path"]
        if not p or library_file(title, p, d) is None:
            continue
        try:
            os.remove(p)
            removed += 1
        except OSError as e:
            log.warning("%s: could not remove %s: %s", title, p, e)
    gone = False
    try:
        os.rmdir(d)
        gone = True
    except OSError as e:
        log.info("%s: library folder %s kept: %s", title, d, e)
    log.info("%s: removed %d file(s) from %s", title, removed, d)
    return bool(removed) or gone


def series_library_dir(title: str, folder) -> str | None:
    """The series' library folder, when mang-arr may remove files from it:
    one plain folder name (db.valid_folder) inside LIBRARY_ROOT once
    symlinks are resolved, and not the root itself. None (logged)
    otherwise."""
    root = library.config.LIBRARY_ROOT
    d = library.library_dir(folder) if db.valid_folder(folder) else None
    if d is None or not library.is_within(d, root) or os.path.realpath(d) == os.path.realpath(root):
        log.warning("%s: library folder %r is not a folder inside %s; no files deleted", title, folder, root)
        return None
    return d


def library_file(title: str, path: str, d: str) -> os.stat_result | None:
    """The lstat of `path` when mang-arr may remove it: a regular file (not
    a symlink) inside the series' library folder d (series_library_dir),
    symlinks resolved. None when it is not there, and (logged) when it is
    outside d, cannot be checked or is not a regular file."""
    import stat
    if not library.is_within(path, d):
        log.warning("%s: not deleting %s: it is outside the series' library folder %s", title, path, d)
        return None
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("%s: could not check %s: %s", title, path, e)
        return None
    if not stat.S_ISREG(st.st_mode):
        log.warning("%s: not deleting %s: not a regular file", title, path)
        return None
    return st


# -- import ------------------------------------------------------------------

def series_staging_dirs(con, series_id: int) -> list[tuple[str, str, int | None]]:
    """[(source name, folder, Suwayomi manga id)] where Suwayomi has written
    this series - only for source entries the plan trusts (an entry noted as
    'author differs' or 'too long' must not feed the library).
    The source name (Suwayomi's displayName, set by extension code) and the
    title are sanitised like Suwayomi's own folder names, and a folder whose
    real path is not inside the staging root (a '..' name, an absolute path
    stored by adopt or a restored backup, a symlink) is refused and logged."""
    out = []
    root = library.config.STAGING_ROOT
    for s in db.sources(con, series_id):
        if s["note"]:
            continue
        folder = source_folder(s)
        if not os.path.isdir(folder):
            continue
        if os.path.islink(folder) or not library.is_within(folder, root):
            log.warning("ignoring staging folder %s for %s: it is a symlink or outside %s",
                        oneline(folder, 300), oneline(s["source_name"], 60), root)
            continue
        out.append((s["source_name"], folder, s["manga_id"]))
    return out


def source_folder(s) -> str:
    """Where Suwayomi writes a source entry's chapters (a series_source row):
    the folder adopt recorded, else <staging>/<source>/<title> made safe as
    Suwayomi makes them. Not checked: see series_staging_dirs."""
    return s["folder"] or os.path.join(library.config.STAGING_ROOT, library.safe_title(s["source_name"]),
                                       library.safe_title(s["title"]))


SETTLE_SECONDS = 120    # a staged file younger than this may still be being written
_PAGES = re.compile(r"(\d+) pages")      # library.verify_archive's detail for a good archive


def import_series(con, series_id: int, client: Client | None = None, downloaded: dict | None = None) -> int:
    """Link every staged chapter into <library>/<folder>/. Returns how many
    chapters were newly linked. Files whose name carries no global chapter
    number (season episodes) are matched through Suwayomi's chapter list.
    With `downloaded` ({number: source name} of the chapters Suwayomi
    reports downloaded, suwayomi_downloaded), a chapter among them that no
    staged file was read as says why it is not in the library
    (_explain_missing) instead of waiting for a download that never comes.
    One bad file never stops the others: a failure is recorded on that
    chapter and the import goes on. A file that fails verification while it
    is still fresh (Suwayomi may be writing it) is left for the next import
    instead of being quarantined; old quarantined files are pruned.
    Each staging folder is opened once and its files are listed, checked,
    set aside and linked through that open folder (library.StagingFolder),
    so a folder swapped for a symlink mid-import cannot point any of it at
    other files. Every write is committed at once, so no database write is
    held across a Suwayomi call or an archive check. A cancel during the
    chapter-list lookup (a cancellable client) only leaves the unnumbered
    files for the next import; the rest is linked as usual. The series'
    lock is held meanwhile (serieslock: its files are not renamed while
    they are linked); serieslock.Busy when it is not free in time."""
    if con.in_transaction:
        con.commit()                    # never hold a write while waiting for the lock
    with serieslock.hold(series_id):
        linked = _import_series(con, series_id, client, downloaded)
    if linked:
        conversions.service.notify(series_id)       # e-reader copies of what is new (does nothing when off)
    return linked


def _import_series(con, series_id: int, client: Client | None, downloaded: dict | None) -> int:
    row = db.get_series(con, series_id)
    title, folder = row["title"], row["folder"]
    known = {r["number"]: dict(r) for r in db.chapters(con, series_id)}
    linked = 0
    staged = Staged()
    for source_name, staging, manga_id in series_staging_dirs(con, series_id):
        try:
            sf = library.StagingFolder(staging)
        except OSError as e:            # swapped for a symlink or removed since it was listed
            log.warning("%s: skipping staging folder %s: %s", title, oneline(staging, 300), e)
            staged.unchecked.add(source_name)
            continue
        with sf:
            linked += _import_folder(con, client, series_id, title, folder, known, sf, source_name, manga_id, staged)
    if downloaded:
        _explain_missing(con, series_id, title, downloaded, staged)
    if linked:
        db.event(con, "imported", f"{linked} chapter(s) linked into the library", series_id)
        log.info("%s: imported %d chapter(s) into %s", title, linked, library.library_dir(folder))
    con.commit()
    if linked:
        komga.scan_retrying()
    elif komga.take_retry():
        komga.scan()                    # the one retry of a scan Komga did not answer (scan_retrying)
    return linked


def _import_folder(con, client: Client | None, series_id: int, title: str, folder: str, known: dict,
                   sf: library.StagingFolder, source_name: str, manga_id: int | None,
                   staged: "Staged | None" = None) -> int:
    """import_series for one opened staging folder. Returns how many
    chapters were newly linked; what the folder holds goes into `staged`."""
    staged = staged if staged is not None else Staged()
    sf.prune_quarantine()
    doubled: list[tuple[str, float, str]] = []           # (file, number, the file that has it)
    found, unparsed = sf.scan(doubled)
    unread = list(unparsed)                 # not even Suwayomi's names match them
    asked = False                           # Suwayomi's names were there to match them
    if unparsed and client is not None and manga_id is not None:
        names: dict | None = None
        try:
            names = library.suwayomi_name_map(client.chapters(manga_id))
        except limits.Cancelled:
            log.info("%s: cancelled before %d unnumbered file(s) in %s were matched; the next import does it",
                     title, len(unparsed), sf.path)
        except SuwayomiError as e:
            log.warning("%s: cannot list chapters of %s entry to match %d unnamed file(s): %s",
                        title, source_name, len(unparsed), e)
        if names is not None:
            matched, unread, asked = 0, [], True
            for name in unparsed:
                n = library.match_unparsed(name, names)
                if n is not None and n not in found:
                    found[n] = name
                    matched += 1
                elif n is not None:
                    doubled.append((name, n, found[n]))
                    log.debug("%s: file %s matches chapter %g, which %s already is", title, name, n, found[n])
                else:
                    unread.append(name)
                    log.debug("%s: no chapter matches file %s", title, name)
            log.info("%s: %d of %d unnumbered file(s) in %s matched through Suwayomi's chapter list",
                     title, matched, len(unparsed), sf.path)
        else:
            staged.unchecked.add(source_name)       # matched at the next import
    elif unparsed:
        log.debug("%s: %d file(s) in %s without a chapter number", title, len(unparsed), sf.path)
    if unread and asked:
        log.info("%s: %d file(s) in %s whose chapter number cannot be read, not imported: %s", title, len(unread),
                 sf.path, ", ".join(oneline(u, 120) for u in unread[:5]) + (" ..." if len(unread) > 5 else ""))
    staged.numbers.update(found)
    staged.unread.setdefault(source_name, []).extend(unread)
    staged.doubled.setdefault(source_name, []).extend(doubled)
    linked = 0
    for n, name in found.items():
        prev = known.get(n)
        if prev and prev["status"] == "junk":
            continue
        if prev and prev["library_path"] and os.path.exists(prev["library_path"]):
            continue
        try:
            with sf.open(name) as f:
                dst = _import_file(con, series_id, title, folder, n, prev, f, source_name)
        except FileNotFoundError:       # renamed or deleted since the folder was listed
            log.warning("%s: ch %g: %s disappeared while it was being imported; skipped",
                        title, n, os.path.join(sf.path, name))
            continue
        except OSError as e:            # swapped for a symlink or special file, unreadable ...
            _import_failed(con, series_id, title, n, f"{source_name}: cannot read {name}", e)
            continue
        if dst:
            known[n] = {"number": n, "status": "have", "library_path": dst}
            linked += 1
    return linked


def _import_file(con, series_id: int, title: str, folder: str, n: float, prev, f: library.StagedFile,
                 source_name: str) -> str | None:
    """Check one opened staged file and link it into the library. Returns the
    library path, or None when it was not linked (why is recorded and
    logged). FileNotFoundError (the file was renamed away meanwhile) is left
    to the caller."""
    ok, detail = library.verify_archive(f)
    if ok is None:                          # ran out of time: slow or busy storage, nothing wrong with the file
        # no quarantine, no failure, no source penalty; the reason says on the
        # series page why the chapter is not in the library
        not_linked(con, series_id, n, f"{source_name}: downloaded, not linked yet: {detail}; checked again at the "
                   "next import")
        log.warning("%s: ch %g from %s could not be checked (%s); trying again at the next import",
                    title, n, source_name, detail)
        return None
    if not ok:
        if _still_being_written(f, title, n, source_name, detail):
            return None
        try:
            moved = library.quarantine(f)
        except FileNotFoundError:
            raise
        except OSError as e:
            _import_failed(con, series_id, title, n, f"{source_name}: bad file {f.name} not set aside", e)
            return None
        if not db.set_status(con, series_id, n, "failed", f"{source_name}: bad file ({detail}); set aside as "
                             f"{os.path.basename(moved)}, will be fetched again", only_from=_IMPORT_OWNED):
            log.info("%s: ch %g is %s: it stays so, although its file from %s was bad", title, n,
                     prev["status"] if prev else "not a chapter of the plan", source_name)
        db.record_source_result(con, source_name, "corrupt")
        con.commit()                        # never hold a write across the next file check
        log.warning("%s: ch %g from %s is unusable (%s); quarantined %s", title, n, source_name, detail, moved)
        return None
    label = prev["name"] if prev and "name" in prev.keys() else None
    chapter = naming.ChapterInfo(n, label)
    names = naming.SeriesNames.from_row(db.get_series(con, series_id) or {"title": title})
    options = settings.naming_options(con)
    name = naming.render(names, chapter, options)       # per the naming settings (Settings -> Media Management)
    expected = os.path.join(library.library_dir(folder), name)
    ours = bool(prev and prev["library_path"] == expected)
    try:
        dst = library.link_into_library(f, folder, n, replace=ours, name=name)
    except FileNotFoundError:
        raise
    except OSError as e:
        _import_failed(con, series_id, title, n, f"{source_name}: cannot link {f.name}", e)
        return None
    if dst is None:                         # another file has its name there (link_into_library logged it)
        not_linked(con, series_id, n, f"{source_name}: downloaded, not linked: the library already has another "
                   f"file at {expected}, which is never overwritten; move it away and the next import links this one")
        return None
    pages = _PAGES.fullmatch(detail)        # kept for the stuck-behind verdict (verdict.py compares lengths)
    db.set_have(con, series_id, n, f.path, dst, source_name, pages=int(pages.group(1)) if pages else None,
                file_title=naming.stored_title(chapter, options))
    con.commit()                            # never hold a write across the next file check
    log.debug("%s: linked ch %g <- %s", title, n, f.path)
    return dst


def not_linked(con, series_id: int, n: float, why: str) -> None:
    """Chapter n is downloaded (its file is in staging) but this import did
    not link it, for `why`; the next import tries again. The reason says so.
    One that had failed is wanted again with it: it arrived after all, so
    its retry schedule is over and it no longer holds the chapters after it
    as a failed one does. It holds them no more than any chapter Suwayomi
    has downloaded (_due and taken_back go past it). One no source listed
    in the last check still says it is waited for (db.with_listing_note). A
    chapter you skipped or ignored, or that is junk, stays as it is.
    Committed."""
    row = db.chapters_by_number(con, series_id, [n]).get(n)
    if row is not None:
        why = db.with_listing_note(row, why)
        if not db.set_status(con, series_id, n, "wanted", why, only_from=("failed",)):
            db.set_reason(con, series_id, n, why)
    con.commit()


def _still_being_written(f: library.StagedFile, title: str, n: float, source_name: str, detail: str) -> bool:
    """True when a file that failed verification was written less than
    SETTLE_SECONDS ago: Suwayomi is probably still writing it, so it is left
    for the next import instead of being quarantined (logged)."""
    try:
        age = time.time() - os.fstat(f.fd).st_mtime
    except OSError:
        return False
    if age >= SETTLE_SECONDS:
        return False
    log.info("%s: ch %g from %s is not readable yet (%s) and was written %.0fs ago; "
             "probably still being written, trying again at the next import", title, n, source_name, detail, age)
    return True


@dataclass
class Staged:
    """What an import found in a series' staging folders: every chapter
    number a file was read as (or matched to through Suwayomi's names), and
    per source the files that are no chapter it could tell: whose number
    could not be read (`unread`), or read as a number another file of the
    folder already has (`doubled`). A source whose folder could not be
    opened, or whose unnumbered files were not matched this time (cancelled,
    Suwayomi not answering), is `unchecked`: nothing is said about it."""
    numbers: set = field(default_factory=set)
    unread: dict = field(default_factory=dict)          # source name -> [file names]
    doubled: dict = field(default_factory=dict)         # source name -> [(file name, number, the file that has it)]
    unchecked: set = field(default_factory=set)

    def why_missing(self, n: float, source: str | None, alone: bool) -> str:
        """The reason of chapter n, which Suwayomi reports downloaded on
        `source` (None: some source) but no staged file was read as. `alone`:
        it is the only such chapter of that source, so a single unreadable
        file there is its file."""
        def files(d: dict) -> list[str]:
            return list(d.get(source, ())) if source is not None else [f for fs in d.values() for f in fs]

        def few(names: list[str]) -> str:
            return ", ".join(oneline(f, 70) for f in names[:2]) + (" ..." if len(names) > 2 else "")
        where = oneline(source, 60) if source else "Suwayomi"
        unread, doubled = files(self.unread), files(self.doubled)
        if len(unread) == 1 and alone:
            return f"{where}: downloaded as {oneline(unread[0], 150)} but its chapter number could not be read; " \
                   "rename it or report the name"
        if unread:
            return f"{where}: downloaded, but its file is one of {len(unread)} whose chapter number could not be " \
                   f"read ({few(unread)}); rename it or report the name"
        if doubled:
            f, m, other = doubled[0]
            said = f"{oneline(f, 70)} read as chapter {m:g}, which {oneline(other, 70)} already is"
            if len(doubled) == 1:
                return f"{where}: reported downloaded, but no file reads as chapter {n:g}; {said}: rename it or " \
                       "report the name"
            return f"{where}: reported downloaded, but no file reads as chapter {n:g}; {len(doubled)} files read as " \
                   f"a chapter another file already is ({said}, ...): rename them or report the names"
        return f"{where}: reported downloaded, but no file in its download folder reads as chapter {n:g}; delete " \
               "the download in Suwayomi to fetch it again"


def suwayomi_downloaded(plan: Plan) -> dict[float, str]:
    """{chapter number: source name} of the chapters Suwayomi reports
    downloaded on a trusted source of the plan (Plan.have)."""
    out: dict[float, str] = {}
    for m in plan.usable:
        for c in m.chapters:
            if c.downloaded:
                out.setdefault(c.number, m.source.name)
    return out


def _explain_missing(con, series_id: int, title: str, downloaded: dict, staged: Staged) -> None:
    """A chapter Suwayomi reports downloaded is never downloaded again, so
    one that no staged file was read as would wait for a download pass
    that never comes, every pass saying "nothing to download" (0.3.0's
    misread names, e.g. Dreaming Freedom 171.01-171.13). Its reason says
    what is wrong instead (Staged.why_missing); a failed one is wanted
    again (not_linked). Logged when the reason is new. Committed."""
    rows = db.chapters_by_number(con, series_id, [n for n in downloaded if n not in staged.numbers])
    missing = sorted(n for n, r in rows.items() if r["status"] in ("wanted", "failed")
                     and (downloaded[n] not in staged.unchecked if downloaded[n] is not None else not staged.unchecked))
    per_source: dict = {}
    for n in missing:
        per_source[downloaded[n]] = per_source.get(downloaded[n], 0) + 1
    fresh = []
    for n in missing:
        why = staged.why_missing(n, downloaded[n], per_source[downloaded[n]] == 1)
        if db.with_listing_note(rows[n], why) != rows[n]["reason"]:
            fresh.append(n)
        not_linked(con, series_id, n, why)
    if fresh:
        log.warning("%s: Suwayomi reports %d chapter(s) downloaded that no file in its download folder reads as, "
                    "so they are not in the library: %s (the chapter's reason says more)", title, len(fresh),
                    ", ".join(f"{n:g}" for n in fresh[:20]) + (" ..." if len(fresh) > 20 else ""))


def _import_failed(con, series_id: int, title: str, n: float, what: str, e: OSError) -> None:
    """One file could not be handled: recorded on its chapter and logged; the
    import goes on with the other files. Committed at once, so the write is
    not held across the checks and Suwayomi calls still to come."""
    why = f"{what}: {type(e).__name__}: {e}"
    log.error("%s: ch %g: %s", title, n, why)
    db.set_status(con, series_id, n, "failed", why[:500], only_from=_IMPORT_OWNED)   # never over your skip
    con.commit()


# -- adopt -------------------------------------------------------------------

@dataclass
class AdoptItem:
    source: str
    folder_name: str
    path: str
    numbers: dict[float, str]
    unparsed: list[str]
    seasons: bool = False
    series: Series | None = None
    candidates: list[Series] = field(default_factory=list)
    manga_id: int | None = None
    tracked: bool = False
    lookup_error: str | None = None     # AniList and MangaDex could not be asked: not identified, scan again

    @property
    def key(self) -> str:
        """Stable id for the Import form: the staging path, hashed (list
        positions shift when a rescan finds a new folder)."""
        return hashlib.sha1(self.path.encode("utf-8", "surrogateescape")).hexdigest()[:16]


ADOPT_PAGE = 500                # entries per request
ADOPT_MAX_ENTRIES = 200_000     # stop paging after this many (Suwayomi caches every search hit)


def suwayomi_downloaded_entries(client: Client, wanted: set[tuple[str, str]] | None = None,
                                progress: Callable[[str], None] | None = None,
                                should_cancel: Callable[[], bool] | None = None) -> dict[tuple[str, str], int]:
    """{(source folder name, series folder name): manga id} for every entry
    with downloads. Both names are sanitised the way Suwayomi names its
    staging folders, so a source whose displayName has ':' or '/' matches.
    Suwayomi caches every search hit, so the list is paged (ADOPT_PAGE per
    request, one try each) and paging stops as soon as every folder in
    `wanted` is found, or after ADOPT_MAX_ENTRIES. `progress` hears how far
    it got; a cancel raises limits.Cancelled between pages."""
    # downloadCount is not filterable server-side, so filter here
    out: dict[tuple[str, str], int] = {}
    offset = 0
    while True:
        if should_cancel and should_cancel():
            raise limits.Cancelled()
        if progress:
            progress(f"listing what Suwayomi has downloaded ({offset} entries so far)")
        nodes, more = client.mangas_page(offset, ADOPT_PAGE)
        for m in nodes:
            if not m.get("downloadCount"):
                continue
            src = library.safe_title((m.get("source") or {}).get("displayName") or "?")
            out[(src, library.safe_title(m["title"]))] = m["id"]
        offset += len(nodes)
        if wanted is not None and wanted <= out.keys():
            log.debug("adopt: all %d staged folders found after %d entries", len(wanted), offset)
            break
        if not more:
            break
        if offset >= ADOPT_MAX_ENTRIES:
            log.warning("adopt: stopped listing Suwayomi entries after %d; folders not matched by then are "
                        "adopted without a source entry (a refresh finds it)", offset)
            break
    return out


def plan_adopt(client: Client, only: str | None = None, progress: Callable[[str], None] | None = None,
               should_cancel: Callable[[], bool] | None = None) -> list[AdoptItem]:
    """Inspect every staged series folder and work out what it is. Each one
    costs a metadata lookup (rate limited), so a big staging tree takes a
    while: `progress` hears which folder is being identified, and a cancel
    raises limits.Cancelled between folders.

    A folder AniList and MangaDex could not be asked about (a network blip)
    keeps its lookup_error and is not identified; the others are. When not
    one lookup got an answer, metadata.LookupError_ is raised instead: a
    list of nothing but unidentified folders would say nothing."""
    dirs = [(src, name, path) for src, name, path in library.staging_dirs()
            if not only or only.lower() in name.lower()]
    entries = suwayomi_downloaded_entries(client, {(src, name) for src, name, _ in dirs}, progress, should_cancel)
    items: list[AdoptItem] = []
    cache: dict[str, tuple[Series | None, list[Series]]] = {}
    asked = failed = 0
    error = None
    for i, (src, name, path) in enumerate(dirs, 1):
        if should_cancel and should_cancel():
            raise limits.Cancelled()
        if progress:
            progress(f"identifying folder {i} of {len(dirs)}: {oneline(name, 80)}")
        numbers, unparsed = library.scan_series_dir(path)
        seasons = any(library.parse_season(u) for u in unparsed)
        it = AdoptItem(src, name, path, numbers, unparsed, seasons, manga_id=entries.get((src, name)))
        if name not in cache:
            asked += 1
            try:
                cache[name] = metadata.lookup(name)
            except metadata.LookupError_ as e:      # not cached: a later folder of that name asks again
                failed += 1
                error = e
                it.lookup_error = f"not looked up: {e}"
        if name in cache:
            it.series, it.candidates = cache[name]
        items.append(it)
        tag = it.series.ref if it.series else "NOT LOOKED UP" if it.lookup_error else \
            f"REVIEW ({len(it.candidates)} candidates)"
        log.info("%-14s %-42s %4d ch%s -> %s", src[:14], name[:42], len(numbers),
                 f" +{len(unparsed)} unparsed" if unparsed else "", tag)
    if asked and failed == asked:
        raise error
    if failed:
        log.warning("adopt scan: %d of %d folder(s) could not be looked up (%s); scan again to identify them",
                    failed, asked, error)
    return items


def apply_adopt(con, items: list[AdoptItem]) -> tuple[list[int], int]:
    """Register the identified folders. Folders of the same series (one per
    source) merge into one tracked series, and a folder of a series tracked
    under its other reference (duplicates.py) into that one. Folders of one
    new series found under both references merge too, as the AniList one.
    A tracked series keeps its primary source. Returns (series ids,
    chapters)."""
    groups: dict[str, list[AdoptItem]] = {}
    for it in items:
        if it.series:
            row = duplicates.tracked_as(con, it.series)
            if row is not None and row["ref"] != it.series.ref:
                it = replace(it, series=db.series_to_model(row))
            groups.setdefault(row["ref"] if row is not None else duplicates.identity(it.series), []).append(it)
    n_chapters = 0
    ids = []
    for group in groups.values():
        series = next((it.series for it in group if it.series.anilist_id is not None), group[0].series)
        series_id = db.upsert_series(con, series)
        ids.append(series_id)
        primary = {r[0] for r in con.execute("SELECT manga_id FROM series_source WHERE series_id=? AND is_primary=1",
                                             (series_id,))}
        first = next((it for it in group if it.manga_id is not None), None)
        for it in group:
            if it.manga_id is not None:
                con.execute(
                    "INSERT OR REPLACE INTO series_source (series_id, manga_id, source_name, title,"
                    " author, match_level, author_level, chapter_count, max_chapter, note,"
                    " is_primary, folder, seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (series_id, it.manga_id, it.source, it.folder_name, None, 0, 1,
                     len(it.numbers), max(it.numbers) if it.numbers else 0, None,
                     int(it.manga_id in primary if primary else it is first), it.path, db.now()))
            for n, path in it.numbers.items():
                db.set_have(con, series_id, n, path, None, it.source)
                n_chapters += 1
        db.event(con, "added", f"adopted from {', '.join(it.source for it in group)}", series_id)
        log.info("adopted %s <- %s", series.title,
                 ", ".join(f"{it.source} ({len(it.numbers)})" for it in group))
    con.commit()
    return ids, n_chapters

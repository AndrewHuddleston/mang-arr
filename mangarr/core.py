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
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import db, downloader, komga, library, limits, metadata, metrics
from .matching import oneline
from .model import Series
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

    @property
    def downloaded(self) -> int:
        return sum(1 for r in self.results.values() if r == "ok")

    @property
    def failed(self) -> int:
        return len(self.results) - self.downloaded


# -- add / refresh ------------------------------------------------------------

def add_series(con, client: Client, series: Series, download: bool = True, do_import: bool = True,
               series_id: int | None = None, should_cancel: Callable[[], bool] | None = None,
               progress: Callable[[str], None] | None = None) -> Outcome:
    """Track a series: resolve it, remember the plan, fetch what is missing,
    link the results into the library. With series_id (a refresh) the row
    must still exist afterwards, or the work is abandoned. A cancel cuts the
    Suwayomi call in flight short: while sources are searched it raises
    limits.Cancelled with nothing saved, afterwards what was saved stays (a
    download returns what arrived)."""
    plan = resolve(client, series, reliability=db.reliability(con), should_cancel=should_cancel, progress=progress)
    lookups = with_cancel(client, should_cancel)
    if series_id is not None and not db.get_series(con, series_id):
        raise Gone(f"{series.title} was deleted during the refresh")
    series_id = db.upsert_series(con, series)
    p = primary(plan)
    expired = db.save_plan(con, series_id, plan, p.manga_id if p else None)
    if expired:
        log.warning("%s: %s could not be searched for over %d days; its entry is dropped and the chapters "
                    "only it listed count as unavailable", series.title, ", ".join(expired), db.UNREACHABLE_KEEP_DAYS)
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
    if do_import:
        out.imported += import_series(con, series_id, lookups)
    if download and p:
        out.results = download_wanted(con, client, series_id, plan, should_cancel, progress)
        if do_import:
            if not db.get_series(con, series_id):      # deleted while it downloaded: link nothing
                raise Gone(f"{series.title} was deleted during the download")
            out.imported += import_series(con, series_id, lookups)
    return out


def refresh_series(con, client: Client, series_id: int, download: bool = True,
                   should_cancel: Callable[[], bool] | None = None,
                   progress: Callable[[str], None] | None = None) -> Outcome:
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
                      progress=progress)


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
    if later:
        nxt = min(r["next_try"] for r in later)[:16]
        parts.append(f"{len(later)} failed chapter(s) waiting for retry (next {nxt})")
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


def download_wanted(con, client: Client, series_id: int, plan: Plan,
                    should_cancel: Callable[[], bool] | None = None,
                    progress: Callable[[str], None] | None = None) -> dict:
    """Download the plan's wanted chapters. No write transaction is open while
    the downloader runs (it can take hours); the outcome is written after, and
    never over a status the user set in the meantime (e.g. 'ignored')."""
    rows = {r["number"]: r for r in db.chapters(con, series_id)}
    have_on_disk = {n for n, r in rows.items() if r["status"] == "have"}
    ignored = {n for n, r in rows.items() if r["status"] == "ignored"}
    later = {n for n, r in rows.items() if r["status"] == "failed" and r["next_try"] and r["next_try"] > db.now()}
    wanted = [n for n in plan.wanted() if n not in have_on_disk and n not in later and n not in ignored]
    if later:
        log.info("%s: %d failed chapter(s) not due for another attempt yet", plan.series.title, len(later))
    if ignored & set(plan.wanted()):
        log.info("%s: %d ignored chapter(s) not downloaded", plan.series.title, len(ignored & set(plan.wanted())))
    if not wanted:
        log.info("%s: nothing to download", plan.series.title)
        return {}
    if con.in_transaction:                          # never hold a write across the downloads
        con.commit()
    wanted_set = set(wanted)

    def dropped() -> set:
        """Chapters ignored (or linked) since the list was made: asked before each chunk."""
        return {r["number"] for r in con.execute(
            "SELECT number, status FROM chapter WHERE series_id=? AND status IN ('ignored','have')", (series_id,))
            if r["number"] in wanted_set}
    reasons: dict = {}
    seen_throttle: set = set()
    results = downloader.download(client, plan, only=wanted_set, should_cancel=should_cancel, reasons=reasons,
                                  progress=progress, throttled=seen_throttle, dropped=dropped)
    for name in seen_throttle:
        db.record_throttle(con, name)
        log.info("%s rate-limited us; it is paced automatically from now on", name)
    for n, r in results.items():
        m = plan.assignment.get(n)
        metrics.record_download(m.source.name if m else "?", r)
        if m:
            db.record_source_result(con, m.source.name, "ok" if r == "ok" else "failed")
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
    con.commit()
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
        ok, failed, why = downloader.download_one(client, s["manga_id"], ch, title, s["source_name"],
                                                  should_cancel=cancel, progress=progress)
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


def delete_series(con, client: Client, series_id: int, delete_library: bool = False) -> None:
    """Stop tracking. Optionally remove the library folder (only the links
    this series recorded; Suwayomi's staging files are never touched). The
    Suwayomi entries are taken out of its library so it stops auto-updating
    them; a Suwayomi outage does not block the delete."""
    row = db.get_series(con, series_id)
    title, folder = row["title"], row["folder"]
    for s in db.sources(con, series_id):
        try:
            client.set_in_library(s["manga_id"], False, retries=1, timeout=10)
        except SuwayomiError as e:
            log.warning("%s: could not unset library flag on %s entry: %s", title, s["source_name"], e)
    if delete_library and folder:
        _delete_library_files(con, series_id, title, folder)
    db.delete_series(con, series_id)
    db.event(con, "deleted", f"{title} removed" + (" with library files" if delete_library else ""))
    con.commit()
    log.info("%s: no longer tracked", title)


def _delete_library_files(con, series_id: int, title: str, folder: str) -> None:
    """Remove the library files this series recorded, then its folder if it is
    empty. Paths come from the database, which a restored backup can fill
    with anything, so only regular files inside this series' own library
    folder (itself inside LIBRARY_ROOT, symlinks resolved) are touched;
    anything else is skipped with a warning."""
    import stat
    root = library.config.LIBRARY_ROOT
    d = library.library_dir(folder) if db.valid_folder(folder) else None
    if d is None or not library.is_within(d, root) or os.path.realpath(d) == os.path.realpath(root):
        log.warning("%s: library folder %r is not a folder inside %s; no files deleted", title, folder, root)
        return
    removed = 0
    for c in db.chapters(con, series_id):
        p = c["library_path"]
        if not p:
            continue
        if not library.is_within(p, d):
            log.warning("%s: not deleting %s: it is outside the series' library folder %s", title, p, d)
            continue
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            continue
        except OSError as e:
            log.warning("%s: could not check %s: %s", title, p, e)
            continue
        if not stat.S_ISREG(st.st_mode):
            log.warning("%s: not deleting %s: not a regular file", title, p)
            continue
        try:
            os.remove(p)
            removed += 1
        except OSError as e:
            log.warning("%s: could not remove %s: %s", title, p, e)
    try:
        os.rmdir(d)
    except OSError as e:
        log.info("%s: library folder %s kept: %s", title, d, e)
    log.info("%s: removed %d file(s) from %s", title, removed, d)


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
        folder = s["folder"] or os.path.join(root, library.safe_title(s["source_name"]),
                                             library.safe_title(s["title"]))
        if not os.path.isdir(folder):
            continue
        if os.path.islink(folder) or not library.is_within(folder, root):
            log.warning("ignoring staging folder %s for %s: it is a symlink or outside %s",
                        oneline(folder, 300), oneline(s["source_name"], 60), root)
            continue
        out.append((s["source_name"], folder, s["manga_id"]))
    return out


SETTLE_SECONDS = 120    # a staged file younger than this may still be being written


def import_series(con, series_id: int, client: Client | None = None) -> int:
    """Link every staged chapter into <library>/<folder>/. Returns how many
    chapters were newly linked. Files whose name carries no global chapter
    number (season episodes) are matched through Suwayomi's chapter list.
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
    files for the next import; the rest is linked as usual."""
    row = db.get_series(con, series_id)
    title, folder = row["title"], row["folder"]
    known = {r["number"]: dict(r) for r in db.chapters(con, series_id)}
    linked = 0
    for source_name, staging, manga_id in series_staging_dirs(con, series_id):
        try:
            sf = library.StagingFolder(staging)
        except OSError as e:            # swapped for a symlink or removed since it was listed
            log.warning("%s: skipping staging folder %s: %s", title, oneline(staging, 300), e)
            continue
        with sf:
            linked += _import_folder(con, client, series_id, title, folder, known, sf, source_name, manga_id)
    if linked:
        db.event(con, "imported", f"{linked} chapter(s) linked into the library", series_id)
        log.info("%s: imported %d chapter(s) into %s", title, linked, library.library_dir(folder))
    con.commit()
    if linked:
        komga.scan()
    return linked


def _import_folder(con, client: Client | None, series_id: int, title: str, folder: str, known: dict,
                   sf: library.StagingFolder, source_name: str, manga_id: int | None) -> int:
    """import_series for one opened staging folder. Returns how many
    chapters were newly linked."""
    sf.prune_quarantine()
    found, unparsed = sf.scan()
    if unparsed and client is not None and manga_id is not None:
        names: dict | None = None
        try:
            names = library.suwayomi_name_map(client.chapters(manga_id))
        except limits.Cancelled:
            log.info("%s: cancelled before %d unnumbered file(s) in %s were matched; the next import does it",
                     title, len(unparsed), sf.path)
        except SuwayomiError as e:
            names = {}
            log.warning("%s: cannot list chapters of %s entry to match %d unnamed file(s): %s",
                        title, source_name, len(unparsed), e)
        if names is not None:
            matched = 0
            for name in unparsed:
                n = library.match_unparsed(name, names)
                if n is not None and n not in found:
                    found[n] = name
                    matched += 1
                else:
                    log.debug("%s: no chapter matches file %s", title, name)
            log.info("%s: %d of %d unnumbered file(s) in %s matched through Suwayomi's chapter list",
                     title, matched, len(unparsed), sf.path)
    elif unparsed:
        log.debug("%s: %d file(s) in %s without a chapter number", title, len(unparsed), sf.path)
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
        # the status stays (no quarantine, no failure, no source penalty); the
        # reason says on the series page why the chapter is not in the library
        db.set_reason(con, series_id, n, f"{source_name}: downloaded, not linked yet: {detail}; checked again at "
                      "the next import")
        con.commit()
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
        db.set_status(con, series_id, n, "failed", f"{source_name}: bad file ({detail}); set aside as "
                      f"{os.path.basename(moved)}, will be fetched again")
        db.record_source_result(con, source_name, "corrupt")
        con.commit()                        # never hold a write across the next file check
        log.warning("%s: ch %g from %s is unusable (%s); quarantined %s", title, n, source_name, detail, moved)
        return None
    label = prev["name"] if prev and "name" in prev.keys() else None
    expected = os.path.join(library.library_dir(folder), library.chapter_filename(n, label))
    ours = bool(prev and prev["library_path"] == expected)
    try:
        dst = library.link_into_library(f, folder, n, replace=ours, label=label)
    except FileNotFoundError:
        raise
    except OSError as e:
        _import_failed(con, series_id, title, n, f"{source_name}: cannot link {f.name}", e)
        return None
    if dst is None:
        return None
    db.set_have(con, series_id, n, f.path, dst, source_name)
    con.commit()                            # never hold a write across the next file check
    log.debug("%s: linked ch %g <- %s", title, n, f.path)
    return dst


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


def _import_failed(con, series_id: int, title: str, n: float, what: str, e: OSError) -> None:
    """One file could not be handled: recorded on its chapter and logged; the
    import goes on with the other files. Committed at once, so the write is
    not held across the checks and Suwayomi calls still to come."""
    why = f"{what}: {type(e).__name__}: {e}"
    log.error("%s: ch %g: %s", title, n, why)
    db.set_status(con, series_id, n, "failed", why[:500])
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
    source) merge into one tracked series. Returns (series ids, chapters)."""
    by_ref: dict[str, list[AdoptItem]] = {}
    for it in items:
        if it.series:
            by_ref.setdefault(it.series.ref, []).append(it)
    n_chapters = 0
    ids = []
    for group in by_ref.values():
        series = group[0].series
        series_id = db.upsert_series(con, series)
        ids.append(series_id)
        for it in group:
            if it.manga_id is not None:
                con.execute(
                    "INSERT OR REPLACE INTO series_source (series_id, manga_id, source_name, title,"
                    " author, match_level, author_level, chapter_count, max_chapter, note,"
                    " is_primary, folder, seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (series_id, it.manga_id, it.source, it.folder_name, None, 0, 1,
                     len(it.numbers), max(it.numbers) if it.numbers else 0, None,
                     int(it is group[0]), it.path, db.now()))
            for n, path in it.numbers.items():
                db.set_have(con, series_id, n, path, None, it.source)
                n_chapters += 1
        db.event(con, "added", f"adopted from {', '.join(it.source for it in group)}", series_id)
        log.info("adopted %s <- %s", series.title,
                 ", ".join(f"{it.source} ({len(it.numbers)})" for it in group))
    con.commit()
    return ids, n_chapters

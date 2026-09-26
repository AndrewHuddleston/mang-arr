"""The operations, independent of how they are invoked (CLI, web, worker).

    add      identity -> resolve -> save -> download -> import
    refresh  refresh metadata, re-resolve a tracked series, pick up new chapters
    import   link what is on disk into the library
    adopt    register everything Suwayomi already downloaded
    delete   stop tracking, optionally remove the library folder
"""
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field

from . import db, downloader, komga, library, metadata, metrics
from .model import Series
from .resolver import Plan, primary, ranges, resolve
from .suwayomi import Client, SuwayomiError

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
               series_id: int | None = None, should_cancel: Callable[[], bool] | None = None) -> Outcome:
    """Track a series: resolve it, remember the plan, fetch what is missing,
    link the results into the library. With series_id (a refresh) the row
    must still exist afterwards, or the work is abandoned."""
    plan = resolve(client, series)
    if series_id is not None and not db.get_series(con, series_id):
        raise Gone(f"{series.title} was deleted during the refresh")
    series_id = db.upsert_series(con, series)
    p = primary(plan)
    db.save_plan(con, series_id, plan, p.manga_id if p else None)
    db.event(con, "resolved", _resolve_summary(plan), series_id)
    if not p:
        db.event(con, "review", _review_summary(plan), series_id)
        con.execute("UPDATE series SET last_error=? WHERE id=?", ("no usable source has this series", series_id))
        log.warning("%s: no usable source. %s", series.title, _review_summary(plan))
    con.commit()
    out = Outcome(series_id, plan)
    if p:
        _set_library_entries(client, plan, p.manga_id)
    if do_import:
        out.imported += import_series(con, series_id, client)
    if download and p:
        out.results = download_wanted(con, client, series_id, plan, should_cancel)
        if do_import:
            out.imported += import_series(con, series_id, client)
    return out


def refresh_series(con, client: Client, series_id: int, download: bool = True,
                   should_cancel: Callable[[], bool] | None = None) -> Outcome:
    """Re-check a tracked series. Metadata is refreshed first (status,
    chapter count, new synonyms) and falls back to the stored row when the
    provider is unavailable."""
    row = db.get_series(con, series_id)
    if not row:
        raise Gone(f"series #{series_id} does not exist")
    series = db.series_to_model(row)
    if not series.manual:
        try:
            fresh = metadata.by_ref(row["ref"])
            if fresh:
                series = fresh
                log.debug("%s: metadata refreshed (%s, %s ch)", series.title, series.status, series.chapters)
        except (metadata.LookupError_, ValueError) as e:
            log.warning("%s: metadata refresh failed, using stored record: %s", row["title"], e)
    return add_series(con, client, series, download=download, series_id=series_id, should_cancel=should_cancel)


def _resolve_summary(plan: Plan) -> str:
    used = sorted({m.source.name for m in plan.assignment.values()})
    s = f"{len(plan.chapters)} chapters listed from {', '.join(used) or 'no source'}; {len(plan.wanted())} wanted"
    if plan.junk:
        s += f"; {len(plan.junk)} junk skipped"
    if plan.gaps():
        s += f"; gaps nobody lists: {ranges(plan.gaps())}"
    if plan.unreachable:
        s += f"; unreachable: {', '.join(src.name for src, _ in plan.unreachable)}"
    return s


def _review_summary(plan: Plan) -> str:
    parts = []
    noted = [m for m in plan.matches if m.note]
    if noted:
        parts.append("not used: " + "; ".join(f"{m.source.name} ({m.note})" for m in noted[:4]))
    if plan.rejected:
        titles = []
        for r in plan.rejected:
            if r.title not in titles:
                titles.append(r.title)
        parts.append("rejected titles: " + " | ".join(titles[:6]))
    if plan.unreachable:
        parts.append("unreachable: " + ", ".join(src.name for src, _ in plan.unreachable))
    return "; ".join(parts) or "no source returned anything"


def _set_library_entries(client: Client, plan: Plan, primary_manga_id: int) -> None:
    """Only the primary entry stays in Suwayomi's library, so its own update
    fetches new chapters from one source, not five copies."""
    for m in plan.matches:
        try:
            client.set_in_library(m.manga_id, m.manga_id == primary_manga_id, retries=1, timeout=30)
        except SuwayomiError as e:
            log.debug("could not set library flag on %d: %s", m.manga_id, e)


def download_wanted(con, client: Client, series_id: int, plan: Plan,
                    should_cancel: Callable[[], bool] | None = None) -> dict:
    have_on_disk = {r["number"] for r in db.chapters(con, series_id) if r["status"] == "have"}
    wanted = [n for n in plan.wanted() if n not in have_on_disk]
    if not wanted:
        log.info("%s: nothing to download", plan.series.title)
        return {}
    reasons: dict = {}
    results = downloader.download(client, plan, only=set(wanted), should_cancel=should_cancel, reasons=reasons)
    for n, r in results.items():
        m = plan.assignment.get(n)
        metrics.record_download(m.source.name if m else "?", r)
        if r != "ok":
            db.set_status(con, series_id, n, "failed", reasons.get(n, "download failed"))
    ok = sum(1 for r in results.values() if r == "ok")
    failed = sorted(n for n, r in results.items() if r != "ok")
    msg = f"{ok} chapter(s) downloaded, {len(failed)} failed"
    if failed:
        msg += ": " + "; ".join(f"ch {n:g}: {reasons.get(n, '?')}" for n in failed[:5])
        if len(failed) > 5:
            msg += f"; ... {len(failed) - 5} more"
    db.event(con, "downloaded", msg[:900], series_id)
    for n in wanted:
        if n not in results:                      # the pass ended (cancelled/interrupted) before this one
            db.set_status(con, series_id, n, "wanted",
                          "not attempted: the download pass was cancelled or interrupted before this chapter")
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


def download_chapter(con, client: Client, series_id: int, number: float, manga_id: int | None = None) -> str:
    """Fetch one chapter now: from the given source entry (manual search) or
    from the best trusted entry that lists it (automatic search). Returns a
    message; the chapter row is updated with the outcome."""
    row = db.get_series(con, series_id)
    if not row:
        raise Gone(f"series #{series_id} does not exist")
    title = row["title"]
    entries = [s for s in db.sources(con, series_id)
               if (manga_id is None and not s["note"]) or s["manga_id"] == manga_id]
    if not entries:
        raise ValueError("no such source entry for this series")
    tried = []
    for s in entries:
        chapters = client.chapters(s["manga_id"])
        ch = next((c for c in chapters if c.number == number), None)
        if ch is None:
            tried.append(f"{s['source_name']}: does not list it")
            continue
        if s["note"] and manga_id is None:
            continue
        ok, failed, why = downloader.download_one(client, s["manga_id"], ch, title, s["source_name"])
        metrics.record_download(s["source_name"], "ok" if ok else "failed")
        if ok:
            db.set_status(con, series_id, number, "wanted", None)     # import_series flips it to have
            con.execute("UPDATE chapter SET manga_id=?, source_name=?, name=COALESCE(?, name),"
                        " uploaded=COALESCE(?, uploaded) WHERE series_id=? AND number=?",
                        (s["manga_id"], s["source_name"], ch.name, ch.uploaded, series_id, number))
            con.commit()
            linked = import_series(con, series_id, client)
            msg = f"chapter {number:g} downloaded from {s['source_name']}" + (" and linked" if linked else "")
            db.event(con, "downloaded", msg, series_id)
            log.info("%s: %s", title, msg)
            return msg
        tried.append(f"{s['source_name']}: {why.get(number, 'failed')}")
    reason = "; ".join(tried) or "no source lists this chapter"
    db.set_status(con, series_id, number, "failed", reason)
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
        removed = 0
        for c in db.chapters(con, series_id):
            p = c["library_path"]
            if p and os.path.isfile(p):
                try:
                    os.remove(p)
                    removed += 1
                except OSError as e:
                    log.warning("%s: could not remove %s: %s", title, p, e)
        d = library.library_dir(folder)
        try:
            os.rmdir(d)
        except OSError as e:
            log.info("%s: library folder %s kept: %s", title, d, e)
        log.info("%s: removed %d file(s) from %s", title, removed, d)
    db.delete_series(con, series_id)
    db.event(con, "deleted", f"{title} removed" + (" with library files" if delete_library else ""))
    con.commit()
    log.info("%s: no longer tracked", title)


# -- import ------------------------------------------------------------------

def series_staging_dirs(con, series_id: int) -> list[tuple[str, str, int | None]]:
    """[(source name, folder, Suwayomi manga id)] where Suwayomi has written
    this series - only for source entries the plan trusts (an entry noted as
    'author differs' or 'too long' must not feed the library)."""
    out = []
    for s in db.sources(con, series_id):
        if s["note"]:
            continue
        folder = s["folder"] or os.path.join(library.config.STAGING_ROOT, s["source_name"],
                                             library.safe_title(s["title"]))
        if os.path.isdir(folder):
            out.append((s["source_name"], folder, s["manga_id"]))
    return out


def import_series(con, series_id: int, client: Client | None = None) -> int:
    """Link every staged chapter into <library>/<folder>/. Returns how many
    chapters were newly linked. Files whose name carries no global chapter
    number (season episodes) are matched through Suwayomi's chapter list."""
    row = db.get_series(con, series_id)
    title, folder = row["title"], row["folder"]
    known = {r["number"]: dict(r) for r in db.chapters(con, series_id)}
    linked = 0
    for source_name, staging, manga_id in series_staging_dirs(con, series_id):
        found, unparsed = library.scan_series_dir(staging)
        if unparsed and client is not None and manga_id is not None:
            try:
                names = library.suwayomi_name_map(client.chapters(manga_id))
            except SuwayomiError as e:
                names = {}
                log.warning("%s: cannot list chapters of %s entry to match %d unnamed file(s): %s",
                            title, source_name, len(unparsed), e)
            matched = 0
            for path in unparsed:
                n = library.match_unparsed(path, names)
                if n is not None and n not in found:
                    found[n] = path
                    matched += 1
                else:
                    log.debug("%s: no chapter matches file %s", title, os.path.basename(path))
            log.info("%s: %d of %d unnumbered file(s) in %s matched through Suwayomi's chapter list",
                     title, matched, len(unparsed), staging)
        elif unparsed:
            log.debug("%s: %d file(s) in %s without a chapter number", title, len(unparsed), staging)
        for n, path in found.items():
            prev = known.get(n)
            if prev and prev["status"] == "junk":
                continue
            if prev and prev["library_path"] and os.path.exists(prev["library_path"]):
                continue
            label = prev["name"] if prev and "name" in prev.keys() else None
            expected = os.path.join(library.library_dir(folder), library.chapter_filename(n, label))
            ours = bool(prev and prev["library_path"] == expected)
            dst = library.link_into_library(path, folder, n, replace=ours, label=label)
            if dst is None:
                continue
            db.set_have(con, series_id, n, path, dst, source_name)
            known[n] = {"number": n, "status": "have", "library_path": dst}
            log.debug("%s: linked ch %g <- %s", title, n, path)
            linked += 1
    if linked:
        db.event(con, "imported", f"{linked} chapter(s) linked into the library", series_id)
        log.info("%s: imported %d chapter(s) into %s", title, linked, library.library_dir(folder))
    con.commit()
    if linked:
        komga.scan()
    return linked


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


def suwayomi_downloaded_entries(client: Client) -> dict[tuple[str, str], int]:
    """{(source name, folder name): manga id} for every entry with downloads."""
    # downloadCount is not filterable server-side; the list of every cached
    # entry is small text, so filter here.
    d = client.gq("{ mangas { nodes { id title downloadCount source { displayName } } } }", timeout=120)
    out = {}
    for m in d["mangas"]["nodes"]:
        if not m.get("downloadCount"):
            continue
        src = (m.get("source") or {}).get("displayName") or "?"
        out[(src, library.safe_title(m["title"]))] = m["id"]
    return out


def plan_adopt(client: Client, only: str | None = None) -> list[AdoptItem]:
    """Inspect every staged series folder and work out what it is."""
    entries = suwayomi_downloaded_entries(client)
    items: list[AdoptItem] = []
    cache: dict[str, tuple[Series | None, list[Series]]] = {}
    for src, name, path in library.staging_dirs():
        if only and only.lower() not in name.lower():
            continue
        numbers, unparsed = library.scan_series_dir(path)
        seasons = any(library.parse_season(u) for u in unparsed)
        it = AdoptItem(src, name, path, numbers, unparsed, seasons, manga_id=entries.get((src, name)))
        if name not in cache:
            cache[name] = metadata.lookup(name)
        it.series, it.candidates = cache[name]
        items.append(it)
        tag = it.series.ref if it.series else f"REVIEW ({len(it.candidates)} candidates)"
        log.info("%-14s %-42s %4d ch%s -> %s", src[:14], name[:42], len(numbers),
                 f" +{len(unparsed)} unparsed" if unparsed else "", tag)
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

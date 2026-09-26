"""The operations, independent of how they are invoked (CLI, web, worker).

    add      identity -> resolve -> save -> download -> import
    refresh  re-resolve a tracked series, pick up new chapters
    import   link what is on disk into the library
    adopt    register everything Suwayomi already downloaded
"""
import logging
import os
from dataclasses import dataclass, field

from . import db, downloader, komga, library, metadata, metrics
from .model import Series
from .resolver import Plan, primary, resolve
from .suwayomi import Client, SuwayomiError

log = logging.getLogger(__name__)


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

def add_series(con, client: Client, series: Series, download: bool = True,
               do_import: bool = True) -> Outcome:
    """Track a series: resolve it, remember the plan, fetch what is missing,
    link the results into the library."""
    plan = resolve(client, series)
    series_id = db.upsert_series(con, series)
    p = primary(plan)
    db.save_plan(con, series_id, plan, p.manga_id if p else None)
    db.event(con, "resolved", f"{len(plan.chapters)} chapters listed, {len(plan.wanted())} wanted"
             + (f", {len(plan.junk)} junk skipped" if plan.junk else ""), series_id)
    con.commit()
    out = Outcome(series_id, plan)
    if p:
        _set_library_entries(client, plan, p.manga_id)
    else:
        log.warning("%s: no usable source", series.title)
    if do_import:
        out.imported += import_series(con, series_id)
    if download and p:
        out.results = download_wanted(con, client, series_id, plan)
        if do_import:
            out.imported += import_series(con, series_id)
    return out


def refresh_series(con, client: Client, series_id: int, download: bool = True) -> Outcome:
    row = db.get_series(con, series_id)
    series = db.series_to_model(row)
    return add_series(con, client, series, download=download)


def _set_library_entries(client: Client, plan: Plan, primary_manga_id: int) -> None:
    """Only the primary entry stays in Suwayomi's library, so its own update
    fetches new chapters from one source, not five copies."""
    for m in plan.matches:
        try:
            client.set_in_library(m.manga_id, m.manga_id == primary_manga_id)
        except SuwayomiError as e:
            log.debug("could not set library flag on %d: %s", m.manga_id, e)


def download_wanted(con, client: Client, series_id: int, plan: Plan) -> dict:
    have_on_disk = {r["number"] for r in db.chapters(con, series_id) if r["status"] == "have"}
    wanted = [n for n in plan.wanted() if n not in have_on_disk]
    if not wanted:
        log.info("%s: nothing to download", plan.series.title)
        return {}
    results = downloader.download(client, plan, only=set(wanted))
    for n, r in results.items():
        m = plan.assignment.get(n)
        metrics.record_download(m.source.name if m else "?", r)
        if r != "ok":
            db.set_status(con, series_id, n, "failed")
    ok = sum(1 for r in results.values() if r == "ok")
    db.event(con, "downloaded", f"{ok} chapter(s) downloaded, {len(results) - ok} failed", series_id)
    con.commit()
    return results


def delete_series(con, client: Client, series_id: int, delete_library: bool = False) -> None:
    """Stop tracking. Optionally remove the library folder (hard links only;
    Suwayomi's staging files are never touched). The Suwayomi entries are
    taken out of its library so it stops auto-updating them."""
    row = db.get_series(con, series_id)
    title = row["title"]
    for s in db.sources(con, series_id):
        try:
            client.set_in_library(s["manga_id"], False)
        except SuwayomiError as e:
            log.warning("%s: could not unset library flag on %s entry: %s", title, s["source_name"], e)
    if delete_library:
        d = library.library_dir(title)
        removed = 0
        if os.path.isdir(d):
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    os.remove(p)
                    removed += 1
            try:
                os.rmdir(d)
            except OSError as e:
                log.warning("%s: library folder %s not removed: %s", title, d, e)
        log.info("%s: removed %d file(s) from %s", title, removed, d)
    db.delete_series(con, series_id)
    db.event(con, "deleted", f"{title} removed" + (" with library files" if delete_library else ""))
    con.commit()
    log.info("%s: no longer tracked", title)


# -- import ------------------------------------------------------------------

def series_staging_dirs(con, series_id: int) -> list[tuple[str, str]]:
    """[(source name, folder)] where Suwayomi has written this series."""
    out = []
    for s in db.sources(con, series_id):
        folder = s["folder"] or os.path.join(library.config.STAGING_ROOT, s["source_name"],
                                             library.safe_title(s["title"]))
        if os.path.isdir(folder):
            out.append((s["source_name"], folder))
    return out


def import_series(con, series_id: int) -> int:
    """Link every staged chapter into <library>/<title>/. Returns how many
    chapters were newly linked."""
    row = db.get_series(con, series_id)
    title = row["title"]
    known = {r["number"]: r for r in db.chapters(con, series_id)}
    linked = 0
    for source_name, folder in series_staging_dirs(con, series_id):
        found, unparsed = library.scan_series_dir(folder)
        if unparsed:
            log.debug("%s: %d file(s) in %s without a chapter number", title, len(unparsed), folder)
        for n, path in found.items():
            prev = known.get(n)
            if prev and prev["status"] == "junk":
                continue
            if prev and prev["library_path"] and os.path.exists(prev["library_path"]):
                continue
            dst = library.link_into_library(path, title, n)
            db.set_have(con, series_id, n, path, dst, source_name)
            log.debug("%s: linked ch %g <- %s", title, n, path)
            linked += 1
    if linked:
        db.event(con, "imported", f"{linked} chapter(s) linked into the library", series_id)
        log.info("%s: imported %d chapter(s) into %s", title, linked, library.library_dir(title))
        komga.scan()
    con.commit()
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


def apply_adopt(con, items: list[AdoptItem]) -> tuple[int, int]:
    """Register the identified folders. Folders of the same series (one per
    source) merge into one tracked series. Returns (series, chapters)."""
    by_ref: dict[str, list[AdoptItem]] = {}
    for it in items:
        if it.series:
            by_ref.setdefault(it.series.ref, []).append(it)
    n_chapters = 0
    for ref, group in by_ref.items():
        series = group[0].series
        series_id = db.upsert_series(con, series)
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
    return len(by_ref), n_chapters

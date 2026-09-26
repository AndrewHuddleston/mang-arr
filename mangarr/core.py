"""The operations, independent of how they are invoked (CLI, web, worker).

    add      identity -> resolve -> save -> download -> import
    refresh  re-resolve a tracked series, pick up new chapters
    import   link what is on disk into the library
    adopt    register everything Suwayomi already downloaded
"""
import os
from dataclasses import dataclass, field

from . import db, downloader, library, metadata
from .matching import query_score
from .model import Series
from .resolver import Plan, primary, resolve
from .suwayomi import Client, SuwayomiError


def _quiet(msg: str = "") -> None:
    pass


# -- add / refresh ------------------------------------------------------------

def add_series(con, client: Client, series: Series, log=_quiet, download: bool = True,
               do_import: bool = True) -> tuple[int, Plan, dict]:
    """Track a series: resolve it, remember the plan, fetch what is missing,
    link the results into the library. Returns (series id, plan, results)."""
    plan = resolve(client, series, log=log)
    series_id = db.upsert_series(con, series)
    p = primary(plan)
    db.save_plan(con, series_id, plan, p.manga_id if p else None)
    db.event(con, "added", f"tracking {series.title} ({len(plan.chapters)} chapters listed)", series_id)
    con.commit()
    if p:
        _set_library_entries(client, plan, p.manga_id)
    if do_import:
        import_series(con, series_id, log=log)
    results: dict = {}
    if download and p:
        results = download_wanted(con, client, series_id, plan, log=log)
        if do_import:
            import_series(con, series_id, log=log)
    return series_id, plan, results


def refresh_series(con, client: Client, series_id: int, log=_quiet, download: bool = True) -> tuple[Plan, dict]:
    row = db.get_series(con, series_id)
    series = db.series_to_model(row)
    return add_series(con, client, series, log=log, download=download)[1:]


def _set_library_entries(client: Client, plan: Plan, primary_manga_id: int) -> None:
    """Only the primary entry stays in Suwayomi's library, so its 12h update
    fetches new chapters from one source, not five copies."""
    for m in plan.matches:
        try:
            client.set_in_library(m.manga_id, m.manga_id == primary_manga_id)
        except SuwayomiError:
            pass


def download_wanted(con, client: Client, series_id: int, plan: Plan, log=_quiet) -> dict:
    have_on_disk = {r["number"] for r in db.chapters(con, series_id) if r["status"] == "have"}
    wanted = [n for n in plan.wanted() if n not in have_on_disk]
    if not wanted:
        return {}
    log(f"downloading {len(wanted)} chapter(s):")
    results = downloader.download(client, plan, log=log, only=set(wanted))
    for n, r in results.items():
        if r != "ok":
            db.set_status(con, series_id, n, "failed")
    ok = sum(1 for r in results.values() if r == "ok")
    db.event(con, "downloaded", f"{ok} chapter(s) downloaded, {len(results) - ok} failed", series_id)
    con.commit()
    return results


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


def import_series(con, series_id: int, log=_quiet) -> int:
    """Link every staged chapter into <library>/<title>/. Returns how many
    chapters are now in the library."""
    row = db.get_series(con, series_id)
    title = row["title"]
    known = {r["number"]: r for r in db.chapters(con, series_id)}
    linked = 0
    for source_name, folder in series_staging_dirs(con, series_id):
        found, _ = library.scan_series_dir(folder)
        for n, path in found.items():
            prev = known.get(n)
            if prev and prev["status"] == "junk":
                continue
            if prev and prev["library_path"] and os.path.exists(prev["library_path"]):
                continue
            dst = library.link_into_library(path, title, n)
            db.set_have(con, series_id, n, path, dst, source_name)
            linked += 1
    if linked:
        db.event(con, "imported", f"{linked} chapter(s) linked into the library", series_id)
        log(f"  imported {linked} chapter(s) into {library.library_dir(title)}")
    con.commit()
    return sum(1 for r in db.chapters(con, series_id) if r["status"] == "have")


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


def plan_adopt(client: Client, log=_quiet, only: str | None = None) -> list[AdoptItem]:
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
        log(f"  {src[:14]:<14} {name[:42]:<42} {len(numbers):>4} ch"
            f"{'  +' + str(len(unparsed)) + ' unparsed' if unparsed else ''}  -> {tag}")
    return items


def apply_adopt(con, items: list[AdoptItem], log=_quiet) -> tuple[int, int]:
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
        log(f"  adopted {series.title} <- {', '.join(f'{it.source} ({len(it.numbers)})' for it in group)}")
    con.commit()
    return len(by_ref), n_chapters

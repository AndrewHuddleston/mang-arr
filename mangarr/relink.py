"""Check library links: undo links made under a misread chapter number.

0.3.0 read the chapter number of a staged file from the wrong part of its
name when a scanlator or site prefix stood right before the chapter marker
("Humane Scans_Ch.17 - Maidens 101_ A Success_" was chapter 101, "Losers in
eXile_Ch.150 - Hana to Yume March 2020 Special" chapter 2020), and linked it
into the library under that number. library.parse_number reads them right
now; this check finds the links it made wrong and repairs them:

  - every 'have' chapter whose library file is a hard link (same inode) of
    its staging file is looked at, and the staging file's name is read again
    (find_misreads: names and lstat only, nothing opened or changed);
  - where it reads as another number, the library link is removed: only a
    regular file inside the series' library folder, the same file as the
    staging file (core.series_library_dir, core.library_file: the checks of
    a series delete). The staging file is never touched;
  - the chapter row goes back to what the plan says: wanted when a source
    lists that number, deleted when none does (2020, 101: no such chapter);
  - every series is imported again, so the file is linked under its real
    number (and a file the old reading took for a chapter already on disk,
    e.g. Dreaming Freedom's spin-offs 171.01-171.13, is linked at last);
    Komga is asked to scan; each repair is logged (WARNING) and recorded as
    an event.

It runs once on the first start after the upgrade (migration 17 schedules
TASK: db.maintenance_due) and is the System task "Check library links".
"""
import logging
import os
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from . import config, core, db, komga, library, limits
from .matching import oneline
from .suwayomi import Client, SuwayomiError

log = logging.getLogger(__name__)

TASK = "check-library-links"            # its name in the maintenance table (db.maintenance_due)


@dataclass(frozen=True)
class Link:
    """A chapter in the library as its row records it."""
    series_id: int | None
    title: str
    folder: str
    number: float
    library_path: str
    staging_path: str


@dataclass(frozen=True)
class Misread:
    """A link made under another number than its staging file's name reads
    as now (`actual`). `st` is the library file as checked: the same file
    (device and inode) as the staging file."""
    link: Link
    actual: float
    st: os.stat_result


def links(con) -> list[Link]:
    """Every chapter the library has with both its files recorded."""
    return [Link(*r) for r in con.execute(
        "SELECT c.series_id, s.title, s.folder, c.number, c.library_path, c.staging_path FROM chapter c"
        " JOIN series s ON s.id=c.series_id WHERE c.status='have' AND c.library_path IS NOT NULL"
        " AND c.staging_path IS NOT NULL ORDER BY s.title COLLATE NOCASE, c.number")]


def find_misreads(found: Iterable[Link]) -> list[Misread]:
    """The links whose staging file's name reads as another chapter number
    (library.parse_number) than the one they are linked as, and whose
    library file mang-arr made: a regular file inside the series' library
    folder, itself inside LIBRARY_ROOT (symlinks resolved), that is the same
    file (inode) as the staging file, a regular file inside STAGING_ROOT. A
    name that reads as no number (matched through Suwayomi's chapter names)
    says nothing and is left alone. Only names are read and files lstat'ed:
    nothing is opened or changed, so this is the planning part and can run
    against any tree."""
    out = []
    for link in found:
        actual = library.parse_number(os.path.basename(link.staging_path))
        if actual is None or actual == link.number:
            continue
        d = core.series_library_dir(link.title, link.folder)
        st = core.library_file(link.title, link.library_path, d) if d else None
        if st is None:
            continue
        staged = _staged(link.staging_path)
        if staged is None or (staged.st_dev, staged.st_ino) != (st.st_dev, st.st_ino):
            log.warning("%s: chapter %g's staging file %s reads as chapter %g, but %s is not a link of it (a copy, "
                        "or not made by mang-arr); left as it is: move it away by hand if it is that file",
                        link.title, link.number, link.staging_path, actual, link.library_path)
            continue
        out.append(Misread(link, actual, st))
    return out


def _staged(path: str) -> os.stat_result | None:
    """The lstat of a staging file: a regular file inside STAGING_ROOT
    (symlinks resolved), else None. Never opened."""
    if not library.is_within(path, config.STAGING_ROOT):
        return None
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st if stat.S_ISREG(st.st_mode) else None


def check_links(con, client: Client | None = None, progress: Callable[[str], None] | None = None,
                should_cancel: Callable[[], bool] | None = None) -> str:
    """Repair every misread link (see the module doc), then import every
    series. With a client (cancellable: limits.Cancelled), Suwayomi's own
    chapter lists decide which numbers a source lists; without one, or when
    it does not answer, the chapter row's own record does (_listed). The
    name is read as the import reads it, so what is removed here is what
    the import links under its real number. Returns a summary."""
    def say(msg: str) -> None:
        if progress:
            progress(msg)
    say("reading the library links again")
    found = find_misreads(links(con))
    by_series: dict[int, list[Misread]] = {}
    for m in found:
        by_series.setdefault(m.link.series_id, []).append(m)
    removed = linked = 0
    fixed: list[str] = []
    imported: set[int] = set()
    for sid, items in by_series.items():
        if should_cancel and should_cancel():
            raise limits.Cancelled()
        say(f"repairing {items[0].link.title}")
        listed = _listed_numbers(con, client, sid, items[0].link.title)
        done = []
        for m in items:
            if not _unlink(m):
                continue
            removed += 1
            done.append((m, _reset(con, sid, m, listed)))
        con.commit()
        if not done:
            continue
        linked += core.import_series(con, sid, client)
        imported.add(sid)
        for m, wanted in done:
            fixed.append(_record(con, sid, m, wanted))
        con.commit()
    rows = db.series_rows(con)
    for i, r in enumerate(rows, 1):
        if should_cancel and should_cancel():
            raise limits.Cancelled()
        if r["id"] in imported:
            continue
        say(f"importing series {i} of {len(rows)}: {oneline(r['title'], 80)}")
        linked += core.import_series(con, r["id"], client)
    if removed and not linked:
        komga.scan()                    # a link went and none came: import_series did not ask
    msg = (f"{len(fixed)} misread link(s) repaired" if fixed else "no misread links") + \
        f"; {linked} chapter(s) imported"
    log.info("check library links: %s", msg)
    return msg


def _listed_numbers(con, client: Client | None, series_id: int, title: str) -> set | None:
    """The chapter numbers the series' trusted source entries list in
    Suwayomi, or None without a client or when Suwayomi does not answer."""
    if client is None:
        return None
    listed: set = set()
    try:
        for s in db.sources(con, series_id):
            if not s["note"]:
                listed.update(c.number for c in client.chapters(s["manga_id"]))
    except SuwayomiError as e:
        log.warning("%s: cannot list its chapters in Suwayomi (%s); deciding from its chapter rows", title, e)
        return None
    return listed


def _unlink(m: Misread) -> bool:
    """Remove the misread library link, checked once more right before: the
    same regular file inside the series' library folder as when it was
    found, and still the same file as the staging file."""
    link = m.link
    d = core.series_library_dir(link.title, link.folder)
    st = core.library_file(link.title, link.library_path, d) if d else None
    staged = _staged(link.staging_path)
    same = st is not None and staged is not None and \
        (st.st_dev, st.st_ino) == (m.st.st_dev, m.st.st_ino) == (staged.st_dev, staged.st_ino)
    if not same:
        log.warning("%s: %s changed since it was checked; not removed", link.title, link.library_path)
        return False
    try:
        os.remove(link.library_path)
    except OSError as e:
        log.warning("%s: could not remove %s: %s", link.title, link.library_path, e)
        return False
    return True


def _reset(con, series_id: int, m: Misread, listed: set | None) -> bool:
    """The misread chapter's row, as the plan has it: wanted when a source
    lists its number, else deleted. Returns whether it is wanted."""
    link = m.link
    row = db.chapters_by_number(con, series_id, [link.number]).get(link.number)
    if row is None:
        return False
    if _listed(row, listed):
        con.execute("UPDATE chapter SET status='wanted', reason=?, staging_path=NULL, library_path=NULL, pages=NULL,"
                    " tries=0, next_try=NULL, failed_since=NULL, updated_at=? WHERE series_id=? AND number=?"
                    " AND status='have'",
                    (f"its file was chapter {m.actual:g} (a misread name); not downloaded yet - waiting for a "
                     "download pass", db.now(), series_id, link.number))
        return True
    con.execute("DELETE FROM chapter WHERE series_id=? AND number=? AND status='have'", (series_id, link.number))
    return False


def _listed(row, listed: set | None) -> bool:
    """Whether a source lists the chapter: Suwayomi's chapter lists when they
    could be read, else what the row keeps of a resolve (the source entry,
    title or date a listing gave it); a row the import alone made (2020:
    no source has such a chapter) has none of these. A listed chapter
    deleted by mistake comes back at the next resolve."""
    if listed is not None:
        return row["number"] in listed
    return bool(row["manga_id"] is not None or row["name"] or row["uploaded"])


def _record(con, series_id: int, m: Misread, wanted: bool) -> str:
    """The event (and WARNING) for one repair, saying what the import that
    followed made of the file."""
    link, n = m.link, m.actual
    row = db.chapters_by_number(con, series_id, [n]).get(n)
    if row is not None and row["status"] == "have" and row["staging_path"] == link.staging_path:
        outcome = f"re-imported as {n:g}"
    elif row is not None and row["status"] == "have":
        outcome = f"chapter {n:g} was in the library already"
    else:
        why = row["reason"] if row is not None and row["reason"] else None
        outcome = f"chapter {n:g} is not imported yet" + (f" ({why})" if why else "")
    msg = f"Chapter {link.number:g} was a misread file (it is chapter {n:g}); link removed, {outcome}"
    if wanted:
        msg += f"; chapter {link.number:g} is wanted again"
    db.event(con, "relinked", msg, series_id)
    log.warning("%s: %s (%s)", link.title, msg, link.staging_path)
    return msg


def run_if_due(client: Client | None = None, progress: Callable[[str], None] | None = None,
               should_cancel: Callable[[], bool] | None = None) -> str | None:
    """Run the check when an upgrade scheduled it and it has not run to the
    end yet; None when it is not due. Marked done once it has."""
    with db.connect() as con:
        if TASK not in db.maintenance_due(con):
            return None
        log.info("checking the library links once after the upgrade (misread chapter numbers)")
        msg = check_links(con, client, progress, should_cancel)
        db.maintenance_done(con, TASK)
    return msg

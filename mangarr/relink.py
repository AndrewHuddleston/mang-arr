"""Check library links: undo links made under a misread chapter number, and
set back chapters whose library file is gone.

0.3.0 read the chapter number of a staged file from the wrong part of its
name when a scanlator or site prefix stood right before the chapter marker
("Humane Scans_Ch.17 - Maidens 101_ A Success_" was chapter 101, "Losers in
eXile_Ch.150 - Hana to Yume March 2020 Special" chapter 2020), and linked it
into the library under that number. library.parse_number reads them right
now; this check finds the links 0.3.0 made wrong and repairs them. A link is
only touched when three readings agree that it is one of those:

  - 0.3.0's reading of the staging file's name (_parse_030, kept here only
    for this) is the number it is linked as: the link came from that
    reading, not from Suwayomi's chapter names or a later version;
  - library.parse_number reads the name as another number now;
  - Suwayomi's own number for that file (the chapter list of the source
    entry whose download folder holds it, matched by file name) is that new
    number. When Suwayomi cannot be asked, nothing of that series is
    changed and the check stays due: it runs again later (after the next
    refresh pass, before the next worker cycle). When Suwayomi numbers the
    file otherwise, or does not list it, the link is left as it is (logged);

and its library file is one mang-arr made: a regular file inside the series'
library folder, the same file (inode) as the staging file (find_misreads:
names and lstat only, nothing opened or changed). Then:

  - a database backup is written before the first change ("before library
    link repair"); when it cannot be, nothing is changed (RepairAborted);
  - the library link is removed, checked once more right before (the
    checks of a series delete: core.series_library_dir, core.library_file).
    The staging file is never touched;
  - the chapter row goes back to what the plan says: wanted when a source
    lists that number, deleted when none does (2020, 101: no such chapter);
  - the series is imported again, so the file is linked under its real
    number (and a file the old reading took for a chapter already on disk,
    e.g. Dreaming Freedom's spin-offs 171.01-171.13, is linked at last);
  - a 'have' chapter whose library file is gone and that the import could
    not link again (a wrong file removed by hand, a restored backup of the
    database from before the repair, a crash between a removal and its
    commit) goes back to what the plan says the same way (_heal);
  - each change is logged (WARNING) and recorded as an event; Komga is asked
    to scan, and once more later when it does not answer.

It runs once on the first start after the upgrade (migration 17 schedules
TASK: db.maintenance_due) and is the System task "Check library links".
"""
import logging
import os
import re
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from . import backup, config, core, db, komga, library, limits, serieslock
from .matching import oneline
from .suwayomi import Client, SuwayomiError

log = logging.getLogger(__name__)

TASK = "check-library-links"            # its name in the maintenance table (db.maintenance_due)
BACKUP_REASON = "before library link repair"


class RepairAborted(RuntimeError):
    """The backup before the first change could not be written: nothing was
    changed, and the check stays due."""


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
    """A link 0.3.0 made from its reading of the staging file's name, which
    reads as another number now (`actual`). `st` is the library file as
    checked: the same file (device and inode) as the staging file."""
    link: Link
    actual: float
    st: os.stat_result


@dataclass
class Result:
    """What one check did. `deferred`: misreads left for a later run because
    Suwayomi could not be asked (the check stays due); `left`: ones Suwayomi
    numbers otherwise or does not list (left as they are for good)."""
    repaired: list = field(default_factory=list)        # event messages
    healed: list = field(default_factory=list)          # event messages
    left: int = 0
    deferred: int = 0
    imported: int = 0

    @property
    def message(self) -> str:
        parts = [f"{len(self.repaired)} misread link(s) repaired" if self.repaired else "no misread links"]
        if self.left:
            parts.append(f"{self.left} left as they are (Suwayomi does not confirm the new number)")
        if self.deferred:
            parts.append(f"{self.deferred} left for a later run (Suwayomi did not answer)")
        if self.healed:
            parts.append(f"{len(self.healed)} chapter(s) whose library file was gone set back")
        parts.append(f"{self.imported} chapter(s) imported")
        return "; ".join(parts)


# -- 0.3.0's reading of a name (reference only) ---------------------------------
# library.parse_number as 0.3.0 had it: the first chapter keyword anywhere in
# the name, scanlator prefix included, then the last number. Kept unchanged
# so the check can tell a link 0.3.0 made from its reading of the name.
_OLD_SEASON = re.compile(r"(?<![A-Za-z])S(\d+)\s*[-–]\s*(?:Episode|Ep\.?|Chapter|Ch\.?)\s*(\d+(?:\.\d+)?)", re.I)
_OLD_KEYWORD = re.compile(
    r"(?:\b(?:chapter|chap|ch|episode|ep|page|day|mission|room|act|step|bullet|part|lesson|round|file|case|"
    r"night|stage)\b\.?|#)\s*(\d+(?:\.\d+)?)", re.I)
_OLD_VOLUME_ONLY = re.compile(r"(?<![A-Za-z])vol(?:ume)?\.?\s*\d+", re.I)
_OLD_LASTNUM = re.compile(r"(?<![\d])(\d+(?:\.\d+)?)\D*$")


def _parse_030(filename: str) -> float | None:
    """The chapter number 0.3.0 read in a file name (None: none)."""
    stem = os.path.splitext(os.path.basename(filename))[0][:library.MAX_PARSE]
    m = _OLD_KEYWORD.search(stem)
    season = _OLD_SEASON.search(stem)
    if m and not (season and season.start() <= m.start()):
        return float(m.group(1))
    if season or _OLD_VOLUME_ONLY.search(stem):
        return None
    m = _OLD_LASTNUM.search(stem)
    return float(m.group(1)) if m else None


# -- planning: names and lstat only -------------------------------------------

def links(con) -> list[Link]:
    """Every chapter the library has with both its files recorded."""
    return [Link(*r) for r in con.execute(
        "SELECT c.series_id, s.title, s.folder, c.number, c.library_path, c.staging_path FROM chapter c"
        " JOIN series s ON s.id=c.series_id WHERE c.status='have' AND c.library_path IS NOT NULL"
        " AND c.staging_path IS NOT NULL ORDER BY s.title COLLATE NOCASE, c.number")]


def find_misreads(found: Iterable[Link]) -> list[Misread]:
    """The links 0.3.0 made from its reading of the staging file's name
    (_parse_030 gives the number they are linked as) that
    library.parse_number reads as another number now, and whose library
    file mang-arr made: a regular file inside the series' library folder,
    itself inside LIBRARY_ROOT (symlinks resolved), that is the same file
    (inode) as the staging file, a regular file inside STAGING_ROOT. A link
    Suwayomi's chapter names made (0.3.0 read the name as no number, or as
    another one) says nothing and is left alone. Only names are read and
    files lstat'ed: nothing is opened or changed, so this is the planning
    part and can run against any tree. Suwayomi confirms each one before
    anything is changed (_confirm)."""
    out = []
    for link in found:
        name = os.path.basename(link.staging_path)
        if _parse_030(name) != link.number:
            continue
        actual = library.parse_number(name)
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


# -- Suwayomi's word -----------------------------------------------------------

class Lists:
    """Suwayomi's chapter lists of one series' source entries, each asked at
    most once. A call raises SuwayomiError (or limits.Cancelled) when
    Suwayomi does not answer; without a client, it never answers."""

    def __init__(self, con, client: Client | None, series_id: int):
        self.client, self.sources, self._lists = client, db.sources(con, series_id), {}

    def chapters(self, manga_id: int) -> list:
        if self.client is None:
            raise SuwayomiError("no Suwayomi client")
        if manga_id not in self._lists:
            self._lists[manga_id] = list(self.client.chapters(manga_id))
        return self._lists[manga_id]

    def listed(self) -> set:
        """The chapter numbers the series' trusted source entries list."""
        out: set = set()
        for s in self.sources:
            if not s["note"]:
                out.update(c.number for c in self.chapters(s["manga_id"]))
        return out

    def number_of(self, staging_path: str) -> float | None:
        """Suwayomi's number for a staged file: from the chapter list of the
        source entry whose download folder holds it, the chapter whose file
        name ('<scanlator>_<name>', made safe as Suwayomi does) is this one
        (number_in). None when no source entry holds the folder or no
        chapter has that name."""
        folder = os.path.normpath(os.path.dirname(staging_path))
        for s in self.sources:
            if _same_folder(core.source_folder(s), folder):
                return number_in(self.chapters(s["manga_id"]), os.path.basename(staging_path))
        return None


def _same_folder(a: str, b: str) -> bool:
    """The same folder: the same path, or two paths to one folder (stat only)."""
    if os.path.normpath(a) == os.path.normpath(b):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _loose(stem: str) -> str:
    """A file stem with only its letters, digits and dots, case folded: what
    survives any sanitising of a name."""
    return "".join(ch for ch in stem.casefold() if ch.isalnum() or ch == ".")


def number_in(chapters, filename: str) -> float | None:
    """The number of the chapter (of one Suwayomi chapter list) that
    `filename` is the download of: by its name as Suwayomi writes it
    (library.suwayomi_name_map's key), else by the same letters, digits and
    dots. None when no chapter matches, or the matches disagree."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    exact: set = set()
    loose: set = set()
    for c in chapters:
        written = f"{c.scanlator}_{c.name}" if c.scanlator else (c.name or "")
        if library.safe_title(written).lower() == stem.lower():
            exact.add(c.number)
        if _loose(written) == _loose(stem):
            loose.add(c.number)
    for found in (exact, loose):
        if found:
            return found.pop() if len(found) == 1 else None
    return None


def _confirm(title: str, items: list[Misread], lists: Lists, result: Result) -> tuple[list[Misread], set | None]:
    """The misreads Suwayomi confirms (its number for the file is the new
    reading), and the numbers the series' sources list. Those it numbers
    otherwise or does not list are left (counted in result.left, logged);
    when it cannot be asked, all of them wait for a later run
    (result.deferred): ([], None)."""
    try:
        numbers = [lists.number_of(m.link.staging_path) for m in items]
        listed = lists.listed()
    except SuwayomiError as e:
        result.deferred += len(items)
        log.warning("%s: %d link(s) look misread, but Suwayomi cannot be asked for its chapter numbers (%s); "
                    "nothing changed, the check runs again later", title, len(items), e)
        return [], None
    confirmed = []
    for m, n in zip(items, numbers, strict=True):
        if n == m.actual:
            confirmed.append(m)
            continue
        result.left += 1
        said = f"numbers it {n:g}" if n is not None else "does not list it"
        log.warning("%s: chapter %g's file %s reads as chapter %g now, but Suwayomi %s; left as it is", title,
                    m.link.number, oneline(m.link.staging_path, 200), m.actual, said)
    return confirmed, listed


# -- the check -----------------------------------------------------------------

def check_links(con, client: Client | None = None, progress: Callable[[str], None] | None = None,
                should_cancel: Callable[[], bool] | None = None) -> Result:
    """Repair every confirmed misread link and set back every 'have'
    chapter whose library file is gone (see the module doc), importing
    every series. With a client (cancellable: limits.Cancelled), Suwayomi's
    chapter lists confirm each misread and decide which numbers a source
    lists; without one, or when it does not answer, no link is changed
    (Result.deferred) and a gone chapter's row decides by its own record
    (_listed). Raises RepairAborted when the backup before the first change
    fails."""
    def say(msg: str) -> None:
        if progress:
            progress(msg)
    say("reading the library links again")
    candidates: dict[int, list[Misread]] = {}
    for m in find_misreads(links(con)):
        candidates.setdefault(m.link.series_id, []).append(m)
    result = Result()
    asked: dict[int, tuple[Lists, list[Misread], set | None]] = {}
    for sid, items in candidates.items():
        if should_cancel and should_cancel():
            raise limits.Cancelled()
        say(f"asking Suwayomi about {oneline(items[0].link.title, 80)}")
        lists = Lists(con, client, sid)
        asked[sid] = (lists, *_confirm(items[0].link.title, items, lists, result))
    rows = db.series_rows(con)
    guard = _BackupFirst()
    if any(confirmed for _, confirmed, _ in asked.values()) or \
            any(_gone(con, r["id"], r["title"], r["folder"], quiet=True) for r in rows):
        guard.before_change()           # before anything changes, the imports too
    for i, r in enumerate(rows, 1):
        if should_cancel and should_cancel():
            raise limits.Cancelled()
        sid = r["id"]
        lists, confirmed, listed = asked.get(sid) or (Lists(con, client, sid), [], None)
        if con.in_transaction:
            con.commit()
        with serieslock.hold(sid, should_cancel=should_cancel):     # its files are not renamed meanwhile
            _check_series(con, client, r, i, len(rows), lists, confirmed, listed, guard, result, say)
    if (result.repaired or result.healed) and not result.imported:
        komga.scan_retrying()           # a link went and none came: import_series did not ask
    log.info("check library links: %s", result.message)
    return result


def _check_series(con, client, r, i: int, of: int, lists: "Lists", confirmed: list, listed: set | None,
                  guard: "_BackupFirst", result: "Result", say: Callable[[str], None]) -> None:
    """check_links for one series, its lock held."""
    sid, title = r["id"], r["title"]
    row = db.get_series(con, sid)
    if row is None:
        return                          # deleted meanwhile
    folder = row["folder"]              # as it is now: a rename may have ended just before the lock was free
    done = []
    if confirmed:
        say(f"repairing {oneline(title, 80)}")
        for m in confirmed:
            guard.before_change()
            if _unlink(m):
                done.append((m, _reset(con, sid, m, listed)))
        con.commit()
    say(f"importing series {i} of {of}: {oneline(title, 80)}")
    result.imported += core.import_series(con, sid, client)
    for m, wanted in done:
        result.repaired.append(_record(con, sid, m, wanted))
    con.commit()
    gone = _gone(con, sid, title, folder)
    if gone:
        if listed is None:
            listed = _listed_numbers(lists, title)
        for row in gone:
            guard.before_change()
            result.healed.append(_heal(con, sid, title, row, listed))
        con.commit()


class _BackupFirst:
    """Writes the database backup once, right before the check's first
    change; a check that changes nothing writes none."""

    def __init__(self):
        self.path: str | None = None

    def before_change(self) -> None:
        if self.path is not None:
            return
        try:
            self.path = backup.create(BACKUP_REASON)
        except backup.FAILURES as e:
            raise RepairAborted(f"library link repair not started: the database backup before it failed ({e}); "
                                "nothing changed, the check runs again later") from e


def _listed_numbers(lists: Lists, title: str) -> set | None:
    """The numbers the series' sources list, or None when Suwayomi cannot be
    asked (the rows then decide: _listed)."""
    try:
        return lists.listed()
    except SuwayomiError as e:
        log.info("%s: cannot list its chapters in Suwayomi (%s); deciding from its chapter rows", title, e)
        return None


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


def _set_back(con, series_id: int, number: float, listed: set | None, reason: str) -> bool:
    """A 'have' chapter row, as the plan has it: wanted (with `reason`) when
    a source lists its number, else deleted. Returns whether it is wanted."""
    row = db.chapters_by_number(con, series_id, [number]).get(number)
    if row is None or row["status"] != "have":
        return False
    if _listed(row, listed):
        con.execute("UPDATE chapter SET status='wanted', reason=?, staging_path=NULL, library_path=NULL,"
                    " file_title=NULL, pages=NULL, tries=0, next_try=NULL, failed_since=NULL, updated_at=?"
                    " WHERE series_id=? AND number=? AND status='have'", (reason, db.now(), series_id, number))
        return True
    con.execute("DELETE FROM chapter WHERE series_id=? AND number=? AND status='have'", (series_id, number))
    return False


def _reset(con, series_id: int, m: Misread, listed: set | None) -> bool:
    """The misread chapter's row, as the plan has it (_set_back)."""
    return _set_back(con, series_id, m.link.number, listed,
                     f"its file was chapter {m.actual:g} (a misread name); not downloaded yet - waiting for a "
                     "download pass")


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
    followed made of the file, and of the number it was linked as (its own
    file linked, when another file is that chapter; else wanted again, or
    dropped)."""
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
    was = db.chapters_by_number(con, series_id, [link.number]).get(link.number)
    if was is not None and was["status"] == "have" and was["staging_path"]:
        msg += f"; chapter {link.number:g} is linked from {os.path.basename(was['staging_path'])} now"
    elif wanted and was is not None:
        msg += f"; chapter {link.number:g} is wanted again"
    db.event(con, "relinked", msg, series_id)
    log.warning("%s: %s (%s)", link.title, msg, link.staging_path)
    return msg


def _gone(con, series_id: int, title: str, folder, quiet: bool = False) -> list:
    """The 'have' rows of a series whose library file is gone (its path, in
    the series' library folder, is not there): after the import had its
    chance to link them again, those it could not. Only while that folder
    itself is there: a library that is not mounted, or a folder removed as
    a whole, is not taken for every file gone (logged unless quiet)."""
    rows = [r for r in db.chapters(con, series_id) if r["status"] == "have" and r["library_path"]]
    if not rows:
        return []
    d = core.series_library_dir(title, folder) if db.valid_folder(folder) else None
    if d is None:
        return []
    out = []
    for r in rows:
        p = r["library_path"]
        if not library.is_within(p, d):
            continue                    # not a path mang-arr makes (a restored backup): not judged here
        try:
            os.lstat(p)
        except FileNotFoundError:
            out.append(r)
        except OSError:
            continue
    if not out:
        return []
    try:
        folder_there = stat.S_ISDIR(os.lstat(d).st_mode)
    except OSError:
        folder_there = False
    if not folder_there:
        if not quiet:
                log.warning("%s: its library folder %s is not there, so %d chapter(s) marked in the library have no "
                        "file; left as they are (is the library mounted?)", title, d, len(out))
        return []
    return out


def _heal(con, series_id: int, title: str, row, listed: set | None) -> str:
    """Set back a 'have' chapter whose library file is gone (_gone): wanted
    when a source lists it, else its row is deleted (a bogus row, e.g. 2020
    once its wrong file was removed by hand). Recorded and logged."""
    n, name = row["number"], os.path.basename(row["library_path"])
    wanted = _set_back(con, series_id, n, listed, f"its library file {name} was gone; not downloaded yet - waiting "
                                                  "for a download pass")
    msg = f"Chapter {n:g}'s library file {name} is gone and no downloaded file could be linked as chapter {n:g}; " + \
        ("wanted again" if wanted else "removed: no source lists it")
    db.event(con, "gone", msg, series_id)
    log.warning("%s: %s", title, msg)
    return msg


def preview(con, client: Client | None) -> tuple[list[str], int]:
    """What check_links would do, changing nothing: a line for every link
    that looks misread (and what Suwayomi says to it) and for every 'have'
    chapter whose library file is gone now; and how many links it would
    remove."""
    lines: list[str] = []
    would = 0
    by_series: dict[int, list[Misread]] = {}
    for m in find_misreads(links(con)):
        by_series.setdefault(m.link.series_id, []).append(m)
    for sid, items in by_series.items():
        lists = Lists(con, client, sid)
        try:
            numbers: list = [lists.number_of(m.link.staging_path) for m in items]
        except SuwayomiError as e:
            numbers = [e] * len(items)
        for m, n in zip(items, numbers, strict=True):
            link = m.link
            head = f"{link.title}: chapter {link.number:g} is {m.actual:g}: "
            if isinstance(n, SuwayomiError):
                lines.append(head + f"Suwayomi cannot be asked ({n}); would wait for it")
            elif n == m.actual:
                would += 1
                lines.append(head + f"would remove {link.library_path} (a link of {link.staging_path})")
            else:
                said = f"Suwayomi numbers it {n:g}" if n is not None else "Suwayomi does not list it"
                lines.append(head + f"{said}; would leave it as it is")
    for r in db.series_rows(con):
        for row in _gone(con, r["id"], r["title"], r["folder"]):
            lines.append(f"{r['title']}: chapter {row['number']:g}'s library file {row['library_path']} is gone: "
                         "would be set back unless the import links it again")
    return lines, would


def finish(con, result: Result) -> None:
    """Done: not due again, unless misreads wait for Suwayomi to answer
    (then due until a run gets through)."""
    if result.deferred:
        db.maintenance_due_again(con, TASK)
    else:
        db.maintenance_done(con, TASK)


def run_if_due(client: Client | None = None, progress: Callable[[str], None] | None = None,
               should_cancel: Callable[[], bool] | None = None) -> str | None:
    """Run the check when an upgrade (or a run Suwayomi did not answer)
    scheduled it and it has not run to the end yet; None when it is not
    due. Marked done once it has (finish)."""
    with db.connect() as con:
        if TASK not in db.maintenance_due(con):
            return None
        log.info("checking the library links (misread chapter numbers, library files gone)")
        result = check_links(con, client, progress, should_cancel)
        finish(con, result)
    return result.message

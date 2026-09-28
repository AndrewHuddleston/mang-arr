"""Renaming a series' library files to the naming formats (Settings ->
Media Management; see naming.py).

Nothing is ever renamed on its own: not by an upgrade, not by changing a
format, not when a source edits a title. Files are renamed only when the
user runs a preview (plan) and then the rename of it (apply).

plan() is the preview: for every chapter the library has, the file's
current path and the one the formats give, and what would stop or spoil the
rename:
- two chapters that would get one name, or a new name that another file in
  the folder already has (that chapter is left as it is, never overwritten);
- names that would sort out of order by plain file name (012.5 before 012);
- what becomes of reading progress in Komga.
It renames nothing, opens nothing for writing and writes nothing to the
database. It reads the database, the series' library folder (which names
are taken) and, when there is something to rename, Komga.

Titles: by default a chapter keeps the title its current file name has
(chapter.file_title, stored with every library path), so a source that
edits a title, or a title that became known after the file was linked,
renames nothing (user decision 1); use_latest_titles takes the source's
latest chapter name instead (a chapter with no name on record keeps its
file's title). A file whose title is not on record (file_title NULL: a name
the migration could not read) is read with the chosen format, else the
default one, and a reading only counts when rendering it again gives the
name back; a name neither made is left as it is, with its reason, unless
use_latest_titles gives it the source's title.

apply() carries a preview out, as a job of the job runner (job, submit):
1. Reading progress: when Komga would not keep it (no Komga, file hashing
   off, books not hashed yet ...), the rename must have been confirmed
   ("reading progress in your reader app may be lost"), else nothing starts.
2. A database backup is written first ("before rename").
3. Series by series, under the series' lock (serieslock: no import,
   download or delete of it meanwhile), the preview is made again and only
   what it still says, and the given preview said, is done.
4. Every step is written to the journal (rename_log: old path, new path,
   series, chapter) and committed to disk before the first file is touched.
5. A file is renamed with one rename(2) inside its folder, never over
   another file, never by link and unlink, and only inside the library:
   staging is never opened, a hard link stays the same file with another
   name. Right after it, chapter.library_path and file_title are updated
   together with the journal row. The series folder is renamed last, then
   series.folder and its chapters' paths.
6. The series is verified (every chapter's file is where the database says)
   and Komga is asked for one scan at the end.

repair() runs at start-up: a rename that was interrupted (a kill, a power
cut) is settled from its journal and the disk. A step whose rename happened
gets its database update; a file caught under a temporary name gets its new
name (or its old one back); a step that had not started is marked so. No
new rename is started by a repair. undo() reverses a finished rename the
same careful way, as a rename of its own.

Converted copies (e-reader conversion, 0.5.0) are renamed together with
their library files: converted_steps is the hook, and the journal has the
kind "converted" for them. Until then there are none.
"""
import ctypes
import dataclasses
import errno
import json
import logging
import os
import stat
from collections.abc import Callable

from . import backup, config, core, db, komga, library, naming, serieslock, settings

log = logging.getLogger(__name__)


NOT_SELECTED = "not selected"
IN_STAGING = "its library file is inside the staging folder, which is never renamed; left as it is"


def plan(con, series_id: int, formats=None, use_latest_titles: bool = False, only=None,
         rename_folder: bool = True, check_komga: bool = True) -> dict:
    """The rename preview for one series, as a dict ready for JSON:

    folder      {"old", "new", "changed", "blocked"} for the series folder;
                blocked says why it cannot be renamed (None when it can)
    chapters    one per chapter the library has, by number: {"number",
                "old_path", "new_path", "old_name", "new_name", "title",
                "title_from" ("file" or "source"), "changed", "skip",
                "old_title", "file_title"}; skip
                says why the file is left as it is (None when it is not);
                old_title and file_title are chapter.file_title as it is
                and as it is after the rename.
                new_name is the name the formats give (None when there is
                none to give); new_path is where the file is once the
                series is organized: for a skipped chapter, under its
                current name, in the renamed folder when the folder is
                renamed (a file outside the folder stays where it is)
    renames     how many files get a new name (changed and not skipped)
    collisions  [{"name", "numbers", "message"}]: names two chapters, or a
                chapter and another file, would share
    warnings    [{"kind", "message"}]: "sort" (names that sort out of order
                by plain file name) or "title" (current names neither the
                default nor the chosen format made: left as they are)
    komga       {"state", "message", "needs_confirmation"}, or None when
                nothing is renamed; needs_confirmation: reading progress may
                be lost, so the rename must be confirmed first

    formats     the naming settings the preview was made with, and
    only, rename_folder: the choices; apply() makes the preview again from
                these

    formats: naming.Options or a dict of the naming settings (None: the
    defaults). only: the chapter numbers to rename (None: all); the others
    keep their names, which stay taken. rename_folder False leaves the
    series folder as it is. check_komga False leaves "komga" None (apply
    asks Komga before it takes the series' lock). Raises naming.FormatError
    when the formats cannot be used and LookupError for an unknown series."""
    options = naming.as_options(formats)
    errors = naming.check_options(options)
    if errors:
        raise naming.FormatError(errors)
    row = db.get_series(con, series_id)
    if row is None:
        raise LookupError(f"no series with id {series_id}")
    series = naming.SeriesNames.from_row(row)
    only = None if only is None else {float(n) for n in only}
    out = {"series_id": series_id, "title": row["title"], "use_latest_titles": bool(use_latest_titles),
           "formats": dataclasses.asdict(options), "only": None if only is None else sorted(only),
           "rename_folder": bool(rename_folder),
           "folder": None, "chapters": [], "renames": 0, "collisions": [], "warnings": [], "komga": None}
    old_folder = row["folder"]
    if not db.valid_folder(old_folder):
        out["folder"] = {"old": old_folder, "new": None, "changed": False,
                         "blocked": "this series has no usable library folder"}
        return out
    new_folder = _new_folder(con, row, series, options) if rename_folder else old_folder
    old_dir = library.library_dir(old_folder)
    folder = {"old": old_folder, "new": new_folder, "changed": new_folder != old_folder, "blocked": None}
    there = _in_library(new_folder) if folder["changed"] else None
    if there is not None and naming.folder_key(there) != naming.folder_key(old_folder):
        folder["blocked"] = f"a folder named {there!r} is already in the library"
        out["collisions"].append({"name": new_folder, "numbers": [], "message": _sentence(folder["blocked"])})
    out["folder"] = folder
    new_dir = old_dir if folder["blocked"] else library.library_dir(new_folder)

    listing = _listing(old_dir)
    unread, on_disk = [], {}
    for r in con.execute("SELECT number, name, library_path, file_title FROM chapter WHERE series_id=?"
                         " AND status='have' ORDER BY number", (series_id,)).fetchall():
        item, state = _item(r, series, options, old_dir, new_dir, listing, use_latest_titles)
        if only is not None and r["number"] not in only and item["changed"] and not item["skip"]:
            _skip(item, NOT_SELECTED)
        out["chapters"].append(item)
        if state:
            on_disk[item["old_name"]] = item
        if state == "unread":
            unread.append(item["number"])
    items = out["chapters"]
    out["collisions"] += _same_name(items) + _taken(items, listing or {}, on_disk)
    out["renames"] = sum(1 for it in items if it["changed"] and not it["skip"])
    out["warnings"] = _sort_warnings(items)
    if unread:
        which = f"chapter {', '.join(_num(n) for n in unread[:5])}{' ...' if len(unread) > 5 else ''}"
        out["warnings"].append({"kind": "title", "message": (
            f"{len(unread)} current file name(s) ({which}) were made by neither the default nor the chosen "
            "format, so the titles in them cannot be told from the rest of the name: those files are left as "
            "they are." + (" Use the latest titles from sources to rename them with the sources' titles."
                           if any(it["skip"] == UNREAD for it in items) else ""))})
    folder_moves = folder["changed"] and not folder["blocked"]
    if check_komga and (out["renames"] or folder_moves):
        out["komga"] = komga_check(old_folder, folder_renamed=folder_moves)
    return out


def _num(n: float) -> str:
    return f"{n:g}"


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _new_folder(con, row, series: naming.SeriesNames, options: naming.Options) -> str:
    """The series' folder under the formats: the one it has while that is
    still a name the folder format gives it (so a folder is never renamed
    only because a plainer name became free), else a new unique one."""
    if naming.folder_matches(row["folder"], series, row["ref"], options):
        return row["folder"]
    taken = {r[0] for r in con.execute("SELECT folder FROM series WHERE folder IS NOT NULL AND id != ?",
                                       (row["id"],))}
    return naming.render(series, None, options, taken=taken, suffix=row["ref"])


def _in_library(folder: str) -> str | None:
    """The name of whatever is in the library root under this folder name,
    ignoring case and normalisation (as some mounts and every SMB client
    do), or None."""
    key = naming.folder_key(folder)
    try:
        with os.scandir(config.LIBRARY_ROOT) as it:
            return next((e.name for e in it if naming.folder_key(e.name) == key), None)
    except OSError:
        return folder if os.path.lexists(library.library_dir(folder)) else None


def _listing(path: str) -> dict[str, bool] | None:
    """{name: is a regular file} for what is in a series' library folder, or
    None when it is missing, a symlink, or not inside the library (it is
    not listed then)."""
    if os.path.islink(path) or not os.path.isdir(path) or not library.is_within(path, config.LIBRARY_ROOT):
        return None
    out = {}
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    out[e.name] = e.is_file(follow_symlinks=False)
                except OSError:
                    out[e.name] = False
    except OSError as e:
        log.warning("cannot list %s for a rename preview: %s", path, e)
        return None
    return out


UNREAD = ("its name was made by neither the default nor the chosen format, so the title in it is not known; "
          "left as it is (the latest titles from sources would rename it)")
UNREAD_NO_SOURCE = ("its name was made by neither the default nor the chosen format, and the source has no name "
                    "for this chapter; left as it is")


def _item(r, series: naming.SeriesNames, options: naming.Options, old_dir: str, new_dir: str,
          listing: dict[str, bool] | None, use_latest_titles: bool) -> tuple[dict, str | None]:
    """One chapter of the preview, and what its file is: "read" (a regular
    file in the series folder, its name understood), "unread" (one whose
    name neither format made: left as it is) or None (no file there to
    rename)."""
    n, path = r["number"], r["library_path"]
    item = {"number": n, "old_path": path, "new_path": None, "old_name": None, "new_name": None,
            "title": None, "title_from": None, "changed": False, "skip": None,
            "old_title": r["file_title"], "file_title": r["file_title"]}
    if not path:
        item["skip"] = "no library file is recorded for this chapter"
        return item, None
    name = item["old_name"] = os.path.basename(path)
    in_folder = os.path.dirname(os.path.normpath(path)) == os.path.normpath(old_dir)
    item["new_path"] = os.path.join(new_dir, name) if in_folder else path      # a skipped file moves with its folder
    if not in_folder:
        item["skip"] = "the file is not in the series' library folder; left as it is"
    elif library.is_within(path, config.STAGING_ROOT):
        item["skip"] = IN_STAGING
    elif listing is None:
        item["skip"] = "the series' library folder is missing, or not a real folder inside the library"
    elif name not in listing:
        item["skip"] = "the file is missing"
    elif not listing[name]:
        item["skip"] = "not a regular file (a symlink or a folder); left as it is"
    if item["skip"]:
        return item, None
    found = _chapter_info(n, name, r["name"], r["file_title"], series, options, use_latest_titles)
    if found is None:
        item["skip"] = UNREAD_NO_SOURCE if r["name"] is None else UNREAD
        return item, "unread"
    chap, item["title_from"] = found
    new = naming.render(series, chap, options)
    item.update(new_name=new, new_path=os.path.join(new_dir, new), changed=new != name,
                title=naming.title_value(chap, options) or None, file_title=naming.stored_title(chap, options))
    return item, "read"


def _chapter_info(number: float, file_name: str, source_name: str | None, stored: str | None,
                  series: naming.SeriesNames, options: naming.Options,
                  use_latest_titles: bool) -> tuple[naming.ChapterInfo, str] | None:
    """(what to render, where its title comes from), or None when the
    title in the current name cannot be known.

    The title on record for the file (chapter.file_title) is the one; only
    without one is the name read.

    The name is read with the chosen format and with the default one (every
    name so far); a reading counts only when rendering its title with that
    format gives the name back. A name the chosen format made keeps the
    chosen format's reading, so it renders to itself again. When the
    source's name gives the title that was read (with the format that made
    the name), the source's name is rendered: the same title, but a new
    colon replacement or title length applies to it."""
    if use_latest_titles and source_name is not None:
        return naming.ChapterInfo(number, source_name), "source"
    if stored is not None:
        return naming.ChapterInfo(number, file_title=stored), "file"
    readings = {}
    for made_with in dict.fromkeys((options, naming.DEFAULTS)):
        title = naming.read_title(file_name, series, number, made_with)
        if title is not None:
            readings[made_with] = title
    if not readings:
        return None
    title = next(iter(readings.values()))              # the chosen format's reading, when it has one
    if any(t == title and naming.chapter_title(number, source_name, m) == title for m, t in readings.items()):
        return naming.ChapterInfo(number, source_name), "file"          # the title as the source spells it
    return naming.ChapterInfo(number, file_title=title), "file"


def _skip(it: dict, reason: str) -> None:
    """Leave a chapter that has a new name as it is: it keeps its current
    name (in the renamed folder, when the folder is renamed)."""
    it["skip"] = reason
    it["new_path"] = os.path.join(os.path.dirname(it["new_path"]), it["old_name"])


def _same_name(items: list[dict]) -> list[dict]:
    """Chapters that would get one name (ignoring case and normalisation,
    as some mounts do). A chapter that already has the name keeps it; the
    others are skipped."""
    groups: dict[str, list[dict]] = {}
    for it in items:
        if not it["skip"]:
            groups.setdefault(naming.folder_key(it["new_name"]), []).append(it)
    out = []
    for group in groups.values():
        if len(group) < 2 or not any(it["changed"] for it in group):
            continue
        numbers = [it["number"] for it in group]
        for it in group:
            if it["changed"]:
                _skip(it, "another chapter would get the same name (chapter "
                      + ", ".join(_num(n) for n in numbers if n != it["number"]) + ")")
        out.append({"name": group[0]["new_name"], "numbers": numbers,
                    "message": f"Chapters {', '.join(_num(n) for n in numbers)} would all be named "
                               f"{group[0]['new_name']}"})
    return out


def _taken(items: list[dict], listing: dict[str, bool], on_disk: dict[str, dict]) -> list[dict]:
    """New names another file in the folder already has: a file mang-arr did
    not record, or a chapter that keeps its name. A chapter whose own file is
    renamed away first does not count, nor does a change of case only.
    Skipping one chapter keeps its name taken, so this repeats until
    nothing changes. on_disk: {name: chapter} for every chapter file in the
    folder."""
    present: dict[str, list[str]] = {}
    for name in listing:
        present.setdefault(naming.folder_key(name), []).append(name)
    out: list[dict] = []
    again = True
    while again:
        again = False
        for it in items:
            if it["skip"] or not it["changed"]:
                continue
            for occupant in present.get(naming.folder_key(it["new_name"]), []):
                if occupant == it["old_name"]:
                    continue
                other = on_disk.get(occupant)
                if other is not None and not other["skip"] and other["changed"] \
                        and naming.folder_key(other["new_name"]) != naming.folder_key(occupant):
                    continue
                if other is None:
                    _skip(it, f"{occupant!r} is already in the folder (mang-arr did not make it); not overwritten")
                    numbers = [it["number"]]
                else:
                    _skip(it, f"chapter {_num(other['number'])} keeps that name")
                    numbers = [it["number"], other["number"]]
                out.append({"name": it["new_name"], "numbers": numbers, "message": _sentence(it["skip"])})
                again = True
                break
    return out


def _sort_warnings(items: list[dict]) -> list[dict]:
    """Neighbouring chapters whose names, after the rename, would sort the
    wrong way round by plain file name, and did not before."""
    after = [(it["number"], it["new_name"] if it["new_name"] and not it["skip"] else it["old_name"])
             for it in items if it["old_name"]]
    before = {(a[0], b[0]) for a, b in naming.sort_inversions((it["number"], it["old_name"])
                                                                for it in items if it["old_name"])}
    new = [(a, b) for a, b in naming.sort_inversions(after) if (a[0], b[0]) not in before]
    if not new:
        return []
    (_, first), (_, second) = new[0]
    more = f" ({len(new)} places in this series)" if len(new) > 1 else ""
    return [{"kind": "sort", "message": f"Readers and file browsers that sort by plain file name would put "
                                        f"{second} before {first}{more}. Komga goes by the chapter number."}]


def komga_check(folder: str, folder_renamed: bool = False) -> dict:
    """Will Komga keep the reading progress of this series' books when their
    files are renamed? {"state", "message", "needs_confirmation"}; states:
    ok, not_in_komga (nothing there to lose), not_configured, hashing_off,
    not_hashed_yet, folder_unverified, ambiguous and error. Only Komga's
    answer to that is known: a reader app that tracks progress by file path
    may start the renamed chapters over, which is why every state but the
    first two needs the user's confirmation. folder_renamed: the series
    folder is renamed too. Komga pairs a renamed file by its hash, but that
    it keeps the books of a renamed series folder is not verified yet
    (media-management proposal 2.2, item 10), so that is folder_unverified
    where the files alone would be ok."""
    return _komga_state(folder, folder_renamed)[0]


def _komga_state(folder: str, folder_renamed: bool = False) -> tuple[dict, list[str]]:
    """(komga_check's answer, the ids of the books Komga has of the series
    now: what a rename is compared with afterwards, komga_kept)."""
    seen: list = []
    return _komga_answer(folder, folder_renamed, seen), _ids(seen[0] if seen else None)


def _komga_answer(folder: str, folder_renamed: bool, seen: list) -> dict:
    """komga_check; the series and books Komga has go into `seen`."""
    if not komga.configured():
        return _komga("not_configured", "No Komga configured: reading progress in your reader app may be lost.")
    library_id = str(settings.get("komga_library_id") or "")
    try:
        found = komga.series_books(folder, library_id or None)
        if found is None:
            return _komga("not_in_komga", "Komga has no books of this series yet, so it has no reading progress to "
                                          "lose.", False)
        seen.append(found)
        lib = komga.library_settings(library_id or found["library_id"])
    except komga.AmbiguousSeries as e:
        if len(e.library_ids) > 1 and not library_id:
            return _komga("ambiguous", f"More than one Komga library has a series folder named {folder!r}: reading "
                                       "progress may be lost. Choose mang-arr's library in Settings -> Komga so it "
                                       "can be checked.")
        return _komga("ambiguous", f"Komga has more than one series in a folder named {folder!r} in the library "
                                   f"{e.library_ids[0]!r}, and cannot tell which one is this series: reading "
                                   "progress may be lost.")
    except Exception as e:          # HTTP or connection error, or an answer that is not what Komga sends
        return _komga("error", f"Could not check Komga ({type(e).__name__}: {str(e)[:200]}): reading progress may "
                               "be lost.")
    if not lib["hash_files"]:
        return _komga("hashing_off", f"Komga file hashing is off for the library {lib['name']!r}: reading progress "
                                     "may be lost. To keep it, turn on Libraries -> Edit -> Options -> Compute hash "
                                     "for files in Komga; Komga then hashes the books in the background.")
    books = found["books"]
    unhashed = sum(1 for b in books if not b["file_hash"])
    if unhashed:
        return _komga("not_hashed_yet", f"Komga has not hashed {unhashed} of {len(books)} books of this series yet: "
                                        "reading progress of those may be lost. Komga hashes them in the "
                                        "background; try again later.")
    if folder_renamed:
        return _komga("folder_unverified", f"Komga keeps the reading progress of renamed files (file hashing is on "
                                           f"for {lib['name']!r} and all {len(books)} books of this series are "
                                           "hashed), but that it keeps it when the series folder is renamed has not "
                                           "been verified yet: reading progress may be lost.")
    return _komga("ok", f"Komga will keep reading progress: file hashing is on for {lib['name']!r} and all "
                        f"{len(books)} books of this series are hashed.", False)


def _ids(found) -> list[str]:
    """The ids of the books in komga.series_books' answer (None: no books)."""
    return [b["id"] for b in found["books"] if b["id"]] if found else []


def _komga(state: str, message: str, needs_confirmation: bool = True) -> dict:
    return {"state": state, "message": message, "needs_confirmation": needs_confirmation}


# -- carrying a preview out ---------------------------------------------------------------------

BACKUP_REASON = "before rename"
UNDO_BACKUP_REASON = "before rename undo"
UNDO_DAYS = 7                   # a finished rename can be undone this long
RUN_WAIT_SECS = 5.0             # for another rename (or a restore) to end
SERIES_WAIT_SECS = 60.0         # for an import, download or delete of the series to end; then it is skipped
TMP_PREFIX = ".mangarr-rename-"         # + the journal row's id: a file set aside for a moment (a ring of names,
TMP_SUFFIX = ".tmp"                     # a change of case only). Hidden, and no chapter file's extension
PROGRESS_LOST = "reading progress in your reader app may be lost"
CHANGED_SINCE = "changed since the preview; left as it is (preview again)"
INTERRUPTED = "the rename was interrupted before this step; left as it is"
PUT_BACK = "the rename was interrupted; the file has its old name back"
FINISHED_STATES = ("done", "cancelled", "failed", "interrupted")


class RenameError(RuntimeError):
    """A rename (or undo) that was refused or could not start: nothing was renamed."""


class NeedsConfirmation(RenameError):
    """Reading progress may be lost and the rename was not confirmed.
    .series: [{"series_id", "title", "state", "message"}] - what to confirm."""

    def __init__(self, series: list[dict]):
        titles = ", ".join(str(s["title"]) for s in series[:3]) + (" ..." if len(series) > 3 else "")
        super().__init__(f"not renamed: {PROGRESS_LOST} ({titles}). Confirm that to rename.")
        self.series = series


def _checkpoint(name: str) -> None:
    """Every step boundary of a rename passes here, with its name. It does
    nothing; the tests put a crash here, to try a kill at every step."""


def converted_steps(con, series_id: int, files: list[dict]) -> list[dict]:
    """The hook for e-reader conversion (0.5.0): the converted copies that
    are renamed together with these library files (user decision 2), as
    journal steps of kind "converted" ({"number", "old_path", "new_path"},
    inside the conversion target's folder), and the conversion rows' source
    paths with them. There is no conversion yet, so there are none; a
    "converted" step in a journal is left alone by this version."""
    return []


def apply(plans, confirmed: bool = False, progress: Callable[[str], None] | None = None,
          should_cancel: Callable[[], bool] | None = None) -> dict:
    """Carry out a preview (plan()'s answer), or a list of them (several
    series, one rename). Only what the preview said and still holds when
    the series' lock is taken is done; everything else is left as it is,
    with its reason. confirmed: the user ticked "reading progress in your
    reader app may be lost" - needed when Komga would not keep it
    (NeedsConfirmation otherwise, before anything starts).

    Returns {"run_id" (None when there was nothing to do), "state",
    "renamed", "skipped", "series": [{"series_id", "title", "renamed",
    "folder", "skipped": [{"number", "name", "reason"}], "problems"}],
    "backup", "komga_scan", "message"}. Raises RenameError when it cannot
    start (another rename is running, the backup failed): nothing was
    renamed then."""
    plans = [plans] if isinstance(plans, dict) else list(plans)
    for p in plans:
        if not isinstance(p, dict) or "series_id" not in p or "formats" not in p:
            raise RenameError("not a rename preview")
    with _run_lock(), db.connect() as con:
        _durable(con)
        _repair_locked(con)                         # an interrupted rename is settled before another starts
        work, komga_before, unconfirmed = [], {}, []
        for given in plans:
            fresh = _fresh(con, given)
            if fresh is None:
                continue
            files, folder, _ = _agreed(given, fresh)
            if not files and not folder:
                continue
            work.append(given)
            check, books = _komga_state(fresh["folder"]["old"], folder_renamed=folder is not None)
            komga_before[str(given["series_id"])] = {"folder": fresh["folder"]["old"], "state": check["state"],
                                                     "books": books}
            if check["needs_confirmation"]:
                unconfirmed.append({"series_id": given["series_id"], "title": fresh["title"],
                                    "state": check["state"], "message": check["message"]})
        if unconfirmed and not confirmed:
            raise NeedsConfirmation(unconfirmed)
        if not work:
            return {"run_id": None, "state": "done", "renamed": 0, "skipped": 0, "series": [], "backup": None,
                    "komga_scan": None, "message": "nothing to rename"}
        options = {"confirmed": bool(confirmed),
                   "series": {str(p["series_id"]): {"formats": p["formats"],
                                                    "use_latest_titles": bool(p.get("use_latest_titles")),
                                                    "only": p.get("only"),
                                                    "rename_folder": bool(p.get("rename_folder", True))}
                              for p in work}}
        run_id, saved = _start(con, BACKUP_REASON, options, komga_before)

        def one(given: dict) -> dict:
            fresh = _fresh(con, given)
            if fresh is None:
                return _result(given["series_id"], str(given.get("title")), note="the series no longer exists")
            files, folder, notes = _agreed(given, fresh)
            out = _execute(con, run_id, given["series_id"], fresh["title"], fresh["folder"]["old"], files, folder,
                           folder_first=False)
            out["skipped"] += notes
            return out
        return _finish(con, run_id, saved, _each(con, run_id, work, one, progress, should_cancel))


def undo(run_id: int, confirmed: bool = False, progress: Callable[[str], None] | None = None,
         should_cancel: Callable[[], bool] | None = None) -> dict:
    """Reverse a finished rename: every file and folder it renamed that is
    still where the rename left it gets its old name (and the chapter its
    old title) back, as a rename of its own - backup, locks, journal and
    all. What has changed since (renamed again, deleted, its old name taken)
    is left as it is, with its reason. For UNDO_DAYS after the rename, once.
    confirmed and the answer: as apply(). Raises RenameError when the run
    cannot be undone."""
    with _run_lock(), db.connect() as con:
        _durable(con)
        _repair_locked(con)
        run = con.execute("SELECT * FROM rename_run WHERE id=?", (run_id,)).fetchone()
        ok, why = can_undo(run)
        if not ok:
            raise RenameError(why)
        work: dict[int, dict] = {}
        for r in con.execute("SELECT * FROM rename_log WHERE run_id=? AND state='done' AND kind IN ('file','folder')"
                             " ORDER BY id DESC", (run_id,)):
            w = work.setdefault(r["series_id"], {"series_id": r["series_id"], "files": [], "folder": None})
            if r["kind"] == "folder":
                w["folder"] = {"old": os.path.basename(r["new_path"]), "new": os.path.basename(r["old_path"])}
            else:
                w["files"].append({"number": r["number"], "old_path": r["new_path"], "new_path": r["old_path"],
                                   "old_title": r["new_title"], "new_title": r["old_title"]})
        if not work:
            raise RenameError("that rename renamed nothing, so there is nothing to undo")
        komga_before, unconfirmed = {}, []
        for sid, w in work.items():
            row = db.get_series(con, sid)
            w["title"] = row["title"] if row else f"series #{sid}"
            w["now"] = row["folder"] if row else None
            if row is None or not db.valid_folder(row["folder"]):
                continue
            check, books = _komga_state(row["folder"], folder_renamed=w["folder"] is not None)
            komga_before[str(sid)] = {"folder": row["folder"], "state": check["state"], "books": books}
            if check["needs_confirmation"]:
                unconfirmed.append({"series_id": sid, "title": w["title"], "state": check["state"],
                                    "message": check["message"]})
        if unconfirmed and not confirmed:
            raise NeedsConfirmation(unconfirmed)
        new_id, saved = _start(con, UNDO_BACKUP_REASON, {"confirmed": bool(confirmed), "undo_of": run_id},
                               komga_before, undo_of=run_id)

        def one(w: dict) -> dict:
            row = db.get_series(con, w["series_id"])
            if row is None:
                return _result(w["series_id"], w["title"], note="the series no longer exists")
            return _execute(con, new_id, w["series_id"], row["title"], row["folder"], w["files"], w["folder"],
                            folder_first=True)
        results = _each(con, new_id, list(work.values()), one, progress, should_cancel)
        con.execute("UPDATE rename_run SET undone_by=? WHERE id=?", (new_id, run_id))
        con.commit()
        return _finish(con, new_id, saved, results)


def can_undo(run) -> tuple[bool, str]:
    """(may this rename_run row be undone, why not)."""
    if run is None:
        return False, "there is no such rename"
    if run["undo_of"] is not None:
        return False, "that is an undo itself; rename again instead"
    if run["state"] not in FINISHED_STATES:
        return False, "that rename has not finished"
    if run["undone_by"] is not None:
        return False, "that rename has been undone already"
    if (run["finished_at"] or run["started_at"] or "") < db.ago(UNDO_DAYS):
        return False, f"that rename is more than {UNDO_DAYS} days old"
    if not run["renamed"]:
        return False, "that rename renamed nothing, so there is nothing to undo"
    return True, ""


class _run_lock:
    """serieslock.rename_run, its Busy as a RenameError."""

    def __enter__(self):
        self._held = serieslock.rename_run(RUN_WAIT_SECS)
        try:
            self._held.__enter__()
        except serieslock.Busy as e:
            raise RenameError(f"not started: {e}") from e
        return self

    def __exit__(self, *exc):
        return self._held.__exit__(*exc)


def _durable(con) -> None:
    """Every commit of this connection reaches the disk before it returns
    (the journal must not be behind the renames after a power cut; the
    database's usual setting syncs only now and then)."""
    con.commit()
    con.execute("PRAGMA synchronous = FULL")


def _fresh(con, given: dict) -> dict | None:
    """The preview made again from what the given one was made with; None
    when the series is gone. RenameError for formats that cannot be used."""
    try:
        return plan(con, given["series_id"], given["formats"], bool(given.get("use_latest_titles")),
                    only=given.get("only"), rename_folder=bool(given.get("rename_folder", True)),
                    check_komga=False)
    except LookupError:
        return None
    except (naming.FormatError, TypeError, ValueError) as e:
        raise RenameError(f"not a rename preview that can be used: {e}") from e


def _agreed(given: dict, fresh: dict) -> tuple[list[dict], dict | None, list[dict]]:
    """What both previews say: (the file steps, the folder step or None, a
    note for each rename only one of them has)."""
    files, notes = [], []
    was = {c.get("number"): c for c in given.get("chapters") or [] if isinstance(c, dict)}
    for c in fresh["chapters"]:
        g = was.get(c["number"])
        mine = c["changed"] and not c["skip"]
        theirs = bool(g and g.get("changed") and not g.get("skip"))
        if mine and theirs and g.get("old_path") == c["old_path"] and g.get("new_path") == c["new_path"]:
            files.append({"number": c["number"], "old_path": c["old_path"],
                          "new_path": os.path.join(os.path.dirname(c["old_path"]), c["new_name"]),
                          "old_title": c["old_title"], "new_title": c["file_title"]})
        elif mine or theirs:
            notes.append({"number": c["number"], "name": c["old_name"],
                          "reason": c["skip"] if theirs and c["skip"] else CHANGED_SINCE})
    folder = None
    f, g = fresh["folder"], given.get("folder") or {}
    if f["changed"] and not f["blocked"]:
        if g.get("changed") and not g.get("blocked") and (g.get("old"), g.get("new")) == (f["old"], f["new"]):
            folder = {"old": f["old"], "new": f["new"]}
        else:
            notes.append({"number": None, "name": f["old"], "reason": CHANGED_SINCE})
    elif g.get("changed") and not g.get("blocked"):
        notes.append({"number": None, "name": f["old"], "reason": f["blocked"] or CHANGED_SINCE})
    return files, folder, notes


def _start(con, reason: str, options: dict, komga_before: dict, undo_of: int | None = None) -> tuple[int, str]:
    """The backup, then the run's row: (its id, the backup's path)."""
    try:
        saved = backup.create(reason)
    except backup.FAILURES as e:
        raise RenameError(f"not started: the database backup before it failed ({e}); nothing was renamed") from e
    cur = con.execute("INSERT INTO rename_run (started_at, state, undo_of, options, backup, komga)"
                      " VALUES (?,?,?,?,?,?)", (db.now(), "running", undo_of, json.dumps(options), saved,
                                                json.dumps(komga_before)))
    con.commit()
    _checkpoint("run")
    return cur.lastrowid, saved


def _result(series_id: int, title: str, note: str | None = None) -> dict:
    out = {"series_id": series_id, "title": title, "renamed": 0, "folder": None, "skipped": [], "problems": []}
    if note:
        out["skipped"].append({"number": None, "name": None, "reason": note})
    return out


def _each(con, run_id: int, work: list[dict], one: Callable[[dict], dict],
          progress: Callable[[str], None] | None, should_cancel: Callable[[], bool] | None) -> list[dict]:
    """one(w) for every series of the run, each under its lock. A series
    that is in use is skipped as a whole; one that fails is settled from
    the disk (as a repair does) and the others go on; a cancel stops before
    the next series, never inside one."""
    cancel = should_cancel or (lambda: False)
    out = []
    for i, w in enumerate(work, 1):
        sid, title = w["series_id"], str(w.get("title") or f"series #{w['series_id']}")
        if cancel():
            out.append(_result(sid, title, note="cancelled before this series"))
            continue
        if progress:
            progress(f"renaming {i} of {len(work)}: {title[:80]}")
        if con.in_transaction:
            con.commit()
        try:
            with serieslock.hold(sid, wait_secs=SERIES_WAIT_SECS, should_cancel=cancel, strict=True):
                try:
                    out.append(one(w))
                except Exception as e:
                    log.exception("%s: renaming failed; settling what was done", title)
                    con.rollback()
                    _settle(con, run_id, sid)
                    res = _result(sid, title)
                    res["problems"].append(f"{type(e).__name__}: {e}"[:300])
                    out.append(res)
        except serieslock.Busy as e:
            log.warning("%s: not renamed: %s", title, e)
            out.append(_result(sid, title, note="an import, download or delete of this series is running; "
                                                "not renamed (try again later)"))
    return out


def _finish(con, run_id: int, saved: str, results: list[dict]) -> dict:
    """Close the run: its counts and message, one Komga scan."""
    _checkpoint("before-finish")
    for res in results:
        res["renamed"] = con.execute("SELECT COUNT(*) FROM rename_log WHERE run_id=? AND series_id=? AND"
                                     " state='done' AND kind='file'", (run_id, res["series_id"])).fetchone()[0]
    renamed, left = _counts(con, run_id)
    left += sum(1 for res in results for s in res["skipped"] if s.get("journal") is None)
    for res in results:
        for s in res["skipped"]:
            s.pop("journal", None)
    cancelled = any(s["reason"].startswith("cancelled") for res in results for s in res["skipped"])
    problems = sum(len(res["problems"]) for res in results)
    state = "cancelled" if cancelled else "failed" if problems and not renamed else "done"
    series = sum(1 for res in results if res["renamed"] or (res["folder"] or {}).get("state") == "done")
    message = (f"{renamed} file(s) and folder(s) renamed in {series} series" if renamed else "nothing renamed") + \
        (f"; {left} left as they are" if left else "") + (f"; {problems} problem(s)" if problems else "")
    con.execute("UPDATE rename_run SET state=?, finished_at=?, renamed=?, skipped=?, message=? WHERE id=?",
                (state, db.now(), renamed, left, message, run_id))
    con.commit()
    _checkpoint("finished")
    scanned = None
    if renamed and komga.configured():
        scanned = komga.scan_retrying()             # one scan for the whole rename, not one per file
    log.info("rename #%d: %s", run_id, message)
    return {"run_id": run_id, "state": state, "renamed": renamed, "skipped": left, "series": results,
            "backup": saved, "komga_scan": scanned, "message": message}


def _counts(con, run_id: int) -> tuple[int, int]:
    """(steps done, steps left undone) of a run's journal."""
    done = con.execute("SELECT COUNT(*) FROM rename_log WHERE run_id=? AND state='done'", (run_id,)).fetchone()[0]
    left = con.execute("SELECT COUNT(*) FROM rename_log WHERE run_id=? AND state != 'done'", (run_id,)).fetchone()[0]
    return done, left


# -- one series --------------------------------------------------------------------------------

def _execute(con, run_id: int, series_id: int, title: str, folder_now: str, files: list[dict],
             folder: dict | None, folder_first: bool) -> dict:
    """Journal the steps of one series and do them (its lock is held):
    the files inside the folder and the folder itself - last for a rename,
    first for an undo, so the file steps' paths are those of the folder
    under its old name either way."""
    out = _result(series_id, title)
    root = config.LIBRARY_ROOT
    rows = []
    if folder is not None:
        rows.append({"kind": "folder", "number": None, "old_path": os.path.join(root, folder["old"]),
                     "new_path": os.path.join(root, folder["new"]), "old_title": None, "new_title": None})
    at = library.library_dir(folder_now)            # where the files are when the journal is written
    rows += [{"kind": "file", **f} for f in files]
    rows += [{"kind": "converted", "old_title": None, "new_title": None, **c}
             for c in converted_steps(con, series_id, files)]
    if not folder_first:
        rows.sort(key=lambda r: r["kind"] == "folder")          # files first, the folder last (stable)
    if not rows:
        return out
    stamp = db.now()
    for r in rows:
        st = _lstat(os.path.join(at, os.path.basename(r["old_path"]))) if r["kind"] == "file" else None
        cur = con.execute(
            "INSERT INTO rename_log (run_id, series_id, number, kind, old_path, new_path, old_title, new_title,"
            " size, mtime_ns, ino, state, at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'planned',?)",
            (run_id, series_id, r["number"], r["kind"], r["old_path"], r["new_path"], r["old_title"],
             r["new_title"], st.st_size if st else None, st.st_mtime_ns if st else None,
             st.st_ino if st else None, stamp))
        r.update(id=cur.lastrowid, run_id=run_id, series_id=series_id, size=st.st_size if st else None,
                 mtime_ns=st.st_mtime_ns if st else None, ino=st.st_ino if st else None, state="planned")
    con.commit()                                    # on disk before the first file is touched (_durable)
    _checkpoint("journal")
    if folder_first and folder is not None:
        out["folder"] = _do_folder(con, series_id, rows[0], out)
    names = [r for r in rows if r["kind"] == "file"]
    if names:
        _do_files(con, series_id, title, names, out)
    for r in rows:
        if r["kind"] == "converted":
            _mark(con, r, "skipped", "converted copies are not renamed by this version", out)
    if not folder_first and folder is not None:
        out["folder"] = _do_folder(con, series_id, rows[-1], out)
    out["problems"] += _verify(con, series_id)
    done = sum(1 for r in rows if r.get("state") == "done")
    if done:
        db.event(con, "renamed", f"{done} file(s) and folder(s) renamed (rename #{run_id})"
                 + (f"; {len(out['skipped'])} left as they are" if out["skipped"] else ""), series_id)
    con.commit()
    return out


def _lstat(path: str) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _key(name: str) -> str:
    return naming.folder_key(name)


def _plain(name: str) -> bool:
    """One usable file or folder name: no separator, NUL, '.' or '..', at
    most NAME_MAX bytes."""
    return (isinstance(name, str) and name not in ("", ".", "..") and "/" not in name and "\0" not in name
            and os.sep not in name and os.path.basename(name) == name
            and len(name.encode("utf-8", "surrogateescape")) <= naming.NAME_MAX)


def _same_dir(a: str, b: str) -> bool:
    return os.path.normpath(a) == os.path.normpath(b)


def _tmp(row_id: int) -> str:
    return f"{TMP_PREFIX}{int(row_id)}{TMP_SUFFIX}"


def _mark(con, row: dict, state: str, detail: str | None, out: dict | None = None) -> None:
    """The journal row's outcome (not 'done': _record_file and
    _record_folder write that together with the database update)."""
    con.execute("UPDATE rename_log SET state=?, detail=?, done_at=? WHERE id=?", (state, detail, db.now(), row["id"]))
    con.commit()
    row["state"] = state
    if out is not None:
        out["skipped"].append({"number": row["number"], "name": os.path.basename(row["old_path"]),
                               "reason": detail, "journal": row["id"]})
    log.info("rename: %s not renamed: %s", row["old_path"], detail)


_renameat2 = None
try:
    _renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    _renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    _renameat2.restype = ctypes.c_int
except (OSError, AttributeError):       # not Linux, or a C library without it
    _renameat2 = None
_NOREPLACE = 1                          # RENAME_NOREPLACE
_NO_RENAMEAT2 = (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP)


def _rename(dir_fd: int, old: str, new: str) -> None:
    """One rename(2) of `old` to `new` inside the open folder, never over
    anything: renameat2 with RENAME_NOREPLACE where the system and the file
    system have it, else a look at the new name and os.rename (nothing else
    writes into a locked series folder). FileExistsError when the new name
    is taken. The file is never linked and unlinked: for a moment it would
    have both names, and a scan at that moment would see two books."""
    if _renameat2 is not None:
        if _renameat2(dir_fd, os.fsencode(old), dir_fd, os.fsencode(new), _NOREPLACE) == 0:
            return
        e = ctypes.get_errno()
        if e not in _NO_RENAMEAT2:
            raise OSError(e, os.strerror(e), old, None, new)
    if library._lstat_at(new, dir_fd) is not None:
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), old, None, new)
    os.rename(old, new, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def _sync(dir_fd: int) -> None:
    """The folder's entries on disk (a rename is a change of the folder)."""
    try:
        os.fsync(dir_fd)
    except OSError as e:                # some network mounts cannot
        log.debug("cannot sync a folder after a rename: %s", e)


def _present(dir_fd: int) -> dict[str, set]:
    """{name as a mount may see it (case, normalisation): the names there}."""
    out: dict[str, set] = {}
    for name in os.listdir(dir_fd):
        out.setdefault(_key(name), set()).add(name)
    return out


def _moved(present: dict[str, set], old: str, new: str) -> None:
    present.get(_key(old), set()).discard(old)
    present.setdefault(_key(new), set()).add(new)


def _open_series_dir(title: str, folder) -> tuple[int, str]:
    """(the series' library folder, open; its path): one plain folder
    inside the library, not the library itself, opened without following a
    symlink. OSError otherwise."""
    d = core.series_library_dir(title, folder)
    if d is None:
        raise OSError(errno.EPERM, "not a folder inside the library", str(folder))
    return library._open_dir_below(config.LIBRARY_ROOT, d), d


def _check_file(con, series_id: int, dir_fd: int, d: str, row: dict) -> str | None:
    """Why this file step cannot be done (None: it can). The paths come from
    the database and the formats; all of it is checked again here."""
    old, new = os.path.basename(row["old_path"]), os.path.basename(row["new_path"])
    if not (_same_dir(os.path.dirname(row["old_path"]), d) and _same_dir(os.path.dirname(row["new_path"]), d)):
        return "the file is not in the series' library folder; left as it is"
    if not _plain(old) or not _plain(new) or new.startswith(TMP_PREFIX) or old == new:
        return "not a file name that can be used; left as it is"
    if library.is_within(row["old_path"], config.STAGING_ROOT):
        return IN_STAGING
    c = con.execute("SELECT status, library_path FROM chapter WHERE series_id=? AND number=?",
                    (series_id, row["number"])).fetchone()
    if c is None or c["status"] != "have" or c["library_path"] != row["old_path"]:
        return CHANGED_SINCE
    st = library._lstat_at(old, dir_fd)
    if st is None:
        return "the file is missing"
    if not stat.S_ISREG(st.st_mode):
        return "not a regular file (a symlink or a folder); left as it is"
    return None


def _do_files(con, series_id: int, title: str, rows: list[dict], out: dict) -> None:
    """The file steps of one series, inside its folder. A step is done when
    its new name is free; one whose new name another step's file still has
    waits for that step. Steps that wait for each other in a ring, and a
    change of case only, go through a temporary name."""
    row = db.get_series(con, series_id)
    try:
        dir_fd, d = _open_series_dir(title, row["folder"] if row else None)
    except OSError as e:
        for r in rows:
            _mark(con, r, "skipped", f"the series' library folder cannot be opened ({e.strerror or e})", out)
        return
    try:
        present = _present(dir_fd)
        pending = []
        for r in rows:
            why = _check_file(con, series_id, dir_fd, d, r)
            if why:
                _mark(con, r, "skipped", why, out)
            else:
                pending.append({"row": r, "old": os.path.basename(r["old_path"]),
                                "new": os.path.basename(r["new_path"]), "src": os.path.basename(r["old_path"])})
        while pending:
            moved = False
            for st in list(pending):
                others = present.get(_key(st["new"]), set()) - {st["src"]}
                if not others and st["src"] == st["old"] and _key(st["old"]) == _key(st["new"]):
                    moved = _park(con, dir_fd, st, present, pending, out) or moved       # a change of case only
                elif not others:
                    _finish_file(con, dir_fd, st, present, out)
                    pending.remove(st)
                    moved = True
                elif not others <= {p["src"] for p in pending if p is not st}:
                    taken = sorted(others - {p["src"] for p in pending if p is not st})[0]
                    _give_up(con, dir_fd, st, present, f"{taken!r} is already in the folder; not overwritten", out)
                    pending.remove(st)
                    moved = True
            if not moved:                   # a ring: set one aside, so the others can move
                st = next((p for p in pending if p["src"] == p["old"]), None)
                if st is None or not _park(con, dir_fd, st, present, pending, out):
                    for p in list(pending):
                        _give_up(con, dir_fd, p, present, "its new name did not become free; left as it is", out)
                    pending.clear()
    finally:
        os.close(dir_fd)


def _park(con, dir_fd: int, st: dict, present: dict, pending: list, out: dict) -> bool:
    """Set a file aside under its temporary name. False (and the step is
    over) when that fails."""
    tmp = _tmp(st["row"]["id"])
    try:
        _checkpoint("before-park")
        _rename(dir_fd, st["src"], tmp)
    except OSError as e:
        _mark(con, st["row"], "failed", f"could not be renamed ({e.strerror or e})", out)
        pending.remove(st)
        return False
    _sync(dir_fd)
    _moved(present, st["src"], tmp)
    st["src"] = tmp
    _checkpoint("parked")
    return True


def _finish_file(con, dir_fd: int, st: dict, present: dict, out: dict) -> None:
    """The rename itself, then the database."""
    row = st["row"]
    try:
        _checkpoint("before-rename")
        _rename(dir_fd, st["src"], st["new"])
    except FileExistsError:
        _give_up(con, dir_fd, st, present, f"{st['new']!r} is already in the folder; not overwritten", out)
        return
    except OSError as e:
        _give_up(con, dir_fd, st, present, f"could not be renamed ({e.strerror or e})", out, state="failed")
        return
    _sync(dir_fd)
    _moved(present, st["src"], st["new"])
    _checkpoint("renamed")
    _record_file(con, row)
    _checkpoint("recorded")


def _give_up(con, dir_fd: int, st: dict, present: dict, why: str, out: dict, state: str = "skipped") -> None:
    """The step is not done. A file that was set aside gets its old name
    back; if even that fails, the database says where it is."""
    row = st["row"]
    if st["src"] != st["old"]:
        try:
            _rename(dir_fd, st["src"], st["old"])
            _sync(dir_fd)
            _moved(present, st["src"], st["old"])
        except OSError as e:
            _stranded(con, row, os.path.join(os.path.dirname(row["old_path"]), st["src"]), e)
            state, why = "failed", f"{why}; it could not get its old name back and is {st['src']!r} now"
    _mark(con, row, state, why, out)


def _stranded(con, row: dict, path: str, e: OSError) -> None:
    """A file that is left under its temporary name: the database follows it."""
    log.error("rename: %s could not get its old name back (%s); it is %s now", row["old_path"], e, path)
    con.execute("UPDATE chapter SET library_path=? WHERE series_id=? AND number=? AND library_path=?",
                (path, row["series_id"], row["number"], row["old_path"]))


def _record_file(con, row) -> None:
    """The database after a file's rename: the chapter's path and title and
    the journal row, in one transaction."""
    con.execute("UPDATE chapter SET library_path=?, file_title=? WHERE series_id=? AND number=? AND library_path=?",
                (row["new_path"], row["new_title"], row["series_id"], row["number"], row["old_path"]))
    con.execute("UPDATE rename_log SET state='done', detail=NULL, done_at=? WHERE id=?", (db.now(), row["id"]))
    con.commit()
    if isinstance(row, dict):
        row["state"] = "done"


def _check_folder(con, series_id: int, root_fd: int, row: dict) -> str | None:
    """Why the folder step cannot be done (None: it can)."""
    old, new = os.path.basename(row["old_path"]), os.path.basename(row["new_path"])
    root = config.LIBRARY_ROOT
    if not (_same_dir(os.path.dirname(row["old_path"]), root) and _same_dir(os.path.dirname(row["new_path"]), root)):
        return "not a folder directly in the library; left as it is"
    if not (_plain(old) and _plain(new) and db.valid_folder(new)) or new.startswith(TMP_PREFIX) or old == new:
        return "not a folder name that can be used; left as it is"
    s = db.get_series(con, series_id)
    if s is None or s["folder"] != old:
        return CHANGED_SINCE
    if any(_key(r[0]) == _key(new) for r in con.execute(
            "SELECT folder FROM series WHERE folder IS NOT NULL AND id != ?", (series_id,))):
        return f"another series has the folder {new!r}"
    st = library._lstat_at(old, root_fd)
    if st is None:
        return "the series' library folder is missing"
    if not stat.S_ISDIR(st.st_mode):
        return "the series' library folder is not a real folder (a symlink?); left as it is"
    others = {n for n in os.listdir(root_fd) if _key(n) == _key(new)} - {old}
    if others:
        return f"a folder named {sorted(others)[0]!r} is already in the library"
    return None


def _do_folder(con, series_id: int, row: dict, out: dict) -> dict:
    """The series folder's rename, then series.folder and the chapters' paths."""
    old, new = os.path.basename(row["old_path"]), os.path.basename(row["new_path"])
    answer = {"old": old, "new": new, "state": "skipped"}
    try:
        root_fd = os.open(config.LIBRARY_ROOT, library._DIR_FLAGS)
    except OSError as e:
        _mark(con, row, "skipped", f"the library folder cannot be opened ({e.strerror or e})", out)
        return answer
    try:
        why = _check_folder(con, series_id, root_fd, row)
        if why:
            _mark(con, row, "skipped", why, out)
            return answer
        src = old
        try:
            if _key(old) == _key(new):                  # a change of case only: through a temporary name
                _checkpoint("before-folder-park")
                _rename(root_fd, old, _tmp(row["id"]))
                src = _tmp(row["id"])
                _sync(root_fd)
                _checkpoint("folder-parked")
            _checkpoint("before-folder-rename")
            _rename(root_fd, src, new)
        except OSError as e:
            state = "skipped" if isinstance(e, FileExistsError) else "failed"
            why = f"a folder named {new!r} is already in the library" if state == "skipped" else \
                f"could not be renamed ({e.strerror or e})"
            if src != old:
                try:
                    _rename(root_fd, src, old)
                except OSError as back:
                    log.error("rename: folder %s could not get its name back (%s); it is %s now", old, back, src)
                    _record_folder(con, {**row, "new_path": os.path.join(config.LIBRARY_ROOT, src)}, state="failed",
                                   detail=f"{why}; it could not get its old name back and is {src!r} now")
                    row["state"] = answer["state"] = "failed"
                    out["skipped"].append({"number": None, "name": old, "reason": why, "journal": row["id"]})
                    return answer
            _mark(con, row, state, why, out)
            answer["state"] = state
            return answer
        _sync(root_fd)
        _checkpoint("folder-renamed")
        _record_folder(con, row)
        row["state"] = answer["state"] = "done"
        _checkpoint("folder-recorded")
        return answer
    finally:
        os.close(root_fd)


def _record_folder(con, row, state: str = "done", detail: str | None = None) -> None:
    """The database after the folder's rename: series.folder, the path of
    every chapter file in it, and the journal row, in one transaction."""
    old_dir, new_dir = row["old_path"], row["new_path"]
    con.execute("UPDATE series SET folder=? WHERE id=? AND folder=?",
                (os.path.basename(new_dir), row["series_id"], os.path.basename(old_dir)))
    for c in con.execute("SELECT number, library_path FROM chapter WHERE series_id=? AND library_path IS NOT NULL",
                         (row["series_id"],)).fetchall():
        if _same_dir(os.path.dirname(c["library_path"]), old_dir):
            con.execute("UPDATE chapter SET library_path=? WHERE series_id=? AND number=?",
                        (os.path.join(new_dir, os.path.basename(c["library_path"])), row["series_id"], c["number"]))
    con.execute("UPDATE rename_log SET state=?, detail=?, done_at=? WHERE id=?", (state, detail, db.now(), row["id"]))
    con.commit()


def _verify(con, series_id: int) -> list[str]:
    """What is not as the database says after a series' rename: every
    chapter the library has must be a regular file at its path, inside the
    library. [] when all is well."""
    out = []
    for c in con.execute("SELECT number, library_path FROM chapter WHERE series_id=? AND status='have'"
                         " AND library_path IS NOT NULL ORDER BY number", (series_id,)).fetchall():
        st = _lstat(c["library_path"])
        if st is None:
            out.append(f"chapter {_num(c['number'])}: no file at {c['library_path']}")
        elif not stat.S_ISREG(st.st_mode) or not library.is_within(c["library_path"], config.LIBRARY_ROOT):
            out.append(f"chapter {_num(c['number'])}: {c['library_path']} is not a file inside the library")
    for p in out[:20]:
        log.warning("rename check: %s", p)
    return out


# -- after an interruption ------------------------------------------------------------------------

def repair() -> dict | None:
    """At start-up: settle every rename that was interrupted (a kill, a
    power cut, a crash), from its journal and the disk, so the database and
    the library agree again. No new rename is started: a step whose rename
    happened gets its database update, a file or folder caught under a
    temporary name gets its new name (its old one when the new one is
    taken), and a step that had not started is marked so; rename again (or
    undo) from there. Returns {"runs", "finished", "put_back", "left"}, or
    None when a rename is running right now (in another process) or there
    is nothing to read. Never raises."""
    try:
        with serieslock.rename_run(0.0), db.connect() as con:
            _durable(con)
            return _repair_locked(con)
    except serieslock.Busy as e:
        log.info("rename repair not run: %s", e)
    except Exception as e:
        log.error("rename repair failed: %s: %s", type(e).__name__, e)
    return None


def _repair_locked(con) -> dict:
    out = {"runs": 0, "finished": 0, "put_back": 0, "left": 0}
    try:
        runs = [r["id"] for r in con.execute("SELECT id FROM rename_run WHERE state='running' ORDER BY id")]
    except Exception as e:                  # a database without the table cannot have an interrupted rename
        log.debug("no rename journal to repair: %s", e)
        return out
    for run_id in runs:
        busy = False
        series = [r[0] for r in con.execute("SELECT DISTINCT series_id FROM rename_log WHERE run_id=? AND"
                                            " state='planned' ORDER BY id", (run_id,))]
        for sid in series:
            try:
                with serieslock.hold(sid, wait_secs=SERIES_WAIT_SECS, strict=True):
                    counts = _settle(con, run_id, sid)
                    for k, v in counts.items():
                        out[k] += v
                    problems = _verify(con, sid)
                    if counts["finished"] or counts["put_back"] or problems:
                        db.event(con, "renamed", f"An interrupted rename (#{run_id}) was settled: "
                                 f"{counts['finished']} step(s) finished, {counts['left']} not started"
                                 + (f"; {len(problems)} file(s) are not where the database says" if problems
                                    else ""), sid)
                        con.commit()
            except serieslock.Busy as e:
                busy = True
                log.warning("rename #%d: series #%d not settled yet (%s); at the next start", run_id, sid, e)
        if busy:
            continue
        done, left = _counts(con, run_id)
        message = f"interrupted; settled at the next start: {done} renamed, {left} left as they are"
        con.execute("UPDATE rename_run SET state='interrupted', finished_at=?, renamed=?, skipped=?, message=?"
                    " WHERE id=? AND state='running'", (db.now(), done, left, message, run_id))
        con.commit()
        out["runs"] += 1
        log.warning("rename #%d was interrupted: %d step(s) renamed, %d left as they are", run_id, done, left)
        if done and komga.configured():
            komga.scan_retrying()               # the interrupted rename never asked for its scan
    return out


def _settle(con, run_id: int, series_id: int) -> dict:
    """Every step of this series the journal still has as planned, decided
    by what is on disk (the series' lock is held)."""
    out = {"finished": 0, "put_back": 0, "left": 0}
    rows = [dict(r) for r in con.execute("SELECT * FROM rename_log WHERE run_id=? AND series_id=? AND"
                                         " state='planned' ORDER BY id", (run_id, series_id)).fetchall()]
    # First the renames that happened and only lack their database update, and nothing is moved: a file
    # that is given a name below (one caught under its temporary name) may take the old name of such a
    # step - two files that swapped names - and the step would then look as if it had never started.
    for moves in (False, True):
        for row in rows:
            if row["state"] != "planned" or (row["kind"] != "file" and not moves):
                continue
            try:
                what = _settle_folder(con, row) if row["kind"] == "folder" else \
                    _settle_file(con, row, moves) if row["kind"] == "file" else None
            except OSError as e:
                _mark(con, row, "failed", f"could not be settled after an interruption ({e.strerror or e})")
                out["left"] += 1
                continue
            if what is not None:
                row["state"] = "settled"
                out[what] += 1
            elif moves:
                _mark(con, row, "skipped", INTERRUPTED)
                out["left"] += 1
    return out


def _is_it(st: os.stat_result | None, row: dict) -> bool:
    """Is this the file the step was planned for (a regular file with the
    inode, size and modification time the journal has; a rename changes
    none of them)?"""
    if st is None or not stat.S_ISREG(st.st_mode):
        return False
    if row.get("ino") is not None and st.st_ino != row["ino"]:
        return False
    return row["size"] is None or (st.st_size, st.st_mtime_ns) == (row["size"], row["mtime_ns"])


def _settle_file(con, row: dict, moves: bool = True) -> str | None:
    """One planned file step: "finished" (it has its new name, and the
    database says so), "put_back" (it was caught under its temporary name
    and has its old name back; the step is marked) or None (the rename had
    not started). moves False: only a rename that happened is recorded, no
    file is given a name (None for a file under its temporary name)."""
    d = os.path.dirname(row["old_path"])
    old, new = os.path.basename(row["old_path"]), os.path.basename(row["new_path"])
    root = config.LIBRARY_ROOT
    if not (_same_dir(os.path.dirname(row["new_path"]), d) and _plain(old) and _plain(new)
            and library.is_within(d, root) and os.path.realpath(d) != os.path.realpath(root)
            and not library.is_within(d, config.STAGING_ROOT)):
        raise OSError(errno.EPERM, "not a file inside a series folder of the library")
    try:
        dir_fd = library._open_dir_below(root, d)
    except FileNotFoundError:
        return None                         # the folder is not there under this name: nothing of the step happened
    try:
        tmp = _tmp(row["id"])
        at_old, at_new = library._lstat_at(old, dir_fd), library._lstat_at(new, dir_fd)
        if _is_it(library._lstat_at(tmp, dir_fd), row):
            if not moves:
                return None
            if at_new is None:
                _rename(dir_fd, tmp, new)
                _sync(dir_fd)
                _record_file(con, row)
                return "finished"
            if at_old is None:
                _rename(dir_fd, tmp, old)
                _sync(dir_fd)
                _mark(con, row, "skipped", PUT_BACK)
                return "put_back"
            _stranded(con, row, os.path.join(d, tmp), OSError(errno.EEXIST, "both of its names are taken"))
            _mark(con, row, "failed", f"interrupted; both of its names are taken, it is {tmp!r} now")
            return "put_back"
        # it has its new name. Its old name is free, or another file has it by now: the one it swapped
        # names with, whose step was recorded (known from this one by its inode)
        if _is_it(at_new, row) and (at_old is None or (row.get("ino") is not None and not _is_it(at_old, row))):
            _record_file(con, row)
            return "finished"
        return None
    finally:
        os.close(dir_fd)


def _settle_folder(con, row: dict) -> str | None:
    """One planned folder step, as _settle_file."""
    old, new = os.path.basename(row["old_path"]), os.path.basename(row["new_path"])
    root = config.LIBRARY_ROOT
    if not (_same_dir(os.path.dirname(row["old_path"]), root) and _same_dir(os.path.dirname(row["new_path"]), root)
            and _plain(old) and _plain(new) and db.valid_folder(new) and db.valid_folder(old)):
        raise OSError(errno.EPERM, "not a folder directly in the library")
    root_fd = os.open(root, library._DIR_FLAGS)
    try:
        def folder(name: str) -> bool:
            st = library._lstat_at(name, root_fd)
            return st is not None and stat.S_ISDIR(st.st_mode)
        tmp = _tmp(row["id"])
        s = db.get_series(con, row["series_id"])
        ours = s is not None and s["folder"] == old        # the database has not followed yet
        if folder(tmp):
            if library._lstat_at(new, root_fd) is None:
                _rename(root_fd, tmp, new)
                _sync(root_fd)
                _record_folder(con, row)
                return "finished"
            if library._lstat_at(old, root_fd) is None:
                _rename(root_fd, tmp, old)
                _sync(root_fd)
                _mark(con, row, "skipped", PUT_BACK)
                return "put_back"
            _record_folder(con, {**row, "new_path": os.path.join(root, tmp)}, state="failed",
                           detail=f"interrupted; both of its names are taken, it is {tmp!r} now")
            return "put_back"
        taken = any(_key(r[0]) == _key(new) for r in con.execute(
            "SELECT folder FROM series WHERE folder IS NOT NULL AND id != ?", (row["series_id"],)))
        if ours and not taken and folder(new) and library._lstat_at(old, root_fd) is None:
            _record_folder(con, row)
            return "finished"
        return None
    finally:
        os.close(root_fd)


def reconcile(con) -> int:
    """After a database restore: the restored rows say where the files were
    when the backup was written; renames done since (the journal is kept
    through a restore) have moved them. Every chapter whose file is not at
    its restored path, and every series whose folder is not, follows the
    journal to where the file or folder is now - only to a regular file (a
    real folder) inside the library that is really there. Returns how many
    rows were corrected."""
    fixed = 0
    rows = con.execute("SELECT * FROM rename_log WHERE state='done' AND kind IN ('file','folder') ORDER BY id"
                       ).fetchall()
    if not rows:
        return 0
    root = config.LIBRARY_ROOT
    for sid in sorted({r["series_id"] for r in rows}):
        s = db.get_series(con, sid)
        if s is None or not db.valid_folder(s["folder"]):
            continue
        mine = [r for r in rows if r["series_id"] == sid]
        folder = s["folder"]
        chapters = {c["number"]: [c["library_path"], c["file_title"]] for c in con.execute(
            "SELECT number, library_path, file_title FROM chapter WHERE series_id=? AND library_path IS NOT NULL",
            (sid,)).fetchall()}
        for r in mine:                      # the journal, replayed on the restored rows
            if r["kind"] == "folder":
                if os.path.basename(r["old_path"]) == folder and _same_dir(os.path.dirname(r["old_path"]), root):
                    folder = os.path.basename(r["new_path"])
                    for c in chapters.values():
                        if _same_dir(os.path.dirname(c[0]), r["old_path"]):
                            c[0] = os.path.join(r["new_path"], os.path.basename(c[0]))
            elif r["number"] in chapters and chapters[r["number"]][0] == r["old_path"]:
                chapters[r["number"]] = [r["new_path"], r["new_title"]]
        if folder != s["folder"] and db.valid_folder(folder) and not os.path.lexists(library.library_dir(s["folder"])):
            d = library.library_dir(folder)
            taken = con.execute("SELECT 1 FROM series WHERE folder=? AND id != ?", (folder, sid)).fetchone()
            if os.path.isdir(d) and not os.path.islink(d) and library.is_within(d, root) and not taken:
                con.execute("UPDATE series SET folder=? WHERE id=?", (folder, sid))
                fixed += 1
        for number, (path, title) in chapters.items():
            was = con.execute("SELECT library_path FROM chapter WHERE series_id=? AND number=?",
                              (sid, number)).fetchone()[0]
            st = _lstat(path)
            if path == was or os.path.lexists(was) or st is None or not stat.S_ISREG(st.st_mode) \
                    or not library.is_within(path, root):
                continue
            con.execute("UPDATE chapter SET library_path=?, file_title=? WHERE series_id=? AND number=?",
                        (path, title, sid, number))
            fixed += 1
    if fixed:
        log.warning("restore: %d chapter path(s) and folder(s) follow the renames done since the backup", fixed)
    return fixed


# -- looking back -----------------------------------------------------------------------------------

def runs(con, limit: int = 50, series_id: int | None = None) -> list[dict]:
    """The renames (and undos) so far, newest first, each with "can_undo"
    and, when it cannot, "undo_reason"."""
    sql = "SELECT * FROM rename_run"
    args: tuple = ()
    if series_id is not None:
        sql += " WHERE id IN (SELECT run_id FROM rename_log WHERE series_id=?)"
        args = (series_id,)
    out = []
    for r in con.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, int(limit))).fetchall():
        ok, why = can_undo(r)
        out.append({**{k: r[k] for k in r.keys() if k not in ("options", "komga")}, "can_undo": ok,
                    "undo_reason": why or None})
    return out


def steps(con, run_id: int) -> list[dict]:
    """The journal of one rename, in order."""
    return [dict(r) for r in con.execute("SELECT * FROM rename_log WHERE run_id=? ORDER BY id", (run_id,))]


def komga_kept(con, run_id: int) -> list[dict]:
    """After a rename and Komga's scan: which of the books Komga had of each
    renamed series it still has (their reading progress with them). One
    {"series_id", "title", "before", "kept", "message"} per series Komga had
    books of; Komga is asked now, so this is for after its scan. A series
    Komga cannot be asked about has kept None."""
    run = con.execute("SELECT komga FROM rename_run WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise LookupError(f"no rename #{run_id}")
    try:
        before = json.loads(run["komga"] or "{}")
    except ValueError:
        before = {}
    library_id = str(settings.get("komga_library_id") or "")
    out = []
    for sid, was in before.items():
        ids = set(was.get("books") or []) if isinstance(was, dict) else set()
        s = db.get_series(con, int(sid)) if str(sid).isdigit() else None
        if not ids or s is None:
            continue
        kept = None
        try:
            found = komga.series_books(s["folder"], library_id or None)
            kept = len(ids & set(_ids(found)))
        except Exception as e:
            log.info("%s: Komga could not be asked about its books: %s: %s", s["title"], type(e).__name__, e)
        message = "Komga could not be asked" if kept is None else \
            f"Komga kept {kept} of {len(ids)} books" + (" (reading progress intact)" if kept == len(ids) else
                                                         "; the others are new books to it: their reading "
                                                         "progress is lost, or its scan has not finished yet")
        out.append({"series_id": int(sid), "title": s["title"], "before": len(ids), "kept": kept,
                    "message": message})
    return out


# -- as a job ---------------------------------------------------------------------------------------

def unconfirmed(plans) -> list[dict]:
    """The series of these previews whose rename must be confirmed first
    (what the previews say; apply asks Komga again)."""
    plans = [plans] if isinstance(plans, dict) else list(plans)
    return [{"series_id": p["series_id"], "title": p.get("title"), "state": p["komga"]["state"],
             "message": p["komga"]["message"]}
            for p in plans if isinstance(p.get("komga"), dict) and p["komga"].get("needs_confirmation")]


def job(plans, confirmed: bool = False) -> Callable:
    """apply(plans) as the function of a job (jobs.Runner.submit): its
    progress and cancel are the job's, and the series are the job's series,
    so nothing else is started for them meanwhile."""
    plans = [plans] if isinstance(plans, dict) else list(plans)

    def run(j) -> str:
        j.active_series_ids = frozenset(p["series_id"] for p in plans)
        return apply(plans, confirmed, progress=lambda m: setattr(j, "progress", m),
                     should_cancel=lambda: j.cancel)["message"]
    return run


def undo_job(run_id: int, confirmed: bool = False) -> Callable:
    """undo(run_id) as the function of a job."""
    def run(j) -> str:
        return undo(run_id, confirmed, progress=lambda m: setattr(j, "progress", m),
                    should_cancel=lambda: j.cancel)["message"]
    return run


def submit(runner, plans, confirmed: bool = False):
    """Queue the rename of these previews on the job runner; returns the
    job. One rename job at a time (a second call returns the one that is
    queued or running). NeedsConfirmation, before anything is queued, when
    a preview says reading progress may be lost and it was not confirmed."""
    plans = [plans] if isinstance(plans, dict) else list(plans)
    ask = unconfirmed(plans)
    if ask and not confirmed:
        raise NeedsConfirmation(ask)
    title = str(plans[0].get("title")) if len(plans) == 1 else f"{len(plans)} series"
    return runner.submit("rename", f"rename files: {title}", job(plans, confirmed),
                         series_id=plans[0]["series_id"] if len(plans) == 1 else None, key="rename")

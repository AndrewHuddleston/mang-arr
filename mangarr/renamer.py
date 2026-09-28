"""Renaming a series' library files to the naming formats (Settings ->
Media Management; see naming.py).

This part plans. plan() is the preview Preview Rename shows: for every
chapter the library has, the file's current path and the one the formats
give, and what would stop or spoil the rename:
- two chapters that would get one name, or a new name that another file in
  the folder already has (that chapter is left as it is, never overwritten);
- names that would sort out of order by plain file name (012.5 before 012);
- what becomes of reading progress in Komga.
It renames nothing, opens nothing for writing and writes nothing to the
database. It reads the database, the series' library folder (which names
are taken) and, when there is something to rename, Komga.

Titles: by default a chapter keeps the title its current file name has, so
a source that edits a title, or a title that became known after the file was
linked, renames nothing (user decision 1); use_latest_titles takes the
source's latest chapter name instead (a chapter with no name on record keeps
its file's title). When the source's name still gives the very title the
file name has, that name is used, so a new colon replacement applies to its
':'.

The title is read from the current name with the chosen format, else the
default one, and a reading only counts when rendering it again gives the
name back; a name the chosen format made is always read with it, so a
second preview with the same formats proposes nothing (a format with text
after the title would otherwise add that text again on every Organize).
Which title a name carries is only known for names these two formats make:
mang-arr keeps no record of older formats yet. A name neither made (a file
organized to a format that has been changed since, say) is left as it is,
with its reason, unless use_latest_titles gives it the source's title.
"""
import logging
import os

from . import config, db, komga, library, naming, settings

log = logging.getLogger(__name__)


def plan(con, series_id: int, formats=None, use_latest_titles: bool = False) -> dict:
    """The rename preview for one series, as a dict ready for JSON:

    folder      {"old", "new", "changed", "blocked"} for the series folder;
                blocked says why it cannot be renamed (None when it can)
    chapters    one per chapter the library has, by number: {"number",
                "old_path", "new_path", "old_name", "new_name", "title",
                "title_from" ("file" or "source"), "changed", "skip"}; skip
                says why the file is left as it is (None when it is not).
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

    formats: naming.Options or a dict of the naming settings (None: the
    defaults). Raises naming.FormatError when the formats cannot be used and
    LookupError for an unknown series."""
    options = naming.as_options(formats)
    errors = naming.check_options(options)
    if errors:
        raise naming.FormatError(errors)
    row = db.get_series(con, series_id)
    if row is None:
        raise LookupError(f"no series with id {series_id}")
    series = naming.SeriesNames.from_row(row)
    out = {"series_id": series_id, "title": row["title"], "use_latest_titles": bool(use_latest_titles),
           "folder": None, "chapters": [], "renames": 0, "collisions": [], "warnings": [], "komga": None}
    old_folder = row["folder"]
    if not db.valid_folder(old_folder):
        out["folder"] = {"old": old_folder, "new": None, "changed": False,
                         "blocked": "this series has no usable library folder"}
        return out
    new_folder = _new_folder(con, row, series, options)
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
    for r in con.execute("SELECT number, name, library_path FROM chapter WHERE series_id=? AND status='have'"
                         " ORDER BY number", (series_id,)):
        item, state = _item(r, series, options, old_dir, new_dir, listing, use_latest_titles)
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
    if out["renames"] or folder_moves:
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
            "title": None, "title_from": None, "changed": False, "skip": None}
    if not path:
        item["skip"] = "no library file is recorded for this chapter"
        return item, None
    name = item["old_name"] = os.path.basename(path)
    in_folder = os.path.dirname(os.path.normpath(path)) == os.path.normpath(old_dir)
    item["new_path"] = os.path.join(new_dir, name) if in_folder else path      # a skipped file moves with its folder
    if not in_folder:
        item["skip"] = "the file is not in the series' library folder; left as it is"
    elif listing is None:
        item["skip"] = "the series' library folder is missing, or not a real folder inside the library"
    elif name not in listing:
        item["skip"] = "the file is missing"
    elif not listing[name]:
        item["skip"] = "not a regular file (a symlink or a folder); left as it is"
    if item["skip"]:
        return item, None
    found = _chapter_info(n, name, r["name"], series, options, use_latest_titles)
    if found is None:
        item["skip"] = UNREAD_NO_SOURCE if r["name"] is None else UNREAD
        return item, "unread"
    chap, item["title_from"] = found
    new = naming.render(series, chap, options)
    item.update(new_name=new, new_path=os.path.join(new_dir, new), changed=new != name,
                title=naming.title_value(chap, options) or None)
    return item, "read"


def _chapter_info(number: float, file_name: str, source_name: str | None, series: naming.SeriesNames,
                  options: naming.Options, use_latest_titles: bool) -> tuple[naming.ChapterInfo, str] | None:
    """(what to render, where its title comes from), or None when the
    title in the current name cannot be known.

    The name is read with the chosen format and with the default one (every
    name so far); a reading counts only when rendering its title with that
    format gives the name back. A name the chosen format made keeps the
    chosen format's reading, so it renders to itself again. When the
    source's name gives the title that was read (with the format that made
    the name), the source's name is rendered: the same title, but a new
    colon replacement or title length applies to it."""
    if use_latest_titles and source_name is not None:
        return naming.ChapterInfo(number, source_name), "source"
    readings = {}
    for made_with in dict.fromkeys((options, naming.DEFAULTS)):
        found, title = naming.title_from_name(file_name, series, number, made_with)
        if found and naming.render(series, naming.ChapterInfo(number, file_title=title or ""),
                                   made_with) == file_name:
            readings[made_with] = title or ""
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
    if not komga.configured():
        return _komga("not_configured", "No Komga configured: reading progress in your reader app may be lost.")
    library_id = str(settings.get("komga_library_id") or "")
    try:
        found = komga.series_books(folder, library_id or None)
        if found is None:
            return _komga("not_in_komga", "Komga has no books of this series yet, so it has no reading progress to "
                                          "lose.", False)
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


def _komga(state: str, message: str, needs_confirmation: bool = True) -> dict:
    return {"state": state, "message": message, "needs_confirmation": needs_confirmation}

"""The files: what Suwayomi wrote, and the clean per-series library.

Suwayomi downloads into <staging>/<Source>/<Series>/<scanlator>_<chapter name>.cbz
and treats that tree as its record of what is downloaded, so it must never
be renamed. The library is a second tree, <library>/<Series>/Chapter 012.0.cbz,
built from hard links, that Komga reads. One folder per series regardless
of how many sources the chapters came from.
"""
import os
import re
import shutil

from . import config

_EXT = (".cbz", ".cbr", ".zip")

# Chapter number from a Suwayomi file name. The scanlator prefix (up to the
# first "_") is ignored by matching the chapter keyword anywhere. Keywords
# come from real names on disk: Chapter, Ch., Episode, #, Page, Day, Mission,
# Room, Act, Step, bullet ...
_SEASON = re.compile(r"(?<![A-Za-z])S(\d+)\s*[-–]\s*(?:Episode|Ep\.?|Chapter|Ch\.?)\s*(\d+(?:\.\d+)?)", re.I)
_KEYWORD = re.compile(
    r"(?:\b(?:chapter|chap|ch|episode|ep|page|day|mission|room|act|step|bullet|part|lesson|round|file|case|night|stage)\b\.?|#)"
    r"\s*(\d+(?:\.\d+)?)", re.I)
_LASTNUM = re.compile(r"(\d+(?:\.\d+)?)(?!.*\d)")


def parse_number(filename: str) -> float | None:
    """Chapter number in a file name, or None when there is no number."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    if _SEASON.search(stem):
        return None                       # season-numbered: handled elsewhere
    m = _KEYWORD.search(stem)
    if m:
        return float(m.group(1))
    m = _LASTNUM.search(stem)
    return float(m.group(1)) if m else None


def parse_season(filename: str) -> tuple[int, float] | None:
    m = _SEASON.search(os.path.splitext(os.path.basename(filename))[0])
    return (int(m.group(1)), float(m.group(2))) if m else None


def scan_series_dir(path: str) -> tuple[dict[float, str], list[str]]:
    """{chapter number: file path} for one series folder, plus the files
    whose number could not be read. Duplicate numbers keep the first name."""
    found: dict[float, str] = {}
    unparsed: list[str] = []
    for name in sorted(os.listdir(path)):
        if not name.lower().endswith(_EXT):
            continue
        full = os.path.join(path, name)
        n = parse_number(name)
        if n is None:
            unparsed.append(full)
        elif n not in found:
            found[n] = full
    return found, unparsed


def staging_dirs(root: str = config.STAGING_ROOT) -> list[tuple[str, str, str]]:
    """[(source name, series folder name, path)] for every series folder."""
    out = []
    if not os.path.isdir(root):
        return out
    for src in sorted(os.listdir(root)):
        sdir = os.path.join(root, src)
        if not os.path.isdir(sdir) or src.startswith("."):
            continue
        for series in sorted(os.listdir(sdir)):
            p = os.path.join(sdir, series)
            if os.path.isdir(p) and not series.startswith("."):
                out.append((src, series, p))
    return out


_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_title(title: str) -> str:
    """Folder name for a series title (same rules Suwayomi uses)."""
    t = _ILLEGAL.sub("_", title).strip().rstrip(".")
    return re.sub(r"\s+", " ", t) or "untitled"


def chapter_filename(number: float) -> str:
    """'Chapter 012.0.cbz' - fixed width so Komga sorts 12 before 12.5 and
    before 100, and .5 chapters sort after their integer."""
    return f"Chapter {number:05.1f}.cbz"


def library_dir(title: str, root: str = config.LIBRARY_ROOT) -> str:
    return os.path.join(root, safe_title(title))


def link_into_library(src_path: str, title: str, number: float,
                      root: str = config.LIBRARY_ROOT) -> str:
    """Hard-link a staged chapter into the library. Copies when the two
    trees are on different filesystems. Returns the library path."""
    d = library_dir(title, root)
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, chapter_filename(number))
    if os.path.exists(dst):
        if os.path.samefile(src_path, dst):
            return dst
        os.remove(dst)
    try:
        os.link(src_path, dst)
    except OSError:
        shutil.copy2(src_path, dst)
    return dst


def scan_library_dir(title: str, root: str = config.LIBRARY_ROOT) -> dict[float, str]:
    d = library_dir(title, root)
    if not os.path.isdir(d):
        return {}
    found, _ = scan_series_dir(d)
    return found

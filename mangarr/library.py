"""The files: what Suwayomi wrote, and the clean per-series library.

Suwayomi downloads into <staging>/<Source>/<Series>/<scanlator>_<chapter name>.cbz
and treats that tree as its record of what is downloaded, so it must never
be renamed. The library is a second tree, <library>/<folder>/Chapter 012.0.cbz,
built from hard links, that Komga reads. One folder per tracked series
(the folder name is stored with the series, so two series with the same
title never share one), regardless of how many sources the chapters came from.
"""
import logging
import os
import re
import shutil

from . import config

log = logging.getLogger(__name__)

_EXT = (".cbz", ".cbr", ".zip")

# Chapter number from a Suwayomi file name. The scanlator prefix (up to the
# first "_") is ignored by matching the chapter keyword anywhere. Keywords
# come from real names on disk: Chapter, Ch., Episode, #, Page, Day, Mission,
# Room, Act, Step, bullet ...
_SEASON = re.compile(r"(?<![A-Za-z])S(\d+)\s*[-–]\s*(?:Episode|Ep\.?|Chapter|Ch\.?)\s*(\d+(?:\.\d+)?)", re.I)
_KEYWORD = re.compile(
    r"(?:\b(?:chapter|chap|ch|episode|ep|page|day|mission|room|act|step|bullet|part|lesson|round|file|case|"
    r"night|stage)\b\.?|#)\s*(\d+(?:\.\d+)?)", re.I)
_VOLUME_ONLY = re.compile(r"(?<![A-Za-z])vol(?:ume)?\.?\s*\d+", re.I)
_LASTNUM = re.compile(r"(\d+(?:\.\d+)?)(?!.*\d)")


def parse_number(filename: str) -> float | None:
    """Chapter number in a file name, or None when there is none (volume-only
    files are not chapters; season-numbered files such as 'S2 - Episode 5'
    carry no global number - import resolves those through Suwayomi's own
    chapter listing, see suwayomi_name_map)."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    m = _KEYWORD.search(stem)
    if m and not (_SEASON.search(stem) and _SEASON.search(stem).start() <= m.start()):
        return float(m.group(1))
    if _SEASON.search(stem):
        return None
    if m:
        return float(m.group(1))
    if m:
        return float(m.group(1))
    if _VOLUME_ONLY.search(stem):
        return None
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
    try:
        names = sorted(os.listdir(path))
    except OSError as e:
        log.error("cannot list %s: %s", path, e)
        return found, unparsed
    for name in names:
        if not name.lower().endswith(_EXT):
            continue
        full = os.path.join(path, name)
        n = parse_number(name)
        if n is None:
            unparsed.append(full)
        elif n not in found:
            found[n] = full
    return found, unparsed


def staging_dirs(root: str | None = None) -> list[tuple[str, str, str]]:
    """[(source name, series folder name, path)] for every series folder."""
    root = root or config.STAGING_ROOT
    out = []
    if not os.path.isdir(root):
        log.error("staging root %s does not exist", root)
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


def unique_folder(title: str, taken: set[str], suffix: str) -> str:
    """A folder name not already used by another series: the title, or the
    title plus a disambiguating suffix (the series ref)."""
    base = safe_title(title)
    if base not in taken:
        return base
    return safe_title(f"{base} ({suffix})")


def chapter_filename(number: float, label: str | None = None) -> str:
    """'Chapter 012.0.cbz' - fixed width so Komga sorts 12 before 12.5 and
    before 100, and .5 chapters sort after their integer. Two decimals only
    when the number needs them (5.25). A label (the source's chapter title
    when it says more than the number, e.g. 'S2 - Episode 5') is appended:
    'Chapter 012.0 - S2 - Episode 5.cbz'."""
    base = f"Chapter {number:06.2f}" if round(number, 1) != round(number, 2) else f"Chapter {number:05.1f}"
    label = chapter_label(number, label)
    return f"{base} - {label}.cbz" if label else f"{base}.cbz"


def chapter_label(number: float, name: str | None) -> str | None:
    """The part of a source chapter name worth keeping in the file name:
    None for 'Chapter 12' / 'Ch.12' / 'Episode 12', the name otherwise."""
    if not name:
        return None
    n = re.sub(r"\s+", " ", name).strip()
    plain = re.fullmatch(r"(?:chapter|chap|ch|episode|ep|#)?\.?\s*0*(\d+(?:\.\d+)?)\s*[:.\-]?\s*", n, re.I)
    if plain and float(plain.group(1)) == number:
        return None
    return safe_title(n)[:80]


def suwayomi_name_map(chapters) -> dict[str, float]:
    """{file stem as Suwayomi writes it: chapter number} so files whose name
    carries no global number (season episodes) can still be matched."""
    out = {}
    for c in chapters:
        stem = f"{c.scanlator}_{c.name}" if c.scanlator else (c.name or "")
        out[safe_title(stem).lower()] = c.number
    return out


def library_dir(folder: str, root: str | None = None) -> str:
    return os.path.join(root or config.LIBRARY_ROOT, folder)


_copy_warned = False
COPIED = 0          # chapters copied because a hard link was impossible


def link_into_library(src_path: str, folder: str, number: float, root: str | None = None,
                      replace: bool = False, label: str | None = None) -> str | None:
    """Hard-link a staged chapter into the library. Copies when the two
    trees are on different filesystems or mounts (logged once). Returns the
    library path, or None when a different file already sits there and
    `replace` is False - the library never overwrites what it did not make."""
    global _copy_warned, COPIED
    d = library_dir(folder, root)
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, chapter_filename(number, label))
    if os.path.exists(dst):
        if os.path.samefile(src_path, dst):
            return dst
        if not replace:
            log.error("%s exists and is not a link to %s; leaving it alone", dst, src_path)
            return None
        os.remove(dst)
    try:
        os.link(src_path, dst)
    except OSError as e:
        if not _copy_warned:
            log.warning("cannot hard-link %s -> %s (%s); copying instead. Staging and library are on "
                        "different filesystems or mounts, so every chapter is stored twice", src_path, dst, e)
            _copy_warned = True
        shutil.copy2(src_path, dst)
        COPIED += 1
    return dst


def scan_library_dir(folder: str, root: str | None = None) -> dict[float, str]:
    d = library_dir(folder, root)
    if not os.path.isdir(d):
        return {}
    found, _ = scan_series_dir(d)
    return found

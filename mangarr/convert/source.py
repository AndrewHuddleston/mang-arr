"""Reading a chapter archive for conversion: which entries are pages and in
what order, each entry read with a size cap, and ComicInfo.xml read only
when it is small and plain. Standard library only.

The input is a library file, so library.verify_archive has already checked
its directory limits, compression methods and CRCs. These caps still hold
on their own, because the converter also runs on files given by hand
(mangarr convert-file) and a page is decoded in memory.
"""
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib

from ..library import MAX_ENTRIES
from . import ConvertError

# Everything the library counts as a page (library._IMAGE_EXT) plus BMP, so
# a page Pillow cannot decode (JPEG XL) fails with its name instead of
# silently going missing from the book.
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".jxl")
MAX_ENTRY_BYTES = 128 << 20         # one page: far above any real scan, and a bound on memory
MAX_COMICINFO = 1 << 20

# zipfile's errors for a damaged or unsupported entry (bad CRC, truncated
# deflate, deflate64, encryption)
ENTRY_ERRORS = (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, EOFError, OSError, ValueError)


def natural_key(name: str) -> list:
    # re.split with one group puts the runs of ASCII digits at the odd
    # indexes, so numbers only meet numbers. A run is compared as (length,
    # digits) without its leading zeros, which orders like int() but never
    # fails: not on "²" (a digit to str.isdigit, not to int), nor on a run
    # past int()'s 4300-digit limit.
    parts = re.split(r"([0-9]+)", name)
    return [(len(d := t.lstrip("0")), d) if i % 2 else t.lower() for i, t in enumerate(parts)]


def page_names(z: zipfile.ZipFile) -> list[str]:
    """The page entries in reading order: natural sort on the full path
    (p2 before p10, folder by folder), without directories, non-images,
    dotfiles and macOS resource forks (__MACOSX)."""
    infos = z.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ConvertError(f"{len(infos)} entries in the archive (at most {MAX_ENTRIES})")
    names = [i.filename for i in infos if not i.is_dir()
             and i.filename.lower().endswith(IMAGE_EXT)
             and not any(p.startswith(".") or p == "__MACOSX" for p in i.filename.split("/"))]
    return sorted(names, key=natural_key)


def too_big(name: str, cap: int) -> ConvertError:
    return ConvertError(f"{name}: larger than {cap / (1 << 20):g} MiB")


def read_entry(z: zipfile.ZipFile, name: str, cap: int | None = None) -> bytes:
    """One entry, refused above cap bytes (default MAX_ENTRY_BYTES) whatever
    its header declares. Reading to the end makes zipfile check the CRC."""
    cap = MAX_ENTRY_BYTES if cap is None else cap
    try:
        if z.getinfo(name).file_size > cap:
            raise too_big(name, cap)
        with z.open(name) as f:
            data = f.read(cap + 1)
    except ConvertError:
        raise
    except ENTRY_ERRORS as e:
        raise ConvertError(f"{name}: cannot be read from the archive ({e})") from e
    if len(data) > cap:
        raise too_big(name, cap)
    return data


_DTD = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.I)


def read_comicinfo(z: zipfile.ZipFile) -> dict[str, str]:
    """The top-level fields of ComicInfo.xml, or {} when there is none or it
    is not usable: larger than MAX_COMICINFO, not UTF-8, carrying a DOCTYPE
    or ENTITY declaration (so no entity is ever expanded), or not XML. It is
    only a fallback for what the caller passes in meta."""
    name = next((n for n in z.namelist() if n.lower() == "comicinfo.xml"), None)
    if name is None:
        return {}
    try:
        text = read_entry(z, name, MAX_COMICINFO).decode("utf-8-sig")
    except (ConvertError, UnicodeDecodeError):
        return {}
    if _DTD.search(text):
        return {}
    try:
        root = ET.fromstring(text)      # a str: expat parses it as UTF-8 whatever the declaration says
    except ET.ParseError:
        return {}
    return {el.tag: (el.text or "").strip() for el in root if len(el) == 0 and isinstance(el.tag, str)}

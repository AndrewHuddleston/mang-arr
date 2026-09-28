"""The library's file and folder names as mang-arr 0.2.3 made them: library.py
at b1fd2db, copied unchanged (safe_title, fit_name, _folder_key,
unique_folder, chapter_filename, chapter_label and their patterns). This is
the reference test_naming.py holds naming.render() to with the default
options: never edit it to make a test pass."""
import hashlib
import logging
import re
import unicodedata

log = logging.getLogger(__name__)

MAX_PARSE = 1000        # chars of a name the chapter-number patterns look at




_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_title(title: str) -> str:
    """Folder name for a series title (same rules Suwayomi uses: illegal
    characters become '_', leading and trailing dots and spaces go, so a
    title such as '.hack//Link' does not become a hidden folder)."""
    t = _ILLEGAL.sub("_", title or "")
    return re.sub(r"\s+", " ", t).strip(" .") or "untitled"


NAME_MAX = 255          # bytes in one file or folder name (ext4, btrfs, xfs, zfs ...)


def fit_name(name: str, max_bytes: int = NAME_MAX, keep: str = "") -> str:
    """name (plus the fixed ending `keep`, e.g. '.cbz') cut to at most
    max_bytes of UTF-8, on a character boundary. A cut name gets a short
    hash of the full name, so two long names that share a prefix stay
    distinct and the same name always gives the same result."""
    full = name + keep
    if len(full.encode("utf-8")) <= max_bytes:
        return full
    tag = "~" + hashlib.sha1(full.encode("utf-8")).hexdigest()[:8]
    room = max_bytes - len((tag + keep).encode("utf-8"))
    cut = name.encode("utf-8")[:max(room, 0)].decode("utf-8", "ignore").rstrip(" .")
    log.debug("name longer than %d bytes, shortened: %r -> %r", max_bytes, full[:80], cut[:40] + "..." + tag)
    return cut + tag + keep


def _folder_key(name: str) -> str:
    """How a filesystem may see a folder name: case-insensitive (macOS,
    Windows, SMB mounts) and normalisation-insensitive (NFC vs NFD)."""
    return unicodedata.normalize("NFC", name).casefold()


def unique_folder(title: str, taken: set[str], suffix: str) -> str:
    """A folder name not already used by another series: the title, or the
    title plus a disambiguating suffix (the series ref), plus a counter if
    even that is taken. Names are NFC, fit NAME_MAX bytes, and are compared
    case- and normalisation-insensitively, so two series never share a
    folder on a case-insensitive mount either."""
    keys = {_folder_key(t) for t in taken if t}
    base = unicodedata.normalize("NFC", safe_title(title))
    for i in range(1000):
        cand = base if i == 0 else f"{base} ({suffix})" if i == 1 else f"{base} ({suffix}) {i}"
        cand = fit_name(unicodedata.normalize("NFC", safe_title(cand)))
        if _folder_key(cand) not in keys:
            if cand != base:
                log.info("library folder %r is taken or too long; using %r", base[:120], cand)
            return cand
    raise ValueError(f"no free library folder name for {title[:120]!r}")


def chapter_filename(number: float, label: str | None = None) -> str:
    """'Chapter 012.0.cbz' - fixed width so Komga sorts 12 before 12.5 and
    before 100, and .5 chapters sort after their integer. Two decimals only
    when the number needs them (5.25). A label (the source's chapter title
    when it says more than the number, e.g. 'S2 - Episode 5') is appended:
    'Chapter 012.0 - S2 - Episode 5.cbz'."""
    base = f"Chapter {number:06.2f}" if round(number, 1) != round(number, 2) else f"Chapter {number:05.1f}"
    label = chapter_label(number, label)
    return fit_name(f"{base} - {label}", keep=".cbz") if label else f"{base}.cbz"


def chapter_label(number: float, name: str | None) -> str | None:
    """The part of a source chapter name worth keeping in the file name:
    None for 'Chapter 12' / 'Ch.12' / 'Episode 12', the name otherwise."""
    if not name:
        return None
    # capped and whitespace-collapsed first, so the patterns below (anchored,
    # no nested quantifiers) stay cheap on a hostile source's chapter name
    n = re.sub(r"\s+", " ", name[:MAX_PARSE]).strip()
    plain = _PLAIN_NAME.fullmatch(n)
    if plain and float(plain.group(1)) == number:
        return None
    lead = _LEAD_NAME.match(n)
    if lead and float(lead.group(1)) == number:
        n = lead.group(2).strip()
    return safe_title(n)[:80]


# 'Chapter 12', 'Ch.012:' ... (float() drops leading zeros, so no 0* here:
# 0* followed by \d+ backtracks quadratically on a long run of zeros)
_PLAIN_NAME = re.compile(r"(?:chapter|chap|ch|episode|ep|#)?\.? ?(\d+(?:\.\d+)?) ?[:.\-]? ?", re.I)
_LEAD_NAME = re.compile(r"(?:chapter|chap|ch|episode|ep|#)?\.? ?(\d+(?:\.\d+)?) ?[:.\-–] ?(.+)$", re.I)


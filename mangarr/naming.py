"""Names in the library: the series folder and chapter file formats
(Settings -> Media Management -> Chapter Naming).

A format is Sonarr's token grammar: literal text plus tokens such as
{Series Title} or {Chapter:000.0}. Token names ignore case. Text inside the
braces before or after the name is kept only when the token has a value, so
"{ - Chapter Title}" adds " - The Storm" to a titled chapter and nothing to a
plain one, and "{ (Year)}" adds " (2017)" only when the year is known.

With the default options, render() gives exactly the names mang-arr has
always given; library.chapter_filename() and unique_folder() call it, and
tests/test_naming.py holds it to the code from before (naming_reference.py):

    series folder   {Series Title}                              Yakuza Fiancé_ Raise wa Tanin ga Ii
    chapter file    Chapter {Chapter:000.0}{ - Chapter Title}   Chapter 064.5 - Extra_ The Daily Life.cbz

One deliberate difference: a chapter file name with no title that is longer
than NAME_MAX bytes (a chapter number of some 240 digits; no real one comes
close) is now cut to fit like every other name, where 0.2.3 left it too long
for the file system.

Whatever the options:
- '/', '\\', NUL and control characters become '_', in a title taken from
  an existing file name too;
- every name fits NAME_MAX bytes. In a chapter file name the titles are cut
  (the chapter title first), not the chapter number or the format's own
  text, and a short hash of the whole name goes where the cut is, so two
  long names stay apart (with the default format that is fit_name, as
  before);
- a folder name is NFC, has no leading or trailing dots or spaces, and is
  never another series' folder, ignoring case and normalisation (a taken
  name gets the series ref as a suffix: "Wind Breaker (anilist_12345)");
- a chapter file name never starts with a dot or a space.
Chapter titles keep the normalisation the source gave them, as they always
have: a file name that changed from NFD to NFC would be a second, different
name on Linux file systems.

Stdlib only; nothing here touches the disk or the database.
"""
import hashlib
import logging
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, fields
from functools import lru_cache

log = logging.getLogger(__name__)

NAME_MAX = 255          # bytes in one file or folder name (ext4, btrfs, xfs, zfs ...)
MAX_PARSE = 1000        # chars of a source chapter name the title patterns look at (as library.MAX_PARSE)
FORMAT_MAX = 200        # characters in a format
TITLE_MAX = 255         # the highest chapter_title_max_chars; names are cut to NAME_MAX bytes anyway
EXT = ".cbz"

# colon_replacement -> what a ':' becomes ("smart" is worked out per colon)
COLON_MODES = {"underscore": "_", "delete": "", "dash": "-", "space_dash": " -", "smart": None}


@dataclass(frozen=True)
class Options:
    """The naming settings. The defaults are the names mang-arr has always made."""
    series_folder_format: str = "{Series Title}"
    chapter_file_format: str = "Chapter {Chapter:000.0}{ - Chapter Title}"
    colon_replacement: str = "underscore"
    replace_illegal_characters: bool = True     # ? * " < > | become '_' (off: removed)
    chapter_title_max_chars: int = 80
    drop_number_only_titles: bool = False       # "Vol.3 chapter 13" counts as no title


DEFAULTS = Options()
OPTION_KEYS = tuple(f.name for f in fields(Options))


def as_options(value=None) -> Options:
    """Options from Options, a dict of settings (other keys are ignored,
    missing ones take their default) or None (the defaults). Not checked:
    see check_options."""
    if value is None:
        return DEFAULTS
    if isinstance(value, Options):
        return value
    return Options(**{k: value[k] for k in OPTION_KEYS if k in value})


@dataclass(frozen=True)
class SeriesNames:
    """What the series tokens are made of."""
    title: str | None                   # mang-arr's title (English, else romaji, else native)
    romaji: str | None = None
    english: str | None = None
    native: str | None = None
    year: int | None = None

    @classmethod
    def from_row(cls, row) -> "SeriesNames":
        """From a series row (sqlite3.Row or dict)."""
        keys = row.keys()
        return cls(*(row[k] if k in keys else None for k in ("title", "romaji", "english", "native", "year")))


@dataclass(frozen=True)
class ChapterInfo:
    """What the chapter tokens are made of. {Chapter Title} comes from
    file_title when it is set (the title as an existing file name carries
    it, already cleaned: used as it is, but for the characters no name may
    hold), else from the source's chapter name (chapter_title)."""
    number: float
    name: str | None = None
    file_title: str | None = None


_NO_SERIES = SeriesNames(None)

# -- the grammar ------------------------------------------------------------------

TITLE_TOKENS = ("Series Title", "Series Romaji", "Series English", "Series Native")
SERIES_TOKENS = (*TITLE_TOKENS, "Year")
CHAPTER_TOKENS = ("Chapter", "Chapter Title")
_LOOKUP = {n.casefold(): n for n in SERIES_TOKENS + CHAPTER_TOKENS}
_LOOKUP.update({"series title romaji": "Series Romaji", "series title english": "Series English",
                "series title native": "Series Native"})
TOKEN_LIST = ", ".join(f"{{{n}}}" for n in (*SERIES_TOKENS, "Chapter:000.0", "Chapter Title"))


@dataclass(frozen=True)
class Token:
    name: str               # the canonical name: "Series Title", "Chapter" ...
    prefix: str = ""        # kept only when the token has a value
    suffix: str = ""
    width: int = 0          # {Chapter:000.0}: zero-padded digits before the dot ...
    decimals: int = 0       # ... and the least number of decimals


class FormatError(ValueError):
    """A format that cannot be used; .messages says why, for the user."""

    def __init__(self, messages: list[str]):
        super().__init__(" ".join(messages))
        self.messages = messages


_BRACED = re.compile(r"\{([^{}]*)\}")
_INSIDE = re.compile(r"(?P<pre>[ \-_.(\[]*)(?P<name>[A-Za-z]+(?: [A-Za-z]+)*)(?::(?P<spec>[^ \-_)\]]*))?"
                     r"(?P<post>[ \-_.)\]]*)")
_SPEC = re.compile(r"(0*)(?:\.(0*))?")
MAX_WIDTH, MAX_DECIMALS = 6, 3


@lru_cache(maxsize=64)
def parse(fmt: str) -> tuple:
    """The parts of a format: str for literal text, Token for a token.
    Raises FormatError."""
    parts: list = []
    errors: list[str] = []
    at = 0
    for m in _BRACED.finditer(fmt):
        _literal(fmt[at:m.start()], parts, errors)
        tok = _token(m.group(1), errors)
        if tok:
            parts.append(tok)
        at = m.end()
    _literal(fmt[at:], parts, errors)
    if errors:
        raise FormatError(errors)
    return tuple(parts)


def _literal(text: str, parts: list, errors: list[str]) -> None:
    if "{" in text:
        errors.append("A { has no closing }.")
    elif "}" in text:
        errors.append("A } has no opening {.")
    elif text:
        parts.append(text)


def _token(inside: str, errors: list[str]) -> Token | None:
    m = _INSIDE.fullmatch(inside)
    name = _LOOKUP.get(m.group("name").casefold()) if m else None
    if name is None:
        errors.append(f"Unknown token {{{inside}}}. Valid tokens: {TOKEN_LIST}.")
        return None
    spec = m.group("spec")
    width = decimals = 0
    if spec is not None:
        if name != "Chapter":
            errors.append(f"{{{inside}}}: only {{Chapter}} takes a number format, such as {{Chapter:000.0}}.")
            return None
        sm = _SPEC.fullmatch(spec)
        if not spec or not sm:
            errors.append(f"{{{inside}}}: a chapter number format is zeros with an optional dot, such as "
                          "{Chapter:000} or {Chapter:000.0}.")
            return None
        width, decimals = len(sm.group(1)), len(sm.group(2) or "")
        if width > MAX_WIDTH or decimals > MAX_DECIMALS:
            errors.append(f"{{{inside}}}: at most {MAX_WIDTH} zeros before the dot and {MAX_DECIMALS} after it.")
            return None
    return Token(name, m.group("pre"), m.group("post"), width, decimals)


def validate(fmt, kind: str) -> list[str]:
    """What is wrong with a series folder format (kind "folder") or a
    chapter file format (kind "chapter"), as messages for the settings
    page; empty when it can be used. Formats are refused, never mended.
    (A name can never come out empty: a folder name falls back to
    "untitled", and a chapter file name always has its number.)"""
    if kind not in ("folder", "chapter"):
        raise ValueError(f"kind must be 'folder' or 'chapter', not {kind!r}")
    if not isinstance(fmt, str) or not fmt.strip():
        return ["The format is empty."]
    errors = []
    if len(fmt) > FORMAT_MAX:
        errors.append(f"The format is {len(fmt)} characters long; the limit is {FORMAT_MAX}.")
    if "/" in fmt or "\\" in fmt:
        errors.append("The format cannot contain / or \\: each series has one folder, and its chapter files "
                      "sit directly in it.")
    try:
        parts = parse(fmt)
    except FormatError as e:
        return errors + e.messages
    names = {p.name for p in parts if isinstance(p, Token)}
    if kind == "folder":
        if names & set(CHAPTER_TOKENS):
            errors.append("{Chapter} and {Chapter Title} can only be used in the chapter file format.")
        if not names & set(TITLE_TOKENS):
            errors.append("The series folder format must contain a series title: {Series Title}, "
                          "{Series Romaji}, {Series English} or {Series Native}.")
    else:
        if "Chapter" not in names:
            errors.append("The chapter file format must contain {Chapter} (for example {Chapter:000.0}): "
                          "without it, every chapter of a series would get the same name.")
        if fmt[:1] in (" ", "."):
            errors.append("The chapter file format cannot start with a space or a dot.")
    return errors


def check_options(options: Options) -> list[str]:
    """Every problem with a set of naming options, as messages for the
    settings page; empty when they can be used."""
    errors = [f"Series Folder Format: {m}" for m in validate(options.series_folder_format, "folder")]
    errors += [f"Chapter File Format: {m}" for m in validate(options.chapter_file_format, "chapter")]
    if not isinstance(options.colon_replacement, str) or options.colon_replacement not in COLON_MODES:
        errors.append(f"Colon Replacement must be one of: {', '.join(COLON_MODES)}.")
    n = options.chapter_title_max_chars
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= TITLE_MAX:
        errors.append(f"Chapter Title Length must be a whole number from 1 to {TITLE_MAX}.")
    for key, label in (("replace_illegal_characters", "Replace Illegal Characters"),
                       ("drop_number_only_titles", "Number-only Titles")):
        if not isinstance(getattr(options, key), bool):
            errors.append(f"{label} must be on or off.")
    return errors


# -- characters ---------------------------------------------------------------------

_ALWAYS = re.compile(r"[\\/\x00-\x1f]")        # separators, NUL and control characters: always '_'
_ILLEGAL = re.compile(r'[*?"<>|]')             # replace_illegal_characters: '_', else removed
_COLON_BEFORE_SPACE = re.compile(r":(?=\s)")
_WS = re.compile(r"\s+")


def replace_chars(text: str, options: Options = DEFAULTS) -> str:
    """text with the characters a name cannot hold replaced (or removed),
    per the options. With the defaults, every one of \\ / : * ? " < > | and
    the control characters becomes '_', as Suwayomi does."""
    t = _ALWAYS.sub("_", text)
    mode = options.colon_replacement
    if mode == "smart":             # "Re:Zero" -> "Re-Zero", "Title: Sub" -> "Title - Sub"
        t = _COLON_BEFORE_SPACE.sub(" -", t).replace(":", "-")
    else:
        t = t.replace(":", COLON_MODES[mode])
    return _ILLEGAL.sub("_" if options.replace_illegal_characters else "", t)


def clean(text: str | None, options: Options = DEFAULTS) -> str:
    """A title made safe for one name: characters replaced, runs of
    whitespace made one space, leading and trailing dots and spaces trimmed
    (so '.hack//Link' is not a hidden folder); "untitled" if nothing is
    left. With the defaults this is library.safe_title."""
    t = replace_chars(text or "", options)
    return _WS.sub(" ", t).strip(" .") or "untitled"


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


_FIT_TAG = re.compile(r"~[0-9a-f]{8}$")


def folder_key(name: str) -> str:
    """How a filesystem may see a name: case-insensitive (macOS, Windows,
    SMB mounts) and normalisation-insensitive (NFC vs NFD)."""
    return unicodedata.normalize("NFC", name).casefold()


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


# -- values ---------------------------------------------------------------------------

def format_number(number: float, width: int = 3, decimals: int = 1) -> str:
    """A chapter number with `width` zero-padded whole digits and at least
    `decimals` decimals; more decimals when the number needs them, up to 2
    (12.25). Wider numbers just grow (1000.0). The defaults are the
    library's names: 005.0, 012.5, 012.25, 100.0."""
    need = 2 if round(number, 1) != round(number, 2) else 1 if round(number, 0) != round(number, 1) else 0
    d = max(decimals, need)
    return f"{number:0{width + (d + 1 if d else 0)}.{d}f}"


# A source chapter name that only repeats the number ('Chapter 12', 'Ch.012:')
# or starts with it ('Chapter 12: The Storm'). float() drops leading zeros,
# so no 0* here: 0* followed by \d+ backtracks quadratically on a run of zeros.
_PLAIN_NAME = re.compile(r"(?:chapter|chap|ch|episode|ep|#)?\.? ?(\d+(?:\.\d+)?) ?[:.\-]? ?", re.I)
_LEAD_NAME = re.compile(r"(?:chapter|chap|ch|episode|ep|#)?\.? ?(\d+(?:\.\d+)?) ?[:.\-–] ?(.+)$", re.I)
# a title that is only volume and chapter numbers: 'Vol.3 chapter 13', 'Vol. 1 Ch. 4', '#13', '13'
_NUMBER_ONLY = re.compile(r"(?:vol(?:ume)?\.? ?\d+(?:\.\d+)?)?[ ,:\-–]*"
                          r"(?:(?:(?:chapter|chap|ch|episode|ep)\.?|#)? ?\d+(?:\.\d+)?)?[ ,:.\-–]*", re.I)
NUMBER_ONLY_MAX = 60    # characters; longer titles say more than numbers


def number_only(title: str) -> bool:
    """Is this title nothing but volume and chapter numbers?"""
    return (len(title) <= NUMBER_ONLY_MAX and any(c.isdigit() for c in title)
            and _NUMBER_ONLY.fullmatch(title) is not None)


def chapter_title(number: float, name: str | None, options: Options = DEFAULTS) -> str:
    """{Chapter Title} from a source's chapter name: "" for a name that only
    repeats the number ('Chapter 12', 'Ch.12', 'Episode 12'), the name
    without a leading repeat of the number otherwise ('Chapter 12: The
    Storm' -> 'The Storm'), cleaned and cut to chapter_title_max_chars.
    With the defaults this is library.chapter_label (with "" for None)."""
    # capped and whitespace-collapsed first (raw_title), so the patterns
    # (anchored, no nested quantifiers) stay cheap on a hostile source's chapter name
    n = raw_title(number, name)
    if n is None or (options.drop_number_only_titles and number_only(n)):
        return ""
    return clean(n, options)[:options.chapter_title_max_chars]


def raw_title(number: float, name: str | None) -> str | None:
    """A source's chapter name as chapter_title reads it, before it is
    cleaned and cut: whitespace made single spaces, a leading repeat of the
    number taken off ('Chapter 12: Extra: The Storm' -> 'Extra: The Storm');
    None for no name and for one that only repeats the number."""
    if not name:
        return None
    n = _WS.sub(" ", name[:MAX_PARSE]).strip()
    plain = _PLAIN_NAME.fullmatch(n)
    if plain and float(plain.group(1)) == number:
        return None
    lead = _LEAD_NAME.match(n)
    if lead and float(lead.group(1)) == number:
        n = lead.group(2).strip()
    return n


def stored_title(chapter: ChapterInfo, options: Options = DEFAULTS) -> str:
    """What chapter.file_title records for the name render() gives this
    chapter: the title that name carries ("" when it has none). A chapter
    rendered from a stored title keeps it. One rendered from the source's
    name stores that name's own spelling of the title (raw_title: its ':'
    still a ':'), so a colon replacement or title length chosen later
    applies to it - but only when that spelling renders to the very title
    in the name; otherwise the title as the name has it."""
    if chapter.file_title is not None:
        return chapter.file_title
    return title_to_store(chapter.number, chapter.name, chapter_title(chapter.number, chapter.name, options), options)


def title_to_store(number: float, source_name: str | None, title: str, made_with: Options = DEFAULTS) -> str:
    """chapter.file_title for a file whose name carries `title` and was made
    with the options `made_with`: the source's own spelling of it
    (raw_title) when that renders to exactly this title, else the title as
    it is (see stored_title)."""
    raw = raw_title(number, source_name)
    if raw and chapter_title(number, source_name, made_with) == title \
            and title_value(ChapterInfo(number, file_title=raw), made_with) == title:
        return raw
    return title


def read_title(filename: str, series: SeriesNames | None, number: float,
               options: Options = DEFAULTS) -> str | None:
    """The title in a chapter file name these options made ("" when it has
    none), or None when they did not make it: the name must have the
    format's shape (title_from_name) and rendering the title again must
    give the name back."""
    found, title = title_from_name(filename, series, number, options)
    if found and render(series, ChapterInfo(number, file_title=title or ""), options) == filename:
        return title or ""
    return None


def title_value(chapter: ChapterInfo, options: Options = DEFAULTS) -> str:
    """What {Chapter Title} gives for this chapter. A title taken from an
    existing file name is already clean and is kept as it is, so the name
    comes out the same; it is only cut when it is longer than the length
    setting (a hash tag that a cut added to that name does not count), and
    left out when it is only numbers and drop_number_only_titles is on.
    Characters no name may hold are replaced in it all the same (replace_chars:
    a clean title has none, so this changes nothing a format made), so a
    file_title from anywhere - a stored one, a restored backup - can never
    put a '/', NUL or control character in a name."""
    if chapter.file_title is None:
        return chapter_title(chapter.number, chapter.name, options)
    t = replace_chars(chapter.file_title, options)
    if not t or (options.drop_number_only_titles and number_only(t)):
        return ""
    tag = _FIT_TAG.search(t)
    body = t[:tag.start()] if tag else t
    return body[:options.chapter_title_max_chars] if len(body) > options.chapter_title_max_chars else t


def _series_value(name: str, series: SeriesNames) -> str:
    if name == "Year":
        return str(series.year) if series.year else ""
    other = {"Series Romaji": series.romaji, "Series English": series.english,
             "Series Native": series.native}.get(name)
    return other or series.title or ""


# -- rendering ------------------------------------------------------------------------------

def render(series: SeriesNames | None, chapter: ChapterInfo | None = None, options: Options = DEFAULTS, *,
           taken=None, suffix: str | None = None) -> str:
    """The series folder name (chapter None) or a chapter's file name, with
    EXT, per the options. For a folder, `taken` (the other series' folder
    names) and `suffix` (the series ref) make it unique, as
    library.unique_folder always has: the name, else the name plus
    " (suffix)", else that plus a counter; a suffix is only needed when the
    name is taken (ValueError without one then). Raises FormatError for a
    format that is not valid for its kind."""
    series = series or _NO_SERIES
    if chapter is None:
        if taken is None:
            return next(_folder_candidates(_folder_base(series, options), options, ""))
        return _unique_folder(series, options, taken, suffix)
    segments: list[tuple[str, str]] = []       # (text, "title", "series" or "" for text that is never cut)
    for p in _parts(options.chapter_file_format, "chapter"):
        if isinstance(p, str):
            segments.append((replace_chars(p, options), ""))
            continue
        v = _chapter_value(p, series, chapter, options)
        if v:
            kind = "title" if p.name == "Chapter Title" else "series" if p.name in TITLE_TOKENS else ""
            segments += [(p.prefix, ""), (v, kind), (p.suffix, "")]
    return _fit_chapter_name(_lstrip(segments))


def _lstrip(segments: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """segments without the leading dots and spaces of the name they make
    (so a name is never hidden)."""
    out = list(segments)
    for i, (text, kind) in enumerate(out):
        out[i] = (text.lstrip(" ."), kind)
        if out[i][0]:
            break
    return out


def _fit_chapter_name(segments: list[tuple[str, str]]) -> str:
    """The chapter file name the segments make, with EXT, cut to NAME_MAX
    bytes when it is longer. Only titles are cut - the chapter title first,
    then the series titles, last one first - so the chapter number and the
    format's own text stay in the name (and it can still be read back:
    title_from_name). The hash tag goes where the first cut is. With the
    default format the title ends the name, so this is fit_name(name,
    keep=EXT), byte for byte. A name whose titles are not enough to cut is
    cut as a whole (fit_name)."""
    full = "".join(text for text, _ in segments)
    if len((full + EXT).encode("utf-8")) <= NAME_MAX:
        return full + EXT
    tag = "~" + hashlib.sha1((full + EXT).encode("utf-8")).hexdigest()[:8]
    over = len((full + tag + EXT).encode("utf-8")) - NAME_MAX
    texts = [text for text, _ in segments]
    order = [i for i, (_, kind) in enumerate(segments) if kind == "title"]
    order += [i for i in reversed(range(len(segments))) if segments[i][1] == "series"]
    first = None
    for i in order:
        if over <= 0:
            break
        raw = texts[i].encode("utf-8")
        texts[i] = raw[:max(len(raw) - over, 0)].decode("utf-8", "ignore")
        over -= len(raw) - len(texts[i].encode("utf-8"))
        first = i if first is None else first
    if over > 0 or first is None:
        return fit_name(full, keep=EXT)
    name = "".join(texts[:first + 1]).rstrip(" .") + tag + "".join(texts[first + 1:]) + EXT
    log.debug("name longer than %d bytes, shortened: %r -> %r", NAME_MAX, full[:80], name)
    return name


def _parts(fmt: str, kind: str) -> tuple:
    """parse(fmt), refused (FormatError) when it has the wrong tokens for its
    kind ("folder" or "chapter")."""
    parts = parse(fmt)
    names = {p.name for p in parts if isinstance(p, Token)}
    if (names & set(CHAPTER_TOKENS)) if kind == "folder" else ("Chapter" not in names):
        raise FormatError(validate(fmt, kind))
    return parts


def _chapter_value(tok: Token, series: SeriesNames, chapter: ChapterInfo, options: Options) -> str:
    if tok.name == "Chapter":
        return format_number(chapter.number, tok.width, tok.decimals)
    if tok.name == "Chapter Title":
        return title_value(chapter, options)
    v = _series_value(tok.name, series)
    return _nfc(clean(v, options)) if v else ""


def _folder_base(series: SeriesNames, options: Options) -> str:
    """The series folder format's name, cleaned and NFC, not yet fitted."""
    out = []
    for p in _parts(options.series_folder_format, "folder"):
        if isinstance(p, str):
            out.append(p)
            continue
        v = _series_value(p.name, series)
        if v:
            out.append(p.prefix + v + p.suffix)
    return _nfc(clean("".join(out), options))


def _folder_candidates(base: str, options: Options, suffix: str | None) -> Iterator[str]:
    """The folder names a series may have, best first: the format's name
    (base), then with " (suffix)", then with a counter as well (1000 in
    all). Each is cleaned, NFC and fitted to NAME_MAX; the suffix goes on
    the name before it is fitted, so a long name keeps it."""
    for i in range(1000):
        cand = base if i == 0 else f"{base} ({suffix})" if i == 1 else f"{base} ({suffix}) {i}"
        yield fit_name(_nfc(clean(cand, options)))


def _unique_folder(series: SeriesNames, options: Options, taken, suffix: str | None) -> str:
    keys = {folder_key(t) for t in taken if t}
    base = _folder_base(series, options)
    for cand in _folder_candidates(base, options, suffix):
        if folder_key(cand) not in keys:
            if cand != base:
                log.info("library folder %r is taken or too long; using %r", base[:120], cand)
            return cand
        if suffix is None:
            raise ValueError(f"library folder {cand[:120]!r} is taken, and there is no series ref to add to it")
    raise ValueError(f"no free library folder name for {str(series.title)[:120]!r}")


def folder_matches(folder: str, series: SeriesNames, suffix: str, options: Options = DEFAULTS) -> bool:
    """Is `folder` one of the names the folder format gives this series
    (with or without its suffix)? A series keeps such a folder: that the
    plain name has become free since is no reason to rename it."""
    return any(cand == folder for cand in _folder_candidates(_folder_base(series, options), options, suffix))


def title_from_name(filename: str, series: SeriesNames | None, number: float,
                    options: Options = DEFAULTS) -> tuple[bool, str | None]:
    """Does `filename` have the shape the chapter file format gives chapter
    `number` (with any title)? Returns (True, title) - title None for a name
    without one - or (False, None). A name whose title was cut to fit keeps
    its hash tag in the title, so rendering that title again gives the same
    name. The shape alone can fit more than one format ("Chapter 012.0 - The
    Storm [2017].cbz" is the default format's with the title "The Storm
    [2017]"): whoever needs to know which format made a name renders the
    title again and compares, as renamer.plan does."""
    if not filename.endswith(EXT):
        return False, None
    series = series or _NO_SERIES
    pattern, titled = [], False
    for p in _parts(options.chapter_file_format, "chapter"):
        if isinstance(p, str):
            pattern.append(re.escape(replace_chars(p, options)))
        elif p.name == "Chapter Title":
            group = "(?P=title)" if titled else "(?P<title>.+?)"
            titled = True
            pattern.append(f"(?:{re.escape(p.prefix)}{group}{re.escape(p.suffix)})?")
        else:
            v = _chapter_value(p, series, ChapterInfo(number), options)
            if v:
                pattern.append(re.escape(p.prefix + v + p.suffix))
    m = re.fullmatch("".join(pattern), filename[:-len(EXT)], re.S)
    if not m:
        return False, None
    return True, m.group("title") if titled else None


# -- preview --------------------------------------------------------------------------------

SAMPLE_SERIES = SeriesNames("Yakuza Fiancé: Raise wa Tanin ga Ii", romaji="Raise wa Tanin ga Ii",
                            english="Yakuza Fiancé: Raise wa Tanin ga Ii", native="来世は他人がいい", year=2017)
SAMPLE_CHAPTERS = (
    ChapterInfo(12.0, "Chapter 12"),
    ChapterInfo(64.5, "Chapter 64.5: Extra: The Daily Life of Two People"),
    ChapterInfo(72.1, "Hibi Chouchou x Hirunaka no Ryuusei Crossover Special: The Day the Two Stories Met "
                      "Under the Same Sky"),
)


def pick_examples(chapters) -> list[ChapterInfo]:
    """Up to three chapters that show a format well: a plain one (no
    title), a fractional one with a title, and the one with the longest
    title. chapters: ChapterInfo, or (number, name) pairs."""
    chapters = [c if isinstance(c, ChapterInfo) else ChapterInfo(*c) for c in chapters]
    titled = [(len(chapter_title(c.number, c.name)), c) for c in chapters]
    picks = [next((c for n, c in titled if not n), None),
             next((c for n, c in titled if n and not float(c.number).is_integer()), None),
             max(titled, key=lambda x: x[0], default=(0, None))[1]]
    out: list[ChapterInfo] = []
    for c in picks:
        if c is not None and c not in out:
            out.append(c)
    return out


def sort_inversions(names) -> list[tuple[tuple[float, str], tuple[float, str]]]:
    """((number, name), (number, name)) for each pair of neighbouring
    chapters, lower number first, whose names sort the other way round by
    plain file name (code point order). names: (number, name) pairs; if
    every neighbouring pair is in order, the whole list is."""
    ordered = sorted(names, key=lambda x: x[0])
    return [(a, b) for a, b in zip(ordered, ordered[1:], strict=False) if b[1] < a[1]]


def _stem(name: str) -> str:
    return name[:-len(EXT)] if name.endswith(EXT) else name


def sort_warnings(options: Options = DEFAULTS, highest: float | None = None) -> list[str]:
    """Warnings (not errors) for a chapter format whose names sort out of
    order by plain file name: 012.5 before 012 (no decimals and nothing
    after the number), or 100 before 12 (fewer digits than `highest`, the
    series' highest chapter). Komga sorts by the number inside the file when
    there is one; other readers and file browsers do not."""
    out = []
    shapes = ((None, None), (None, "Extra"), ("The Storm", None), ("The Storm", "Extra"))
    for a, b in shapes:
        pair = [(n, render(SAMPLE_SERIES, ChapterInfo(n, t), options)) for n, t in ((12.0, a), (12.5, b))]
        if sort_inversions(pair):
            out.append(f"Readers and file browsers that sort by plain file name put {_stem(pair[1][1])} "
                       f"before {_stem(pair[0][1])}.")
            break
    width = max(next((p.width for p in parse(options.chapter_file_format)
                      if isinstance(p, Token) and p.name == "Chapter"), 0), 1)
    if highest is not None and highest >= 10 ** width:
        pair = [(n, render(SAMPLE_SERIES, ChapterInfo(n), options)) for n in (10 ** width - 1, 10 ** width)]
        if sort_inversions(pair):
            out.append(f"Readers and file browsers that sort by plain file name put {_stem(pair[1][1])} "
                       f"before {_stem(pair[0][1])}.")
    return out


def examples(options: Options = DEFAULTS, series: SeriesNames | None = None, chapters=None,
             highest: float | None = None) -> dict:
    """The live preview under the naming fields: the folder name and a few
    chapter file names these options give, with sort-order warnings; or
    only the errors, when the options cannot be used. series and chapters
    are real ones from the library when there are any (pick_examples), else
    built-in samples."""
    options = as_options(options)
    errors = check_options(options)
    if errors:
        return {"folder": None, "chapters": [], "errors": errors, "warnings": []}
    series = series or SAMPLE_SERIES
    chapters = pick_examples(chapters) if chapters else list(SAMPLE_CHAPTERS)
    return {"folder": render(series, None, options),
            "chapters": [render(series, c, options) for c in chapters],
            "errors": [],
            "warnings": sort_warnings(options, highest)}

"""Title normalisation and the strict match rules.

The one lesson from every wrong download so far: fuzzy matching loses to
anthologies, promos and spin-offs, because those share most of the words.
So a source's title is accepted only when it equals one of the series'
known titles after normalisation. Anything looser is reported, never used.

Titles come from scraped sites, metadata providers and user input, so every
function here is linear in its input and caps it first (MAX_TITLE): a
50 KB "title" must cost microseconds, not a GIL-holding regex backtrack.
"""
import difflib
import re
import unicodedata

# Typographic apostrophes and quotes, read as their plain forms. Titles come
# with either: AniList's search finds nothing for "It’s Mine" (a Suwayomi
# folder name) but its series first for "It's Mine".
_QUOTE_FORMS = {**dict.fromkeys("’‘‛ʼ′＇´`", "'"), **dict.fromkeys("“”„‟″", '"')}
_QUOTES = str.maketrans(_QUOTE_FORMS)
_NORM = str.maketrans({**_QUOTE_FORMS, "…": ""})

MAX_TITLE = 500          # chars; Suwayomi itself truncates titles to 512
MAX_KNOWN_TITLES = 200   # known titles compared per hit (AniList/MangaDex list ~50 at most)
CLOSE = 0.8              # difflib ratio of two normalised titles: 'One Peice' ~ 'One Piece' (0.89)
_OPENERS, _CLOSERS = "([", ")]"
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]+")


def oneline(text, limit: int = 200) -> str:
    """Untrusted text made safe for one log line or event message: control
    characters (newlines included, so a scraped title cannot forge log
    lines) become a space, and it is cut to `limit` characters."""
    t = _CONTROL.sub(" ", str(text if text is not None else "")[:limit * 2 + 16])
    return t if len(t) <= limit else t[:limit - 1] + "…"


def _cap(title: str | None) -> str:
    return (title or "")[:MAX_TITLE]


def plain_quotes(text: str) -> str:
    """Typographic apostrophes and quotes as their plain forms (’ ‘ ‛ ʼ ′ ＇ ´ `
    as ', “ ” „ ‟ ″ as "): 'It’s Mine' -> "It's Mine". Linear in the text."""
    return text.translate(_QUOTES)


def norm(text: str | None) -> str:
    """Lower-case, fold accents, straighten quotes, drop punctuation.
    '_' counts as punctuation too: Suwayomi writes folder names with ':' and
    '?' replaced by '_'. '&' reads as 'and'."""
    t = (text or "").translate(_NORM).replace("&", " and ")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^\w\s]|_", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _trailing_group(title: str) -> int | None:
    r"""Index of the '(' or '[' that opens the title's trailing group, or None.
    Same result as the regex [([][^)\]]*[)\]]\s*$ (the leftmost opener after
    the last inner closer), found with rfind/scan so it is linear: the regex
    backtracks quadratically on long runs of '(' or whitespace."""
    t = title.rstrip()
    if not t or t[-1] not in _CLOSERS:
        return None
    end = len(t) - 1
    inner = max(t.rfind(c, 0, end) for c in _CLOSERS)    # the group holds no closer
    for i in range(inner + 1, end):
        if t[i] in _OPENERS:
            return i
    return None


def disambiguator(title: str) -> str | None:
    """The trailing '(...)' of a title, if any: 'Wind Breaker (NII Satoru)' -> 'NII Satoru'."""
    t = _cap(title).rstrip()
    i = _trailing_group(t)
    if i is None or i + 1 >= len(t) - 1:                  # no group, or an empty '()'
        return None
    return t[i + 1:-1].strip()


def strip_disambiguator(title: str) -> str:
    """'Perfect World (Rie Aruga)' -> 'Perfect World'. Only a trailing group."""
    t = _cap(title)
    i = _trailing_group(t)
    return (t[:i] if i is not None else t).strip()


# Match levels. Lower is better; only EXACT and EXACT_BASE are accepted.
EXACT = 0        # normalised title equals a known title
EXACT_BASE = 1   # equal once a trailing "(...)" disambiguator is removed
NONE = 9

ACCEPTED = (EXACT, EXACT_BASE)


def match_level(candidate: str | None, known_titles: list[str]) -> tuple[int, str | None]:
    """How well a source's title matches the series. Returns (level, matched).
    Both sides are capped (MAX_TITLE chars, MAX_KNOWN_TITLES titles): the
    candidate is scraped, the known titles may be user-typed aliases."""
    candidate = _cap(candidate)
    c = norm(candidate)
    if not c:
        return NONE, None
    known = {norm(_cap(t)): t for t in known_titles[:MAX_KNOWN_TITLES] if t}
    if c in known:
        return EXACT, known[c]
    cb = norm(strip_disambiguator(candidate))
    if cb and cb in known:
        return EXACT_BASE, known[cb]
    for _k, original in known.items():
        kb = norm(strip_disambiguator(original))
        if kb and kb == c:
            return EXACT_BASE, original
    return NONE, None


_NO_AUTHOR = {"unknown", "n a", "none", "anonymous", "various", "author"}


def name_tokens(name: str | None) -> set[str]:
    """Surname/given-name tokens, order-independent, for author comparison.
    Placeholders sources use when they have no author yield no tokens."""
    n = norm(name)
    if not n or n in _NO_AUTHOR:
        return set()
    return {t for t in n.split() if len(t) > 1}


# Author agreement. Sources are sloppy about authors (romanisation, native
# script, artist vs writer), so this demotes rather than rejects.
AUTHOR_AGREE = 0
AUTHOR_UNKNOWN = 1
AUTHOR_DIFFER = 2


def author_level(source_author: str | None, series_authors: list[str]) -> int:
    src = name_tokens(source_author)
    if not src or not series_authors:
        return AUTHOR_UNKNOWN
    for a in series_authors:
        if src & name_tokens(a):
            return AUTHOR_AGREE
    return AUTHOR_DIFFER


def query_score(title: str | None, query: str) -> int:
    """Rank AniList candidates against what the user typed. Lower is better."""
    t, q = norm(title), norm(query)
    if not t or not q:
        return 9
    if t == q:
        return 0
    if t.startswith(q) or q.startswith(t):
        return 1
    if q in t:
        return 2
    qw, tw = set(q.split()), set(t.split())
    return 3 + len(qw - tw)


def close_title(text: str | None, titles) -> bool:
    """Whether `text` is nearly one of `titles` once both are normalised: a
    misspelling ('One Peice', 'Naurto'), never a title with words added
    ('One Piece hunter2' is 0.69). Both sides are capped (MAX_TITLE,
    MAX_KNOWN_TITLES), and the cheap upper bounds of the ratio are tried
    first: most titles differ in length too much to be close."""
    t = norm(_cap(text))
    if not t:
        return False
    for title in list(titles)[:MAX_KNOWN_TITLES]:
        k = norm(_cap(title))
        if not k:
            continue
        m = difflib.SequenceMatcher(None, t, k, autojunk=False)
        if m.real_quick_ratio() >= CLOSE and m.quick_ratio() >= CLOSE and m.ratio() >= CLOSE:
            return True
    return False

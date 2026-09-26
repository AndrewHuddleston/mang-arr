"""Title normalisation and the strict match rules.

The one lesson from every wrong download so far: fuzzy matching loses to
anthologies, promos and spin-offs, because those share most of the words.
So a source's title is accepted only when it equals one of the series'
known titles after normalisation. Anything looser is reported, never used.
"""
import re
import unicodedata

_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "…": ""})
_PAREN_SUFFIX = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")


def norm(text: str | None) -> str:
    """Lower-case, fold accents, straighten quotes, drop punctuation.
    '_' counts as punctuation too: Suwayomi writes folder names with ':' and
    '?' replaced by '_'. '&' reads as 'and'."""
    t = (text or "").translate(_QUOTES).replace("&", " and ")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^\w\s]|_", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def disambiguator(title: str) -> str | None:
    """The trailing '(...)' of a title, if any: 'Wind Breaker (NII Satoru)' -> 'NII Satoru'."""
    m = re.search(r"[\(\[]([^\)\]]+)[\)\]]\s*$", title or "")
    return m.group(1).strip() if m else None


def strip_disambiguator(title: str) -> str:
    """'Perfect World (Rie Aruga)' -> 'Perfect World'. Only a trailing group."""
    return _PAREN_SUFFIX.sub("", title or "").strip()


# Match levels. Lower is better; only EXACT and EXACT_BASE are accepted.
EXACT = 0        # normalised title equals a known title
EXACT_BASE = 1   # equal once a trailing "(...)" disambiguator is removed
NONE = 9

ACCEPTED = (EXACT, EXACT_BASE)


def match_level(candidate: str | None, known_titles: list[str]) -> tuple[int, str | None]:
    """How well a source's title matches the series. Returns (level, matched)."""
    c = norm(candidate)
    if not c:
        return NONE, None
    known = {norm(t): t for t in known_titles if t}
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

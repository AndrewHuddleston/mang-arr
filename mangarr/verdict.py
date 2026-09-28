"""Is the chapter a series is stuck behind part of the story?

With chapters downloaded in order, a chapter that failed on every source
listing it holds back every later chapter of its series. Most such blockers
are fractional chapters (7.2, 1.3, 171.14) that only an aggregator site
lists: a notice, an extra, a duplicate, or a piece of a chapter the site
split in two. This module judges which from deterministic signals only, and
gives its evidence as plain sentences the user can check:

  * the chapter's name on each source: side story, spin-off, omake,
    afterword, extra (and romanised or native forms) -> a side story; bonus
    and special -> the same, less surely (ordinary words more often); "part
    2" -> the rest of a chapter; any other title -> a chapter of its own,
    which outweighs a side story's name elsewhere. Chapter N's own title
    (words like "Extra" in it too) marks the site's split of chapter N: no
    side story, and a real title against one's name on another source. A
    side-story word the series' whole chapters carry too (every chapter of
    a spin-off called "... Gaiden Chapter N"), in your copies' names or on
    a site that lists this one (yours are often another site's "Chapter N",
    or unnamed), is the series' word and decides nothing. These words
    decide only for a chapter numbered between
    the story's (7.5): a whole number is the story's own numbering. Notice
    words ("Hiatus Notice", 公告) are shown but never decide anything: "The
    Announcement" is a chapter title too.
  * MangaDex's English chapter list (mangadex.english_chapters): it has the
    whole chapter N, goes on with N+1 and lacks this number -> a site's own
    split or duplicate, already covered when you have N at its length; it
    lists this number under a title, or at a chapter's length -> a chapter
    of its own; it lists another chapter between N and N+1 that you lack
    (by any name but a side story's own words: "Special" and "Extra" are
    titles too) -> this may be that one under another number, whether or
    not it has N. A list that ends or skips ahead before the number says
    nothing about it.
  * page counts: your N much shorter than MangaDex's N or the series' usual
    chapter -> the site split N, and this is probably the rest of it; the
    whole chapter only at 85-115% of those (longer, it may hold more). The
    pieces of N you have count with it; one MangaDex lists as a chapter of
    its own is compared with MangaDex's copy of it, never added to yours
    against MangaDex's N alone
  * position: after the last chapter of a finished series, or numbered .5
    -> hints shown with the other signs that never decide (a last chapter
    split in two looks just the same; the last whole chapter itself, even
    one called Epilogue, is never after the end)

When the signals disagree, or there are too few, the verdict is "unknown".
Only side_story and covered suggest skipping, as advice with the evidence
shown, and only a HIGH-confidence one may be skipped automatically:

  * side_story: a fractional chapter that a site listing it names a side
    story, spin-off, afterword, omake or extra (not the series' own word),
    with no title of its own on any other source or on MangaDex (nor a
    notice's name, which may be one, nor chapter N's title), and no chapter
    between N and N+1 on MangaDex you lack that it may be
  * covered: MangaDex has N and goes on with N+1 without this number or
    another chapter between them you lack (but a side story); your N (with
    the pieces of it you have) is 85-115% of MangaDex's N (with the same
    pieces); and your N+1 is from a source that does not list this number
    either, so the numbering after N is not shifted by it (a site's 7.1
    can be MangaDex's 8)

Any other verdict is LOW: advice for the "Skip it" button only, with the
reasons it is not certain among its evidence.

Chapter names are scraped text: every check here is linear in its input,
which is capped first (matching.MAX_TITLE). Numbers and page counts that are
not plain finite numbers count as not known.
"""
import math
import re
import statistics
from dataclasses import dataclass, field

from . import config, mangadex
from .matching import MAX_TITLE, norm, oneline, plain_quotes
from .model import Series

SHORT = 0.6         # your N at less than this share of the usual chapter length is only part of it
SAME = 0.85         # ... from this share of MangaDex's copy of N (or the usual length) the whole one ...
LONG = 1.15         # ... up to this share; longer, it may hold more than chapter N
MIN_SAMPLE = 5      # chapters with a known page count needed before "the usual length" means anything
MAX_NUMBER = 1e6    # chapter numbers and page counts from this on are not plain numbers

KINDS = ("side_story", "covered", "rest_of_chapter", "unknown")
HIGH, LOW = "high", "low"   # confidence; only a HIGH side_story or covered verdict may be skipped automatically


@dataclass(slots=True)
class Chapter:
    """One chapter row of the series (db.chapters)."""
    number: float
    status: str                     # wanted | have | failed | junk | unavailable | ignored
    name: str | None = None
    source: str | None = None
    pages: int | None = None        # when known

    @classmethod
    def from_row(cls, row) -> "Chapter":
        return cls(row["number"], row["status"], row["name"], row["source_name"], row["pages"])


@dataclass
class Blocker:
    """The chapter the series is stuck behind."""
    number: float
    # {source: its name there} for every source listing it: "covered" is certain only when the source of
    # chapter N+1 is not among them
    names: dict[str, str | None] = field(default_factory=dict)
    reason: str | None = None       # why it failed; for the note, the verdict does not depend on it
    # {source: {number: name}} of the sources listing it: their whole chapters named with a side-story or extra
    # word (site_words), so a word that site gives all its chapters is the series', not this chapter's
    wholes: dict = field(default_factory=dict)


@dataclass
class Verdict:
    kind: str                       # one of KINDS
    headline: str
    evidence: list[str]
    confidence: str = LOW           # HIGH or LOW; only ever HIGH for side_story or covered

    @property
    def skippable(self) -> bool:
        """Whether it suggests skipping (side_story or covered, never unknown
        or rest_of_chapter): advice, the user decides."""
        return self.kind in ("side_story", "covered")

    @property
    def auto_skip(self) -> bool:
        """Whether automatic skipping may act on it: skippable, and HIGH."""
        return self.skippable and self.confidence == HIGH


# -- what a chapter's name says ---------------------------------------------

# "Vol.1 Chapter 7.2: ", "Ch. 171.01 ", "Episode 3 - ", "#12 ": the source's numbering, not a title
# (but not the 1 of "1st Anniversary Special")
_PREFIX = re.compile(r"^\s*(?:vol(?:ume)?\.?\s*\d+\s*[,:.-]?\s*)?(?:(?:ch(?:apter)?|ep(?:isode)?)\.?\s*|#\s*)?"
                     r"\d+(?:\.\d+)?(?!\w)\s*(?:[:.\-–—]\s*)?")
_JUNK = re.compile(r"お知らせ|休載|공지|휴재|公告")     # notices in the native scripts (公告 is in titles too)
# "Notice", "announcement" and the like are ordinary words too ("Notice Me,
# Senpai", "The Engagement Announcement"), so a name reads as a notice only
# when a part of it is made of the words notices use ("Hiatus Notice", "We
# are recruiting!", "Release postponed") and its other parts are notices,
# extras or numbers too ("Twitter Extra - Update Schedule"). Even then it
# only hints: "The Announcement" and "Hiatus" are chapter titles as well.
_NOTICE = {"notice", "notices", "hiatus", "announcement", "announcements", "recruiting", "recruitment",
           "postponed", "postponement", "schedule"}
_NOTICE_TOO = {"a", "an", "the", "we", "re", "are", "is", "s", "on", "will", "be", "our", "of", "for", "and", "to",
               "about", "this", "important", "official", "update", "updates", "release", "chapter", "chapters",
               "series", "manga", "next", "week", "weeks", "month", "months", "until", "short", "temporary",
               "break", "delay", "delayed", "news", "info", "status", "group", "team", "staff", "scanlation",
               "translator", "translators", "translation", "new", "members", "help", "wanted", "needed", "discord",
               "twitter", "author"}
_SIDE = re.compile(r"\b(?:side[\s-]*stor(?:y|ies)|spin[\s-]*offs?|omake|afterwords?|atogaki|gaiden"
                   r"|bangai(?:[\s-]*hen)?|fanwai|oejeon)\b|番外|外伝|外传|おまけ|あとがき|외전|번외")
# every _SIDE and _LOOSE word starts with one of these: a name without any has no mark (_marks), cheaply
_MARKLESS = re.compile(r"side|spin|omake|afterword|atogaki|gaiden|bangai|fanwai|oejeon|extra|bonus|special"
                       r"|番外|外伝|外传|おまけ|あとがき|외전|번외")
_EPILOGUE = re.compile(r"\bepilogue\b|エピローグ|에필로그")
_PART = re.compile(r"\b(?:part|pt)\.?\s*(?:[2-9]|ii|iii|iv|two|three|b)\b|\bsecond\s+half\b"
                   r"|\(\s*[2-9]\s*/\s*[2-9]\s*\)|\bcont(?:inued|\.)")
# "Extra", "Bonus" and "Special" are also ordinary words ("Extra Innings",
# "Special Training", "Just an Extra", "Nothing Special"), so they count only
# as a whole part of the name, before a number or a word like "chapter", or
# at its end after only words that say when or where it appeared ("Christmas
# Special", "Twitter Extra", "Volume 3 Bonus").
_LOOSE = {"extra", "extras", "bonus", "special", "specials"}
_AFTER_LOOSE = {"chapter", "chapters", "ch", "episode", "episodes", "ep", "story", "stories", "edition", "comic",
                "comics", "manga", "page", "pages", "short", "shorts", "part", "illustration", "illustrations"}
_BEFORE_LOOSE = {"christmas", "xmas", "halloween", "valentine", "valentines", "white", "day", "new", "year",
                 "years", "end", "s", "summer", "winter", "spring", "autumn", "holiday", "holidays", "easter",
                 "april", "fools", "golden", "week", "birthday", "anniversary", "celebration", "commemoration",
                 "commemorative", "milestone", "million", "views", "followers", "popularity", "poll", "character",
                 "anime", "adaptation", "release", "launch", "completion", "finale", "season", "volume", "vol",
                 "tankobon", "tankoubon", "book", "print", "magazine", "anthology", "store", "shop", "preorder",
                 "twitter", "pixiv", "fanbox", "patreon", "web", "online", "color", "colour", "cover", "koma",
                 "yonkoma", "mini"}
_NUMBER = re.compile(r"\d+(?:st|nd|rd|th)?")
_PARTS = re.compile(r"\s-\s|[:|/()\[\]{}–—~,;!?]")
_WORDS = re.compile(r"[^\W_]+")


def _clean(name: str | None, series: Series) -> str:
    """The name in lower case without the series' own titles ("The Novel's
    Extra Chapter 45") and without the source's numbering in front."""
    t = plain_quotes(oneline(name, MAX_TITLE)).lower()
    for title in series.titles:
        if len(title) >= 3:
            t = t.replace(plain_quotes(title[:MAX_TITLE]).lower(), " ")
    return _PREFIX.sub("", t, count=1).strip()


def _notice(words: list[str]) -> bool:
    """Whether one part of a name (its words) is a notice's."""
    return any(w in _NOTICE for w in words) and all(w in _NOTICE or w in _NOTICE_TOO or w.isdigit() for w in words)


def _loose(words: list[str]) -> str | None:
    """The extra, bonus or special word that marks one part of a name (its
    words) as one, if any."""
    if words[0] in _LOOSE and (len(words) == 1 or words[1].isdigit() or words[1] in _AFTER_LOOSE):
        return words[0]
    if words[-1] in _LOOSE and all(w in _BEFORE_LOOSE or _NUMBER.fullmatch(w) for w in words[:-1]):
        return words[-1]
    return None


def sign(name: str | None, series: Series) -> str | None:
    """What a chapter's name says it is: 'junk' (a notice), 'side' (a side
    story, spin-off, omake or afterword), 'extra', 'bonus' (a bonus or
    special: more often ordinary words than 'extra' is), 'epilogue', 'part'
    (the next part of a chapter), or None: a title, or nothing beyond the
    source's numbering (_clean() is then empty)."""
    t = _clean(name, series)
    if not t:
        return None
    parts = [(p, w) for p in _PARTS.split(t) if (w := _WORDS.findall(p))]
    if _JUNK.search(t) or (any(_notice(w) for _, w in parts) and all(
            _notice(w) or _loose(w) or _SIDE.search(p) or all(x.isdigit() for x in w) for p, w in parts)):
        return "junk"
    if _SIDE.search(t):
        return "side"
    loose = {x for _, w in parts if (x := _loose(w))}
    if loose:
        return "extra" if loose & {"extra", "extras"} else "bonus"
    if _EPILOGUE.search(t):
        return "epilogue"
    if _PART.search(t):
        return "part"
    return None


def _marks(t: str) -> set[str]:
    """The side-story, extra, bonus or special words in a cleaned name, each
    as the first four letters of its words ('side' for "side stories",
    'gaid', 'extr', 'spec'), so the forms of one word are one mark."""
    out = {"".join(_WORDS.findall(m.group(0)))[:4] for m in _SIDE.finditer(t)}
    for p in _PARTS.split(t):
        w = _WORDS.findall(p)
        if w and (x := _loose(w)):
            out.add(x[:4])
    return out - {""}


SERIES_WORD = 2     # whole chapters named with a side-story or extra word that make it the series' own word
SITE_WORDS = 3      # a site's whole chapters kept per such word (site_words), the nearest to the blocker first


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _series_word(name: str | None, series: Series, rows: dict, b: float, wholes: dict | None = None) -> str | None:
    """When the side-story or extra words of this name are in the names of
    the series' whole chapters too (SERIES_WORD of them, or chapter N): a
    spin-off whose every chapter is "... Gaiden Chapter 12", a chapter N
    called "Extra? No, I'm the Protagonist!". Then they are the series'
    words and say nothing about this chapter: the clause that says so
    ('39 of the series' whole chapters ...'); else None. Your chapters are
    named by whichever site each came from, or not at all, so the whole
    chapters of the sites listing this one count too (wholes: {source:
    {number: name}}, as site_words keeps them). Only the names that hold
    the word's first letters are cleaned and read."""
    marks = _marks(_clean(name, series))
    if not marks:
        return None
    whole = float(math.floor(b))

    def carry(pairs) -> dict[str, set]:
        found: dict[str, set] = {m: set() for m in marks}
        for n, text in pairs:
            if n == b or n != int(n) or not isinstance(text, str) or not text:
                continue
            low = text.lower()
            hits = [m for m in marks if m in low]
            if hits:
                theirs = _marks(_clean(text, series))
                for m in hits:
                    if m in theirs:
                        found[m].add(n)
        return found

    def enough(found: dict[str, set]) -> bool:
        return all(len(ns) >= SERIES_WORD or whole in ns for ns in found.values())

    mine = carry((n, c.name) for n, c in rows.items())
    if enough(mine):
        ns = sorted(set().union(*mine.values()))
        if len(ns) == 1:
            return f"your chapter {_g(ns[0])} has that word in its name too ({_quote(rows[ns[0]].name)})"
        return (f"{len(ns)} of the series' whole chapters have that word in their names too (like "
                f"{_quote(rows[ns[-1]].name)})")
    sites = {src: carry(got.items()) for src, got in (wholes or {}).items()}
    if not enough({m: mine[m].union(*(f[m] for f in sites.values())) for m in marks}):
        return None
    hits = sorted((abs(n - b), n, src) for src, f in sites.items() for n in set().union(*f.values()))
    _, n, src = hits[0]
    where = _and([oneline(k, 60) for k in dict.fromkeys(src for _, _, src in hits)])
    if len({n for _, n, _ in hits} | set().union(*mine.values())) == 1:
        return f"chapter {_g(n)} on {where} has that word in its name too ({_quote(wholes[src][n])})"
    return f"whole chapters on {where} have that word in their names too (like {_quote(wholes[src][n])})"


def site_words(chapters, series: Series, b: float) -> dict[float, str]:
    """Of one site's chapters ((number, name) pairs, as its list has them),
    the whole ones named with a side-story or extra word: SITE_WORDS per
    word, the nearest to the blocker b first. What the verdict reads, with
    your own chapters' names, to tell a word the site gives every chapter
    (a spin-off's "... Gaiden Chapter 12") from this chapter's own word
    (Blocker.wholes). Every name is capped first (matching.MAX_TITLE)."""
    b = _num(b)
    if b is None:
        return {}
    marked = []
    for n, name in chapters:
        n = _num(n)
        if n is None or n != int(n) or not isinstance(name, str) or not name:
            continue
        name = oneline(name, MAX_TITLE)
        if _MARKLESS.search(name.lower()) and (marks := _marks(_clean(name, series))):
            marked.append((abs(n - b), n, name, marks))
    out: dict[float, str] = {}
    kept: dict[str, int] = {}
    for _, n, name, marks in sorted(marked, key=lambda x: x[:2]):
        if n not in out and any(kept.get(m, 0) < SITE_WORDS for m in marks):
            out[n] = name
            for m in marks:
                kept[m] = kept.get(m, 0) + 1
    return out


def _wholes(blocker: "Blocker") -> dict[str, dict[float, str]]:
    """blocker.wholes with its sources, numbers and names checked (kept as
    JSON, its numbers are text)."""
    out: dict[str, dict[float, str]] = {}
    for src, got in (blocker.wholes.items() if isinstance(blocker.wholes, dict) else ()):
        if not isinstance(src, str) or not isinstance(got, dict):
            continue
        keep = {}
        for k, name in got.items():
            try:
                n = _num(float(k)) if not isinstance(k, bool) else None
            except (TypeError, ValueError):
                n = None
            if n is not None and n == int(n) and isinstance(name, str) and name:
                keep[n] = oneline(name, MAX_TITLE)
        if keep:
            out[src] = keep
    return out


def _says(name: str | None, pages: int | None, series: Series, min_pages: int) -> str | None:
    """sign() of one copy of a chapter: a notice's name on a copy with at
    least min_pages pages is a chapter's title after all."""
    s = sign(name, series) if name else None
    return None if s == "junk" and pages is not None and pages >= min_pages else s


# -- evidence -----------------------------------------------------------------

def _num(x) -> float | None:
    """x as a chapter number (rounded as the database keeps it), or None
    when it is not a finite number from 0 up to MAX_NUMBER."""
    if type(x) is float:                    # what the database gives: checked with two comparisons (NaN fails)
        return (x if x.is_integer() else round(x, 4)) if 0 <= x < MAX_NUMBER else None
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return round(float(x), 4) if 0 <= x < MAX_NUMBER else None


def _count(x) -> int | None:
    """x as a page count, or None (not known) when it is not a finite number
    from 1 up to MAX_NUMBER."""
    if x is None:
        return None
    if type(x) is int:
        return x if 1 <= x < MAX_NUMBER else None
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return int(x) if 1 <= x < MAX_NUMBER else None


def _g(n: float) -> str:
    return f"{n:g}"


def _join(numbers) -> str:
    """'7', '7 and 7.5', '7, 7.1 and 7.5', '171, 171.01, ... and 9 more'."""
    items = [_g(n) for n in numbers]
    if len(items) > 6:
        return f"{', '.join(items[:5])} and {len(items) - 5} more"
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _pages(n: int) -> str:
    return f"{n} page" if n == 1 else f"{n} pages"


def _quote(text: str | None) -> str:
    return '"' + oneline(text, 80) + '"'


def _md_says(c: "mangadex.MdChapter") -> str:
    out = f"MangaDex lists {_g(c.number)}" + (f" as {_quote(c.title)}" if c.title else "")
    return out + (f" ({_pages(c.pages)})" if c.pages else "")


def _md_near(md: "mangadex.ChapterList | None") -> dict:
    """md.near with its numbers, titles and page counts checked the way the
    chapter rows are."""
    out = {}
    for n, c in (md.near if md is not None and isinstance(md.near, dict) else {}).items():
        k = _num(n)
        if k is not None and isinstance(c, mangadex.MdChapter):
            out[k] = mangadex.MdChapter(k, oneline(c.title, 200) if isinstance(c.title, str) and c.title else None,
                                        _count(c.pages))
    return out


def _length(whole: float, b: float, rows: dict, near: dict) -> tuple[str, str, bool]:
    """Is your copy of chapter N (with any pieces of it you have below the
    blocker, 7 and 7.1 for 7.2) the whole chapter? ('normal' | 'short' |
    'long' | 'unclear' | 'unknown', the sentence that says why, whether it
    was compared with MangaDex's copy of N).

    A piece MangaDex lists as a chapter of its own (its 7.1) is compared
    with MangaDex's copy of it: your 7 and 7.1 with MangaDex's 7 and 7.1,
    never your 7 and 7.1 with MangaDex's 7 alone, which a short 7 would
    pass. Against the usual chapter such a piece does not count."""
    base = rows[whole]
    pieces = sorted(n for n, c in rows.items() if whole < n < b and c.status == "have" and c.pages)
    if not base.pages:
        return ("unknown", f"The page count of your chapter {_g(whole)} is not known, so its length cannot be "
                           f"compared.", False)
    md_base = near.get(whole)
    listed = [n for n in pieces if n in near]           # chapters of their own on MangaDex
    if md_base is not None and md_base.pages and all(near[n].pages for n in listed):
        ours = base.pages + sum(rows[n].pages for n in pieces)
        theirs = md_base.pages + sum(near[n].pages for n in listed)
        label = (f"Your chapters {_join([whole, *pieces])} have {_pages(ours)} together" if pieces
                 else f"Your chapter {_g(whole)} has {_pages(ours)}")
        what = f"MangaDex's chapters {_join([whole, *listed])}" if listed else f"MangaDex's chapter {_g(whole)}"
        has = "have" if listed else "has"
        if ours > LONG * theirs:
            return "long", (f"{label} but {what} {has} only {theirs}: yours holds more than chapter "
                            f"{_g(whole)}."), True
        if ours >= SAME * theirs:
            return "normal", f"{label}, like {what} ({theirs}).", True
        if ours < SHORT * theirs:
            return "short", f"{label} but {what} {has} {theirs}: yours is probably only part of it.", True
        return "unclear", f"{label} and {what} {has} {theirs}: close, but not clearly the same.", True
    split = [n for n in pieces if n not in near]        # the site's own pieces of N
    ours = base.pages + sum(rows[n].pages for n in split)
    label = (f"Your chapters {_join([whole, *split])} have {_pages(ours)} together" if split
             else f"Your chapter {_g(whole)} has {_pages(ours)}")
    usual = [c.pages for n, c in rows.items() if n == int(n) and n != whole and c.status == "have" and c.pages]
    if len(usual) < MIN_SAMPLE:
        return "unknown", (f"Too few chapters of this series have a known page count to tell whether your chapter "
                           f"{_g(whole)} is complete."), False
    median = statistics.median(usual)
    if ours > LONG * median:
        return "long", (f"{label} while this series' chapters usually have about {round(median)}: more than one "
                        f"chapter's length."), False
    if ours >= SAME * median:
        return "normal", f"{label}, a usual length for this series (about {round(median)}).", False
    if ours < SHORT * median:
        return "short", (f"{label} while this series' chapters usually have about {round(median)}: the site "
                         f"probably split chapter {_g(whole)} in two."), False
    return "unclear", (f"{label} while this series' chapters usually have about {round(median)}: not clearly "
                       f"the whole chapter."), False


def _last_whole(series: Series, rows: dict, b: float) -> float:
    """The last whole chapter of the series, as far as anything but the
    blocker itself says."""
    wholes = [n for n, c in rows.items() if n == int(n) and n != b and c.status != "junk"]
    return max([*wholes, _num(series.chapters) or 0.0])


def classify(series: Series, blocker: Blocker, chapters, md: "mangadex.ChapterList | None" = None,
             min_pages: int = config.MIN_PAGES) -> Verdict:
    """The verdict on the chapter `blocker` from the series' chapter rows
    (Chapter, every row of the series) and, when known, MangaDex's English
    chapters around it (mangadex.english_chapters; None: not checked).
    A copy with at least min_pages pages (the setting of that name) is a
    chapter whatever its name; one with fewer is not taken for a notice by
    that alone (a webtoon's stitched strips make few pages too)."""
    b = _num(blocker.number)
    if b is None:
        return Verdict("unknown", "Unknown: its number is not a plain chapter number",
                       [f"It is numbered {_quote(str(blocker.number))}, which cannot be compared with other "
                        f"chapters."])
    whole = float(math.floor(b))
    fractional = b != whole
    rows: dict[float, Chapter] = {}
    for c in chapters:
        n = _num(c.number)
        if n is not None:
            p = _count(c.pages)
            rows[n] = c if n == c.number and p == c.pages and type(c) is Chapter else \
                Chapter(n, c.status, c.name, c.source, p)
    have = {n for n, c in rows.items() if c.status == "have"}
    wholes = _wholes(blocker)
    near = _md_near(md)
    following = _num(md.following) if md is not None else None
    highest = _num(md.highest) if md is not None else None

    side: list[str] = []        # it is not story: a side story or extra (decides)
    rest: list[str] = []        # it is (the rest of) story you do not have
    story: list[str] = []       # it is a chapter of its own, so not a site's split or duplicate, nor an extra
    listed: list[str] = []      # MangaDex has it as a chapter: not a split or duplicate, but maybe an extra
    support: list[str] = []     # agrees with "not story" but never decides
    notes: list[str] = []       # checked, and says nothing either way
    doubts: list[str] = []      # why a covered verdict is not certain (it is then LOW) ...
    titled: list[str] = []      # ... and why neither a covered nor a side_story one is
    covered: list[str] | None = None        # MangaDex's list shows it is covered by N, if N is whole
    lacking: list[float] = []   # chapters from N to N+1 MangaDex lists that you do not have
    sure = False                # a site listing it names it a side story or extra outright
    loose = epilogue = md_side = md_story = False

    last = _last_whole(series, rows, b)
    # only a number between the story's can come after its end: the last whole
    # chapter itself, even one called "Epilogue", is story
    after_final = series.status == "FINISHED" and fractional and last > 0 and b > last
    undecided = (f"but {_g(b)} is numbered as a chapter of the story, so that alone does not decide it"
                 if not fractional else
                 f"but {_g(b)} does not come after the last chapter of a finished series, so that alone does not "
                 f"decide it")

    mine = rows.get(b)
    listing = {k for k in blocker.names if k}
    if mine is not None and mine.source:
        listing.add(mine.source)
    names = {k: v for k, v in blocker.names.items() if v}
    if not names and mine is not None and mine.name:
        names = {mine.source or "The source": mine.name}
    pages = mine.pages if mine is not None else None
    base = rows.get(whole) if fractional else None
    base_title = norm(_clean(base.name, series)) if base is not None and base.name else ""
    plain, like_base = [], []
    for source, name in names.items():
        who, what = oneline(source, 60), _quote(name)
        s = _says(name, pages, series, min_pages)
        if base_title and _clean(name, series) and norm(_clean(name, series)) == base_title:
            # chapter N's own title (words like "Extra" in it included): the site split chapter N
            like_base.append(f"{who} names it {what}, as your chapter {_g(whole)} is named.")
            continue
        own = _series_word(name, series, rows, b, wholes) if s in ("side", "extra", "bonus") else None
        if own:
            support.append(f"{who} names it {what}, but {own}, so that is the series' word, not this chapter's.")
        elif s in ("side", "extra", "bonus") and fractional:
            side.append(f"{who} names it {what}, which marks a side story or extra.")
            sure, loose = sure or s != "bonus", loose or s == "bonus"
        elif s in ("side", "extra", "bonus"):
            support.append(f"{who} names it {what}, which may mark a side story or extra, {undecided}.")
        elif s == "epilogue" and after_final:
            side.append(f"{who} names it {what}: an epilogue after the last chapter.")
            epilogue = True
        elif s == "epilogue":
            support.append(f"{who} names it {what}, {undecided}.")
        elif s == "part":
            rest.append(f"{who} names it {what}, which reads like the next part of a chapter.")
        elif s == "junk":
            support.append(f"{who} names it {what}, which reads like a notice, but notice words are in chapter "
                           f"titles too, so that never decides it.")
            titled.append(f"{who}'s notice-like name for it could be a chapter's own title.")
        elif not _clean(name, series):
            plain.append(what)
        else:
            story.append(f"{who} names it {what}: a title of its own, so probably a chapter of its own.")
    if plain and len(plain) == len(names):
        notes.append(f"It has no title of its own, only {plain[0]}.")

    if md is None:
        notes.append("This series was added by hand, without an AniList or MangaDex entry, so MangaDex's chapter "
                     "list cannot be looked up." if series.manual else
                     "MangaDex's chapter list was not checked (not reachable, or not looked up yet).")
    elif md.manga_id is None:
        notes.append("No MangaDex entry found under this series' titles links to its AniList entry, so MangaDex's "
                     "chapter list cannot help.")
    elif not md.count:
        notes.append("MangaDex lists no English chapters of this series, so its chapter list cannot help.")
    elif b in near:
        c = near[b]
        s = _says(c.title, c.pages, series, min_pages)
        if (s in ("side", "extra", "bonus") and fractional) or (s == "epilogue" and after_final):
            side.append(f"{_md_says(c)}, which marks a side story or extra.")
            md_side = True
        elif s in ("side", "extra", "bonus", "epilogue"):
            support.append(f"{_md_says(c)}, which may mark a side story or extra, {undecided}.")
        elif s == "part":
            rest.append(f"{_md_says(c)}, which reads like the next part of a chapter.")
        elif s == "junk":
            support.append(f"{_md_says(c)}, which reads like a notice, but notice words are in chapter titles too, "
                           f"so that never decides it.")
            titled.append("MangaDex's notice-like name for it could be a chapter's own title.")
        elif c.title:
            story.append(f"{_md_says(c)}: a chapter of its own there.")
            md_story = True
        elif c.pages is not None and c.pages >= min_pages:
            listed.append(f"{_md_says(c)}: a chapter of its own there.")
        elif c.pages is not None:
            support.append(f"{_md_says(c)}, few for a chapter, but a webtoon's stitched strips make few pages too, "
                           f"so that does not decide it.")
        else:
            notes.append(f"{_md_says(c)} too, without a title.")
    elif fractional and whole in near:
        numbers = sorted(near)
        had = [n for n in numbers if n in have]
        line = f"MangaDex's English chapter list has {'chapter ' * (len(numbers) == 1)}{_join(numbers)} but no {_g(b)}"
        if len(had) == len(numbers):
            line += "; you have " + ("it" if len(had) == 1 else "both" if len(had) == 2 else "all of them")
        elif had:
            line += f"; you have {_join(had)}"
        lines, missing, unsure = [line + "."], [], []
        for n in numbers:
            c = near[n]
            if n == whole or n in have:
                continue
            s = _says(c.title, c.pages, series, min_pages)
            if s == "side" and _series_word(c.title, series, rows, b, wholes):
                s = None                    # the series' word, not the chapter's
            if s == "side":                 # a side story's own words; "extra" and "special" are titles too
                lines.append(f"{_md_says(c)}, an extra.")
            elif s == "junk" or (not c.title and c.pages is not None and c.pages < min_pages):
                lines.append(f"{_md_says(c)}, perhaps a notice.")
                unsure.append(n)
            else:
                missing.append(n)
        # its lack of the number means something only where the list goes on
        # right after N: one that ends, or skips ahead, may just not have got there
        goes_on = following is not None and following <= whole + 1
        if missing:
            lacking = missing
            story += [*lines, f"You do not have {_join(missing)} from that list: {_g(b)} may be the same chapter "
                              f"under another number."]
        elif not goes_on:
            notes += [*lines, f"It skips from {_g(numbers[-1])} to {_g(following)}, so its lack of {_g(b)} says "
                              f"nothing." if following is not None else
                              f"It ends at {_g(highest if highest is not None else numbers[-1])}, so its lack of "
                              f"{_g(b)} says nothing."]
        elif whole in have:
            covered = [*lines, f"It goes on with chapter {_g(following)}, so it does not simply end before {_g(b)}."]
            if unsure:
                doubts.append(f"MangaDex's {_join(unsure)}, which you do not have, could be a chapter rather than a "
                              f"notice.")
        else:
            notes += lines
    elif fractional:
        others = sorted(near)
        notes.append(f"MangaDex's English chapter list has no chapter {_g(whole)}"
                     + (f" (it lists {_join(others)})" if others else "") + ", so it cannot compare.")
        # but a chapter of its own it lists between N and N+1 that you lack may be this one under another number
        for n in others:
            c = near[n]
            s = _says(c.title, c.pages, series, min_pages)
            if s == "side" and _series_word(c.title, series, rows, b, wholes):
                s = None                    # the series' word, not the chapter's
            if whole < n < whole + 1 and n not in have and s not in ("side", "junk") and \
                    (c.title or c.pages is None or c.pages >= min_pages):
                lacking.append(n)
        if lacking:
            story += [f"{_md_says(near[n])}, which you do not have." for n in lacking[:3]]
            story.append(f"{_g(b)} may be {'that chapter' if len(lacking) == 1 else 'one of them'} under another "
                         f"number.")
    elif highest is not None and highest < b:
        notes.append(f"MangaDex's English chapter list ends at {_g(highest)}, before {_g(b)}, so it cannot help.")
    else:
        notes.append(f"MangaDex's English chapter list has no chapter {_g(b)} either.")

    if like_base and (side or md_side):
        story += [f"{line[:-1]}: a real title, so it may be the rest of chapter {_g(whole)}." for line in like_base]
    else:
        support += like_base

    length, length_line, against_md = "unknown", "", False
    if fractional and whole in have:
        length, length_line, against_md = _length(whole, b, rows, near)
        if length == "short":
            rest.append(length_line)
        elif length != "normal":
            notes.append(length_line)

    if after_final:
        support.append(f"The series is finished and its last chapter is {_g(last)}; {_g(b)} comes after it, "
                       f"where epilogues and extras go.")
    if fractional and abs(b - whole - 0.5) < 1e-9:
        support.append("Chapters numbered .5 are often extras, but that alone does not decide it.")
    if length == "normal":
        support.append(length_line)

    # Position and a usual length only agree: the second half of a last
    # chapter split in two comes after the end at a usual length too.
    covers = covered is not None and length == "normal"
    if (side or covers) and (rest or story):
        return Verdict("unknown", "Unknown: the signs disagree",
                       [*side, *(covered or []), *rest, *story, *listed, *notes, *support])
    if rest:
        what = f"the rest of chapter {_g(whole)}" if fractional else "part of the story"
        return Verdict("rest_of_chapter", f"Probably {what} (skipping leaves a gap)", rest)
    if story:
        headline = (f"Unknown: MangaDex lists {_join(lacking)}, which you do not have" if lacking
                    else f"Unknown: MangaDex lists {_g(b)} as a chapter of its own" if md_story
                    else "Unknown: it has a title of its own")
        return Verdict("unknown", headline, [*story, *notes, *support])
    if side:
        unsure = [] if sure else [
            "Only MangaDex's name for it marks it so, not a site that lists it, so that is not certain."
            if md_side and not (loose or epilogue) else
            "\"Bonus\" and \"special\" name ordinary chapters too, so that is not certain." if loose else
            "An epilogue can be the story's own ending, so that is not certain."]
        return Verdict("side_story", "Probably a side story",
                       [*side, *listed, *(covered if covers else []), *support, *unsure, *titled],
                       HIGH if sure and not titled else LOW)
    if listed:
        return Verdict("unknown", f"Unknown: MangaDex lists {_g(b)} as a chapter of its own",
                       [*listed, *notes, *support])
    if covers:
        after = rows.get(whole + 1)
        n1 = _g(whole + 1)
        is_from = "is from" if after is not None and after.status == "have" else "will come from"
        if after is None or after.status not in ("have", "wanted", "failed"):
            doubts.append(f"You have no chapter {n1} to check that your numbering after {_g(whole)} is MangaDex's.")
        elif not after.source:
            doubts.append(f"Where your chapter {n1} {is_from} is not known, so whether your numbering after "
                          f"{_g(whole)} is MangaDex's cannot be checked.")
        elif after.source in listing:
            doubts.append(f"Your chapter {n1} {is_from} {oneline(after.source, 60)}, which lists {_g(b)} too: if "
                          f"{_g(b)} is a chapter of its own there, its numbering after {_g(whole)} is one ahead of "
                          f"MangaDex's.")
        else:
            covered.append(f"{oneline(after.source, 60)}, where your chapter {n1} {is_from}, does not list {_g(b)}.")
        if not against_md:
            doubts.insert(0, f"MangaDex has no page count for its chapter {_g(whole)}, so yours is only compared "
                             f"with this series' usual chapter.")
        return Verdict("covered", f"Probably already covered by chapter {_g(whole)} you have",
                       [*covered, *support, *doubts, *titled], LOW if doubts or titled else HIGH)
    return Verdict("unknown", "Unknown: no clear sign either way", [*(covered or []), *notes, *support])


def judge(series: Series, blocker: Blocker, chapters, fetch: bool = True,
          min_pages: int = config.MIN_PAGES) -> Verdict:
    """classify() with MangaDex's chapter list looked up; with fetch=False
    only an answer already cached is used (no network, never waits): the
    download pass calls it so, and a background job does the lookups."""
    b = _num(blocker.number)
    md = mangadex.english_chapters(series, b, fetch=fetch) if b is not None else None
    return classify(series, blocker, chapters, md, min_pages)

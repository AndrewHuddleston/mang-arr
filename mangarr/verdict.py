"""Is the chapter a series is stuck behind part of the story?

With chapters downloaded in order, a chapter that failed on every source
listing it holds back every later chapter of its series. Most such blockers
are fractional chapters (7.2, 1.3, 171.14) that only an aggregator site
lists: a notice, an extra, a duplicate, or a piece of a chapter the site
split in two. This module judges which from deterministic signals only, and
gives its evidence as plain sentences the user can check:

  * the chapter's name on each source: side story, spin-off, omake,
    afterword (and romanised or native forms) -> a side story; extra, bonus,
    special -> the same, but these are ordinary words too, so only for a
    chapter numbered between the story's (7.5) or after its end; a notice
    ("Hiatus Notice", "Update Schedule") -> not a chapter, unless its copy
    has a chapter's page count; "part 2" -> the rest of a chapter
  * MangaDex's English chapter list (mangadex.english_chapters): it has the
    whole chapter N and not this number -> a site's own split or duplicate,
    already covered when you have N at its normal length; it lists this
    number under an extra's name -> a side story
  * page counts: your N much shorter than MangaDex's N or the series' usual
    chapter -> the site split N, and this is probably the rest of it; only
    about as long as those is the whole chapter
  * position: after the last chapter of a finished series, or numbered .5 ->
    hints shown with the other signs that never decide (a last chapter split
    in two looks just the same)

When the signals disagree, or there are too few, the verdict is "unknown".
Only side_story and covered are ever candidates for skipping.

Chapter names are scraped text: every check here is linear in its input,
which is capped first (matching.MAX_TITLE).
"""
import math
import re
import statistics
from dataclasses import dataclass, field

from . import config, mangadex
from .matching import MAX_TITLE, oneline, plain_quotes
from .model import Series

SHORT = 0.6         # your N at less than this share of the usual chapter length is only part of it
SAME = 0.85         # ... and at this share of MangaDex's copy of N (or the usual length) or more, the whole one
MIN_SAMPLE = 5      # chapters with a known page count needed before "the usual length" means anything

KINDS = ("side_story", "covered", "rest_of_chapter", "unknown")


@dataclass
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
    names: dict[str, str | None] = field(default_factory=dict)  # {source: its name there}, every source listing it
    reason: str | None = None       # why it failed; for the note, the verdict does not depend on it


@dataclass
class Verdict:
    kind: str                       # one of KINDS
    headline: str
    evidence: list[str]

    @property
    def skippable(self) -> bool:
        """Whether automatic skipping may act on it (never unknown or rest_of_chapter)."""
        return self.kind in ("side_story", "covered")


# -- what a chapter's name says ---------------------------------------------

# "Vol.1 Chapter 7.2: ", "Ch. 171.01 ", "Episode 3 - ", "#12 ": the source's numbering, not a title
# (but not the 1 of "1st Anniversary Special")
_PREFIX = re.compile(r"^\s*(?:vol(?:ume)?\.?\s*\d+\s*[,:.-]?\s*)?(?:(?:ch(?:apter)?|ep(?:isode)?)\.?\s*|#\s*)?"
                     r"\d+(?:\.\d+)?(?!\w)\s*(?:[:.\-–—]\s*)?")
_JUNK = re.compile(r"お知らせ|休載|공지|휴재|公告")     # notices in the native scripts, never a title's words
# "Notice", "announcement" and the like are ordinary words too ("Notice Me,
# Senpai", "The Engagement Announcement"), so a name reads as a notice only
# when a part of it is made of the words notices use ("Hiatus Notice", "We
# are recruiting!", "Release postponed") and its other parts are notices,
# extras or numbers too ("Twitter Extra - Update Schedule").
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


def _extra(words: list[str]) -> bool:
    """Whether one part of a name (its words) marks an extra, bonus or special."""
    if words[0] in _LOOSE and (len(words) == 1 or words[1].isdigit() or words[1] in _AFTER_LOOSE):
        return True
    return words[-1] in _LOOSE and all(w in _BEFORE_LOOSE or _NUMBER.fullmatch(w) for w in words[:-1])


def sign(name: str | None, series: Series) -> str | None:
    """What a chapter's name says it is: 'junk' (a notice), 'side' (a side
    story, spin-off, omake or afterword), 'extra' (an extra, bonus or
    special), 'epilogue', 'part' (the next part of a chapter), or None."""
    t = _clean(name, series)
    if not t:
        return None
    parts = [(p, w) for p in _PARTS.split(t) if (w := _WORDS.findall(p))]
    if _JUNK.search(t) or (any(_notice(w) for _, w in parts) and all(
            _notice(w) or _extra(w) or _SIDE.search(p) or all(x.isdigit() for x in w) for p, w in parts)):
        return "junk"
    if _SIDE.search(t):
        return "side"
    if any(_extra(w) for _, w in parts):
        return "extra"
    if _EPILOGUE.search(t):
        return "epilogue"
    if _PART.search(t):
        return "part"
    return None


def _says(name: str | None, pages: int | None, series: Series, min_pages: int) -> str | None:
    """sign() of one copy of a chapter: a notice's name on a copy with at
    least min_pages pages is a chapter's title after all."""
    s = sign(name, series) if name else None
    return None if s == "junk" and pages is not None and pages >= min_pages else s


# -- evidence -----------------------------------------------------------------

def _key(n) -> float:
    return round(float(n), 4)


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


def _length(whole: float, b: float, rows: dict, md_base) -> tuple[str, str | None]:
    """Is your copy of chapter N (with any pieces of it you have below the
    blocker, 7 and 7.1 for 7.2) the whole chapter? ('normal' | 'short' |
    'unclear' | 'unknown', the sentence that says why)."""
    base = rows[whole]
    pieces = [n for n, c in rows.items() if whole < n < b and c.status == "have" and c.pages]
    if not base.pages:
        return "unknown", f"The page count of your chapter {_g(whole)} is not known, so its length cannot be compared."
    ours = base.pages + sum(rows[n].pages for n in pieces)
    label = (f"Your chapters {_join([whole, *pieces])} have {_pages(ours)} together" if pieces
             else f"Your chapter {_g(whole)} has {_pages(ours)}")
    if md_base is not None and md_base.pages:
        theirs = md_base.pages
        if ours >= SAME * theirs:
            return "normal", f"{label}, like MangaDex's chapter {_g(whole)} ({theirs})."
        if ours < SHORT * theirs:
            return "short", (f"{label} but MangaDex's chapter {_g(whole)} has {theirs}: yours is probably only "
                             f"part of it.")
        return "unclear", f"{label} and MangaDex's chapter {_g(whole)} has {theirs}: close, but not clearly the same."
    usual = [c.pages for n, c in rows.items() if n == int(n) and n != whole and c.status == "have" and c.pages]
    if len(usual) < MIN_SAMPLE:
        return "unknown", (f"Too few chapters of this series have a known page count to tell whether your chapter "
                           f"{_g(whole)} is complete.")
    median = statistics.median(usual)
    if ours >= SAME * median:
        return "normal", f"{label}, a usual length for this series (about {round(median)})."
    if ours < SHORT * median:
        return "short", (f"{label} while this series' chapters usually have about {round(median)}: the site "
                         f"probably split chapter {_g(whole)} in two.")
    return "unclear", (f"{label} while this series' chapters usually have about {round(median)}: not clearly "
                       f"the whole chapter.")


def _last_whole(series: Series, rows: dict, b: float) -> float:
    """The last whole chapter of the series, as far as anything says."""
    wholes = [n for n, c in rows.items() if n == int(n) and n != b and c.status != "junk"]
    return max([*wholes, float(series.chapters or 0)])


def classify(series: Series, blocker: Blocker, chapters, md: "mangadex.ChapterList | None" = None,
             min_pages: int = config.MIN_PAGES) -> Verdict:
    """The verdict on the chapter `blocker` from the series' chapter rows
    (Chapter, every row of the series) and, when known, MangaDex's English
    chapters around it (mangadex.english_chapters; None: not checked).
    A copy with fewer than min_pages pages (the setting of that name) reads
    as a notice, and one with at least that many as a chapter, whatever its
    name."""
    b = _key(blocker.number)
    whole = float(math.floor(b))
    fractional = b != whole
    rows = {_key(c.number): c for c in chapters}
    have = {n for n, c in rows.items() if c.status == "have"}

    side: list[str] = []        # it is not story: an extra or side story ...
    junk: list[str] = []        # ... or a notice
    rest: list[str] = []        # it is (the rest of) story you do not have
    story: list[str] = []       # it is a chapter of its own elsewhere, so not a site's split or duplicate
    support: list[str] = []     # agrees with "not story" but never decides
    notes: list[str] = []       # checked, and says nothing either way
    covered: list[str] | None = None        # MangaDex's list shows it is covered by N, if N is whole
    lacking: list[float] = []   # chapters from N to N+1 MangaDex lists that you do not have

    last = _last_whole(series, rows, b)
    after_final = series.status == "FINISHED" and last > 0 and b > last
    # extra, bonus and special are ordinary words too ("Special Chapter" can
    # be story): they decide only off the story's own numbering
    extra_decides = fractional or after_final
    numbered = f"but {_g(b)} is numbered as a chapter of the story, so that alone does not decide it"

    names = {k: v for k, v in blocker.names.items() if v}
    if not names and b in rows and rows[b].name:
        names = {rows[b].source or "The source": rows[b].name}
    pages = rows[b].pages if b in rows else None
    plain = []
    for source, name in names.items():
        who, what = oneline(source, 60), _quote(name)
        s = _says(name, pages, series, min_pages)
        if s == "junk":
            junk.append(f"{who} names it {what}, which reads like a notice, not a chapter.")
        elif s == "side" or (s == "extra" and extra_decides):
            side.append(f"{who} names it {what}, which marks a side story or extra.")
        elif s == "extra":
            support.append(f"{who} names it {what}, which may mark an extra, {numbered}.")
        elif s == "epilogue" and after_final:
            side.append(f"{who} names it {what}: an epilogue after the last chapter.")
        elif s == "part":
            rest.append(f"{who} names it {what}, which reads like the next part of a chapter.")
        elif not _clean(name, series):
            plain.append(what)
        else:
            notes.append(f"{who} names it {what}, which says nothing either way.")
    if plain and not (side or junk or rest or notes):
        notes.append(f"It has no title of its own, only {plain[0]}.")

    if md is None:
        notes.append("MangaDex's chapter list was not checked (not reachable, or not looked up yet).")
    elif md.manga_id is None:
        notes.append("No MangaDex entry found under this series' titles links to its AniList entry, so MangaDex's "
                     "chapter list cannot help.")
    elif not md.count:
        notes.append("MangaDex lists no English chapters of this series, so its chapter list cannot help.")
    elif b in md.near:
        mine = md.near[b]
        s = _says(mine.title, mine.pages, series, min_pages)
        if s == "junk":
            junk.append(f"{_md_says(mine)}: a notice, not a chapter.")
        elif s == "side" or (s == "extra" and extra_decides) or (s == "epilogue" and after_final):
            side.append(f"{_md_says(mine)}, which marks a side story or extra.")
        elif mine.pages is not None and mine.pages < min_pages:
            junk.append(f"{_md_says(mine)}: too few pages for a chapter, so probably a notice.")
        elif s == "extra":
            support.append(f"{_md_says(mine)}, which may mark an extra, {numbered}.")
        elif s == "part":
            rest.append(f"{_md_says(mine)}, which reads like the next part of a chapter.")
        elif mine.title:
            story.append(f"{_md_says(mine)}: a chapter of its own there.")
        else:
            notes.append(f"{_md_says(mine)} too, without a title.")
    elif fractional and whole in md.near:
        listed = sorted(md.near)
        had = [n for n in listed if n in have]
        line = f"MangaDex's English chapter list has {'chapter ' * (len(listed) == 1)}{_join(listed)} but no {_g(b)}"
        if len(had) == len(listed):
            line += "; you have " + ("it" if len(had) == 1 else "both" if len(had) == 2 else "all of them")
        elif had:
            line += f"; you have {_join(had)}"
        lines, missing = [line + "."], []
        for n in listed:
            c = md.near[n]
            if n == whole or n in have:
                continue
            s = _says(c.title, c.pages, series, min_pages)
            if s in ("side", "extra", "junk") or (c.pages is not None and c.pages < min_pages):
                lines.append(f"{_md_says(c)}, an extra or notice.")
            else:
                missing.append(n)
        if missing:
            lacking = missing
            story += [*lines, f"You do not have {_join(missing)} from that list: {_g(b)} may be the same chapter "
                              f"under another number."]
        elif whole in have:
            covered = lines
        else:
            notes += lines
    elif fractional:
        others = sorted(md.near)
        notes.append(f"MangaDex's English chapter list has no chapter {_g(whole)}"
                     + (f" (it lists {_join(others)})" if others else "") + ", so it cannot compare.")
    else:
        notes.append(f"MangaDex's English chapter list has no chapter {_g(b)} either.")

    length, length_line = "unknown", None
    if fractional and whole in have:
        length, length_line = _length(whole, b, rows, md.near.get(whole) if md else None)
        if length == "short":
            rest.append(length_line)
        elif length != "normal":
            notes.append(length_line)

    if after_final:
        support.append(f"The series is finished and its last chapter is {_g(last)}; {_g(b)} comes after it, "
                       f"where epilogues and extras go.")
    half = fractional and abs(b - whole - 0.5) < 1e-9
    if half:
        support.append("Chapters numbered .5 are often extras, but that alone does not decide it.")
    if length == "normal":
        support.append(length_line)

    # Position and a usual length only agree: the second half of a last
    # chapter split in two comes after the end at a usual length too.
    not_story = bool(side or junk)
    covers = covered is not None and length == "normal"
    if (not_story or covers) and (rest or story):
        return Verdict("unknown", "Unknown: the signs disagree",
                       [*side, *junk, *(covered or []), *rest, *story, *support, *notes])
    if rest:
        what = f"the rest of chapter {_g(whole)}" if fractional else "part of the story"
        return Verdict("rest_of_chapter", f"Probably {what} (skipping leaves a gap)", rest)
    if story:
        headline = (f"Unknown: MangaDex lists {_join(lacking)}, which you do not have" if lacking
                    else f"Unknown: MangaDex lists {_g(b)} as a chapter of its own")
        return Verdict("unknown", headline, [*story, *support, *notes])
    if not_story:
        headline = "Probably a notice, not a chapter" if junk and not side else "Probably a side story"
        return Verdict("side_story", headline, [*side, *junk, *(covered if covers else []), *support])
    if covers:
        return Verdict("covered", f"Probably already covered by chapter {_g(whole)} you have",
                       [*covered, length_line])
    return Verdict("unknown", "Unknown: no clear sign either way", [*(covered or []), *support, *notes])


def judge(series: Series, blocker: Blocker, chapters, fetch: bool = True,
          min_pages: int = config.MIN_PAGES) -> Verdict:
    """classify() with MangaDex's chapter list looked up; with fetch=False
    only an answer already cached is used (no network, never waits)."""
    md = mangadex.english_chapters(series, blocker.number, fetch=fetch)
    return classify(series, blocker, chapters, md, min_pages)

"""Turn a series into a per-chapter download plan.

    series (AniList/MangaDex/manual) -> search every source with every title
                                     -> accept only exact title matches
                                     -> drop implausible chapter numbers
                                     -> distrust sources whose length is off
                                     -> union the chapter numbers
                                     -> drop fractional "chapters" with no pages
                                     -> pick a source for each chapter
"""
import logging
import math
import statistics
from dataclasses import dataclass, field

from . import config
from .matching import ACCEPTED, AUTHOR_DIFFER, MAX_TITLE, author_level, match_level, oneline
from .model import Series
from .suwayomi import Chapter, Client, Source, SuwayomiError, SuwayomiUnreachable

log = logging.getLogger(__name__)

# What a source lists is scraped from a website, so it is bounded before it
# is used: no real series has chapter 100000 or 10000 distinct chapters
# (the longest run to about 4000), and nothing needs more than a handful of
# search titles per source.
MAX_CHAPTER_NUMBER = 100_000
OUTLIER_FACTOR = 10          # a top number this many times the next one ...
OUTLIER_MIN_JUMP = 1000      # ... and this far above it is a typo or a date, not a chapter,
OUTLIER_MIN_BELOW = 5        # ... judged only with this many chapters below it (not [1, 1500])
MAX_CHAPTERS_PER_SOURCE = 10_000
MAX_SEARCH_TITLES = 8
MAX_GAP_SPANS = 40
MAX_CHAPTER_NAME = 500


@dataclass
class SourceMatch:
    source: Source
    manga_id: int
    title: str
    author: str | None
    match: int                    # matching.EXACT / EXACT_BASE
    matched_title: str | None
    author_ok: int                # matching.AUTHOR_*
    chapters: list[Chapter] = field(default_factory=list)
    query: str = ""               # which title found it
    note: str = ""                # why it is not used, if it is not
    reliability: float = 0.5      # share of past downloads from this source that arrived intact (learned)

    @property
    def numbers(self) -> set[float]:
        return {c.number for c in self.chapters}

    @property
    def max(self) -> float:
        return max(self.numbers) if self.numbers else 0.0

    @property
    def usable(self) -> bool:
        return not self.note

    def rank(self) -> tuple:
        """Lower is better: health, then trust in the match, then how reliably
        the source has delivered before (in steps of 10 %, so a few outcomes do
        not reorder sources), then coverage."""
        return (self.source.throttled, self.author_ok, self.match, -round(self.reliability, 1), -len(self.chapters))


@dataclass
class Rejected:
    source: Source
    title: str
    query: str
    reason: str


@dataclass
class Plan:
    series: Series
    matches: list[SourceMatch]
    rejected: list[Rejected]
    # sources that could not be searched this time, with why; their stored
    # entries and chapter states are kept as they are (see db.save_plan)
    unreachable: list[tuple[Source, str]]
    assignment: dict[float, SourceMatch]     # chapter number -> source that will provide it
    junk: dict[float, tuple[SourceMatch, int]] = field(default_factory=dict)  # dropped: too few pages
    # every usable source per chapter, best first (the first is used, the rest are fallbacks)
    candidates: dict[float, list[SourceMatch]] = field(default_factory=dict)

    @property
    def usable(self) -> list[SourceMatch]:
        return [m for m in self.matches if m.usable]

    @property
    def chapters(self) -> list[float]:
        return sorted(self.assignment)

    def have(self) -> set[float]:
        """Chapter numbers Suwayomi reports downloaded on a trusted source.
        A download sitting on an entry that turned out to be another series
        does not count."""
        return {c.number for m in self.usable for c in m.chapters if c.downloaded}

    def wanted(self) -> list[float]:
        have = self.have()
        return [n for n in self.chapters if n not in have]

    def gaps(self, limit: int = MAX_GAP_SPANS) -> list[tuple[int, int]]:
        """Runs of whole chapter numbers below the highest one that no source
        lists, as (first, last) spans, at most `limit` of them. Walks the
        listed numbers instead of range(1, top): cost follows how many
        chapters are listed, never how big the top number is."""
        listed = sorted({int(n) for n in self.assignment if math.isfinite(n) and n >= 1})
        out: list[tuple[int, int]] = []
        prev = 0
        for n in listed:
            if n > prev + 1:
                out.append((prev + 1, n - 1))
                if len(out) >= limit:
                    break
            prev = n
        return out

    def gap_text(self, limit: int = MAX_GAP_SPANS) -> str:
        """'3-5, 9, 12-40' for the gaps, '' when there are none; '...' when
        there are more spans than `limit`."""
        spans = self.gaps(limit + 1)
        text = ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in spans[:limit])
        return text + (", ..." if len(spans) > limit else "")


def resolve(client: Client, series: Series, sources: list[Source] | None = None,
            reliability: dict[str, float] | None = None) -> Plan:
    sources = sources if sources is not None else client.sources()
    reliability = reliability or {}
    titles = capped_search_titles(series)
    log.info("resolving %s [%s] across %d sources, titles: %s", oneline(series.title), series.ref[:80],
             len(sources), oneline(" | ".join(titles[:5]), 400))
    matches: list[SourceMatch] = []
    rejected: list[Rejected] = []
    unreachable: list[tuple[Source, str]] = []

    skipped = [s.name for s in sources if s.unusable]
    if skipped:
        log.debug("not searching disabled source(s): %s", ", ".join(skipped))
    for src in sources:
        if src.unusable:                      # disabled in Settings: not searched, not downloaded from
            continue
        found = _search_source(client, src, series, titles, rejected)
        if isinstance(found, str):
            unreachable.append((src, found))
            log.warning("%s unreachable: %s", src.name, found)
            continue
        if found is None:
            log.info("%-26s no match", src.name)
            continue
        found.reliability = reliability.get(src.name, 0.5)
        matches.append(found)

    _trust(series, matches)
    candidates = _assign(matches)
    assignment = {n: c[0] for n, c in candidates.items()}
    plan = Plan(series, matches, rejected, unreachable, assignment, candidates=candidates)
    _prune_junk(client, plan)
    log.info("%s: %d chapters listed from %d source(s), %d on disk per Suwayomi, %d wanted, %d junk",
             series.title, len(plan.chapters), len({m.manga_id for m in assignment.values()}),
             len(plan.have()), len(plan.wanted()), len(plan.junk))
    return plan


def capped_search_titles(series: Series) -> list[str]:
    """At most MAX_SEARCH_TITLES titles to search with, each at most
    MAX_TITLE characters: every title is one search per source that lacks
    the series, and aliases can be user-typed or come from a provider. The
    native title is always kept (last, as in search_titles) even when the
    Latin titles alone would fill the cap: Korean, Chinese and Japanese
    sources often index only that one."""
    titles = series.search_titles
    if len(titles) > MAX_SEARCH_TITLES:
        native = series.native if series.native in titles[MAX_SEARCH_TITLES:] else None
        kept = [t for t in titles if t != native][:MAX_SEARCH_TITLES - (1 if native else 0)]
        titles = kept + ([native] if native else [])
        log.debug("%s: %d titles; searching with %d of them", oneline(series.title), len(series.search_titles),
                  len(titles))
    return [t[:MAX_TITLE] for t in titles]


_unreachable: dict[str, tuple[float, str]] = {}   # source id -> (when, why); skip it for a while
UNREACHABLE_TTL = 900
# What a source's own DNS or connection failure looks like when Suwayomi
# relays it. Only these are remembered across series: any other error (an
# HTTP 403/429, a parse error for one odd title, a slow answer) concerns that
# one search, so the next series tries the source again.
_CONNECTIVITY = ("unable to resolve host", "unknownhost", "no address associated", "nodename nor servname",
                 "name or service not known", "failed to connect", "connection refused", "no route to host",
                 "network is unreachable", "connectexception")


def _is_connectivity(msg: str) -> bool:
    m = msg.lower()
    return any(k in m for k in _CONNECTIVITY)


def _search_source(client, src, series, titles, rejected):
    """Best accepted hit on one source, trying each title until one lands.
    Returns SourceMatch, None (no acceptable hit) or str (could not search it
    this time). A source whose site cannot be reached at all (DNS/connection)
    is not asked again for UNREACHABLE_TTL. When Suwayomi itself does not
    answer, SuwayomiUnreachable is raised: that is no verdict on any source,
    so the caller keeps the plan it has."""
    import time
    seen_ids: set[int] = set()
    known = _unreachable.get(src.id)
    if known and time.monotonic() - known[0] < UNREACHABLE_TTL:
        return known[1] + " (skipped: unreachable earlier)"
    for q in titles:
        try:
            hits = client.search(src, q)
        except SuwayomiUnreachable:
            raise
        except SuwayomiError as e:
            msg = str(e)
            if _is_connectivity(msg):
                why = "DNS/network: " + msg[:80]
                _unreachable[src.id] = (time.monotonic(), why)
                log.warning("%s: site unreachable (%s); not searched again for %d min", src.name, msg[:120],
                            UNREACHABLE_TTL // 60)
                return why
            log.info("%s: search for %r failed (%s); trying it again for the next series", src.name, q, msg[:120])
            return msg[:80]
        log.debug("%s search %r -> %d hit(s)", src.name, q, len(hits))
        scored = []
        for h in hits:
            if h["id"] in seen_ids:
                continue
            seen_ids.add(h["id"])
            lvl, matched = match_level(h.get("title"), series.titles)
            if lvl in ACCEPTED:
                scored.append((lvl, h, matched))
                log.debug("%s   accept %r == %r", src.name, (h.get("title") or "")[:200], matched)
            else:
                rejected.append(Rejected(src, (h.get("title") or "?")[:MAX_TITLE], q, "title differs"))
                log.debug("%s   reject %r", src.name, (h.get("title") or "")[:200])
        if not scored:
            continue
        scored.sort(key=lambda x: x[0])
        lvl, hit, matched = scored[0]
        try:
            manga, chapters = client.manga(hit["id"])
        except SuwayomiUnreachable:
            raise
        except SuwayomiError as e:
            if "no chapters" in str(e).lower():       # matched, but the entry is empty
                manga, chapters = hit, []
            else:
                return str(e)[:60]
        author = manga.get("author") or manga.get("artist") or hit.get("author")
        author = author[:MAX_TITLE] if isinstance(author, str) else author
        a_lvl = author_level(author, series.authors)
        listed = len(chapters)
        chapters = plausible_chapters(src.name, chapters)
        m = SourceMatch(src, hit["id"], manga.get("title") or hit["title"], author,
                        lvl, matched, a_lvl, chapters, query=q)
        if a_lvl == AUTHOR_DIFFER:
            m.note = f"author differs ({oneline(author, 80)!r} vs {series.authors[:2]})"
        elif src.unusable:
            m.note = "source cannot deliver images from here"
        elif not chapters:
            m.note = "lists no chapters" if not listed else "lists no plausible chapter numbers"
        elif len(chapters) > MAX_CHAPTERS_PER_SOURCE:
            m.note = f"lists {len(chapters)} chapters, more than any real series ({MAX_CHAPTERS_PER_SOURCE})"
            log.warning("%s: %s - not trusted", src.name, m.note)
        log.info("%-26s %-34s %4d ch, max %-6g id=%-6d %s", src.name, oneline(m.title, 34), len(chapters),
                 m.max, hit["id"], f"[{m.note}]" if m.note else "")
        return m
    return None


def plausible_chapters(source_name: str, chapters: list[Chapter]) -> list[Chapter]:
    """The chapters of one source entry without numbers no real chapter has:
    not finite, above MAX_CHAPTER_NUMBER (dates such as 20240115, garbage
    like 999999999), or a top number that jumps far past the rest of the
    list. Checked here, whatever _trust decides later, so one bad number can
    neither flood the plan nor make the gap computation huge. Over-long
    chapter names are cut. Every drop is logged with the source's name."""
    ok, dropped = [], []
    for c in chapters:
        n = c.number
        if isinstance(n, (int, float)) and math.isfinite(n) and 0 <= n <= MAX_CHAPTER_NUMBER:
            ok.append(c)
        else:
            dropped.append(n)
    ok.sort(key=lambda c: c.number)
    # a source listing only its first chapter and its latest one ([1, 1500])
    # is normal, so a lone top number is judged only against enough others
    while len(ok) > OUTLIER_MIN_BELOW:
        top, below = ok[-1].number, ok[-2].number
        if top > OUTLIER_FACTOR * max(below, 1) and top - below > OUTLIER_MIN_JUMP:
            dropped.append(ok.pop().number)
        else:
            break
    if dropped:
        log.warning("%s: ignoring %d implausible chapter number(s): %s", oneline(source_name, 60), len(dropped),
                    ", ".join(f"{n:g}" if isinstance(n, float) else oneline(n, 20) for n in dropped[:5])
                    + (" ..." if len(dropped) > 5 else ""))
    for c in ok:
        if isinstance(c.name, str) and len(c.name) > MAX_CHAPTER_NAME:
            c.name = c.name[:MAX_CHAPTER_NAME]
    return ok


def _trust(series: Series, matches: list[SourceMatch]) -> None:
    """Flag sources whose length says they merged in another series."""
    good = [m for m in matches if m.usable and m.max]
    if not good:
        return
    expected = None
    if series.status == "FINISHED" and series.chapters:
        expected = float(series.chapters)
    elif len(good) >= 3:
        expected = statistics.median(m.max for m in good)
    if not expected:
        return
    for m in good:
        if m.max > expected * config.DISAGREE:
            m.note = f"too long: max {m.max:g} vs expected ~{expected:g}"
            log.warning("%s: %s - not trusted", m.source.name, m.note)


def _assign(matches: list[SourceMatch]) -> dict[float, list[SourceMatch]]:
    """Every usable source that lists each chapter, best-ranked first. The
    first is used; the rest are fallbacks when a download fails."""
    ranked = sorted((m for m in matches if m.usable), key=SourceMatch.rank)
    out: dict[float, list[SourceMatch]] = {}
    for m in ranked:
        for n in m.numbers:
            out.setdefault(n, []).append(m)
    return out


def _prune_junk(client: Client, plan: Plan) -> None:
    """Drop fractional chapters that turn out to be a handful of pages:
    notices and ads, not chapters. Every fractional chapter is probed, not
    just single-source ones - aggregators (Bato, Manganato) scrape the same
    upstream and list the same junk, so agreement between them proves nothing."""
    from . import settings
    min_pages = int(settings.get("min_pages"))
    suspects = [n for n in plan.assignment if n != int(n)]
    if not suspects:
        return
    log.info("probing %d fractional chapter(s) for junk (< %d pages)", len(suspects), min_pages)
    for n in sorted(suspects):
        m = plan.assignment[n]
        ch = next((c for c in m.chapters if c.number == n), None)
        if ch is None or ch.downloaded:
            continue
        pages = client.page_count(ch.id)
        log.debug("%s ch %g: %s pages", m.source.name, n, pages)
        if pages is not None and pages < min_pages:
            plan.junk[n] = (m, pages)
            del plan.assignment[n]
            plan.candidates.pop(n, None)
    if plan.junk:
        log.info("dropped %d junk chapter(s): %s", len(plan.junk), ranges(sorted(plan.junk)))


def primary(plan: Plan) -> SourceMatch | None:
    """The one source entry to keep in Suwayomi's library for new-chapter
    updates: the best-ranked usable source that reaches the furthest."""
    usable = plan.usable
    if not usable:
        return None
    return sorted(usable, key=lambda m: (m.source.throttled, -m.max, m.rank()))[0]


def ranges(nums) -> str:
    """[1,2,3,5,6.5] -> '1-3, 5, 6.5'"""
    def fmt_number(n):
        return f"{n:g}"
    nums = sorted(nums)
    if not nums:
        return "-"
    out, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1 and n == int(n):
            prev = n
            continue
        out.append(fmt_number(start) if start == prev else f"{fmt_number(start)}-{fmt_number(prev)}")
        start = prev = n
    out.append(fmt_number(start) if start == prev else f"{fmt_number(start)}-{fmt_number(prev)}")
    return ", ".join(out)

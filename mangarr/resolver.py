"""Turn a series into a per-chapter download plan.

    series (AniList/MangaDex/manual) -> search every source with every title
                                     -> accept only exact title matches
                                     -> distrust sources whose length is off
                                     -> union the chapter numbers
                                     -> drop fractional "chapters" with no pages
                                     -> pick a source for each chapter
"""
import logging
import statistics
from dataclasses import dataclass, field

from . import config
from .matching import (ACCEPTED, AUTHOR_DIFFER, author_level, match_level)
from .model import Series
from .suwayomi import Chapter, Client, Source, SuwayomiError

log = logging.getLogger(__name__)


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
        """Lower is better. Health first, then trust, then coverage."""
        return (self.source.throttled, self.author_ok, self.match, -len(self.chapters))


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
    unreachable: list[tuple[Source, str]]
    assignment: dict[float, SourceMatch]     # chapter number -> source that will provide it
    junk: dict[float, tuple[SourceMatch, int]] = field(default_factory=dict)  # dropped: too few pages
    candidates: dict[float, list[SourceMatch]] = field(default_factory=dict)  # every usable source per chapter, best first

    @property
    def usable(self) -> list[SourceMatch]:
        return [m for m in self.matches if m.usable]

    @property
    def chapters(self) -> list[float]:
        return sorted(self.assignment)

    def have(self) -> set[float]:
        """Chapter numbers Suwayomi reports downloaded on any accepted source."""
        return {c.number for m in self.matches for c in m.chapters if c.downloaded}

    def wanted(self) -> list[float]:
        have = self.have()
        return [n for n in self.chapters if n not in have]

    def gaps(self) -> list[int]:
        """Whole chapter numbers below the highest one that no source lists."""
        if not self.assignment:
            return []
        top = int(max(self.assignment))
        listed = {int(n) for n in self.assignment}
        return [n for n in range(1, top + 1) if n not in listed]


def resolve(client: Client, series: Series, sources: list[Source] | None = None) -> Plan:
    sources = sources if sources is not None else client.sources()
    log.info("resolving %s [%s] across %d sources, titles: %s", series.title, series.ref,
             len(sources), " | ".join(series.search_titles[:5]))
    matches: list[SourceMatch] = []
    rejected: list[Rejected] = []
    unreachable: list[tuple[Source, str]] = []
    titles = series.search_titles

    for src in sources:
        found = _search_source(client, src, series, titles, rejected)
        if isinstance(found, str):
            unreachable.append((src, found))
            log.warning("%s unreachable: %s", src.name, found)
            continue
        if found is None:
            log.info("%-26s no match", src.name)
            continue
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


def _search_source(client, src, series, titles, rejected):
    """Best accepted hit on one source, trying each title until one lands.
    Returns SourceMatch, None (no acceptable hit) or str (unreachable)."""
    seen_ids: set[int] = set()
    for q in titles:
        try:
            hits = client.search(src, q)
        except SuwayomiError as e:
            msg = str(e)
            if "unreachable" in msg.lower() or "resolve" in msg.lower() or "hostname" in msg.lower():
                return "DNS/network"
            return msg[:60]
        log.debug("%s search %r -> %d hit(s)", src.name, q, len(hits))
        scored = []
        for h in hits:
            if h["id"] in seen_ids:
                continue
            seen_ids.add(h["id"])
            lvl, matched = match_level(h.get("title"), series.titles)
            if lvl in ACCEPTED:
                scored.append((lvl, h, matched))
                log.debug("%s   accept %r == %r", src.name, h.get("title"), matched)
            else:
                rejected.append(Rejected(src, h.get("title") or "?", q, "title differs"))
                log.debug("%s   reject %r", src.name, h.get("title"))
        if not scored:
            continue
        scored.sort(key=lambda x: x[0])
        lvl, hit, matched = scored[0]
        try:
            manga, chapters = client.manga(hit["id"])
        except SuwayomiError as e:
            if "no chapters" in str(e).lower():       # matched, but the entry is empty
                manga, chapters = hit, []
            else:
                return str(e)[:60]
        author = manga.get("author") or manga.get("artist") or hit.get("author")
        a_lvl = author_level(author, series.authors)
        m = SourceMatch(src, hit["id"], manga.get("title") or hit["title"], author,
                        lvl, matched, a_lvl, chapters, query=q)
        if a_lvl == AUTHOR_DIFFER:
            m.note = f"author differs ({author!r} vs {series.authors[:2]})"
        elif src.unusable:
            m.note = "source cannot deliver images from here"
        elif not chapters:
            m.note = "lists no chapters"
        log.info("%-26s %-34s %4d ch, max %-6g id=%-6d %s", src.name, m.title[:34], len(chapters),
                 m.max, hit["id"], f"[{m.note}]" if m.note else "")
        return m
    return None


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
    suspects = [n for n in plan.assignment if n != int(n)]
    if not suspects:
        return
    log.info("probing %d fractional chapter(s) for junk", len(suspects))
    for n in sorted(suspects):
        m = plan.assignment[n]
        ch = next((c for c in m.chapters if c.number == n), None)
        if ch is None or ch.downloaded:
            continue
        pages = client.page_count(ch.id)
        log.debug("%s ch %g: %s pages", m.source.name, n, pages)
        if pages is not None and pages < config.MIN_PAGES:
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
    nums = sorted(nums)
    if not nums:
        return "-"
    out, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1 and n == int(n):
            prev = n
            continue
        out.append(f"{start:g}" if start == prev else f"{start:g}-{prev:g}")
        start = prev = n
    out.append(f"{start:g}" if start == prev else f"{start:g}-{prev:g}")
    return ", ".join(out)

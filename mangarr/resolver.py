"""Turn an AniList series into a per-chapter download plan.

    series (AniList) -> search every source with every title
                     -> accept only exact title matches
                     -> distrust sources whose length is off
                     -> union the chapter numbers
                     -> pick a source for each chapter
"""
import statistics
from dataclasses import dataclass, field

from . import config
from .model import Series
from .matching import (ACCEPTED, AUTHOR_AGREE, AUTHOR_DIFFER, NONE, author_level, match_level)
from .suwayomi import Chapter, Client, Source, SuwayomiError


@dataclass
class SourceMatch:
    source: Source
    manga_id: int
    title: str
    author: str | None
    match: int                    # matching.EXACT / EXACT_BASE / NONE
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

    @property
    def usable(self) -> list[SourceMatch]:
        return [m for m in self.matches if m.usable]

    @property
    def chapters(self) -> list[float]:
        return sorted(self.assignment)

    def have(self) -> set[float]:
        """Chapter numbers already downloaded on any accepted source."""
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


def resolve(client: Client, series: Series, sources: list[Source] | None = None,
            log=lambda m: None) -> Plan:
    sources = sources if sources is not None else client.sources()
    matches: list[SourceMatch] = []
    rejected: list[Rejected] = []
    unreachable: list[tuple[Source, str]] = []
    titles = series.search_titles

    for src in sources:
        found = _search_source(client, src, series, titles, rejected, log)
        if isinstance(found, str):
            unreachable.append((src, found))
            log(f"  {src.name:<26} unreachable: {found}")
            continue
        if found is None:
            log(f"  {src.name:<26} no match")
            continue
        matches.append(found)

    _trust(series, matches, log)
    assignment = _assign(matches)
    plan = Plan(series, matches, rejected, unreachable, assignment)
    _prune_junk(client, plan, log)
    return plan


def _prune_junk(client: Client, plan: Plan, log) -> None:
    """Drop fractional chapters that turn out to be a handful of pages:
    notices and ads, not chapters. Every fractional chapter is probed, not
    just single-source ones - aggregators (Bato, Manganato) scrape the same
    upstream and list the same junk, so agreement between them proves nothing."""
    suspects = [n for n in plan.assignment if n != int(n)]
    if not suspects:
        return
    log(f"  checking {len(suspects)} fractional chapter(s) for junk ...")
    for n in sorted(suspects):
        m = plan.assignment[n]
        ch = next((c for c in m.chapters if c.number == n), None)
        if ch is None or ch.downloaded:
            continue
        pages = client.page_count(ch.id)
        if pages is not None and pages < config.MIN_PAGES:
            plan.junk[n] = (m, pages)
            del plan.assignment[n]
    if plan.junk:
        log(f"  dropped {len(plan.junk)} as junk (<{config.MIN_PAGES} pages)")


def _search_source(client, src, series, titles, rejected, log):
    """Best accepted hit on one source, trying each title until one lands.
    Returns SourceMatch, None (no acceptable hit) or str (unreachable)."""
    seen_ids: set[int] = set()
    for q in titles:
        try:
            hits = client.search(src, q)
        except SuwayomiError as e:
            msg = str(e)
            if "unreachable" in msg.lower() or "resolve" in msg.lower():
                return "DNS/network"
            return msg[:60]
        scored = []
        for h in hits:
            if h["id"] in seen_ids:
                continue
            seen_ids.add(h["id"])
            lvl, matched = match_level(h.get("title"), series.titles)
            if lvl in ACCEPTED:
                scored.append((lvl, h, matched))
            else:
                rejected.append(Rejected(src, h.get("title") or "?", q, "title differs"))
        if not scored:
            continue
        scored.sort(key=lambda x: x[0])
        lvl, hit, matched = scored[0]
        try:
            manga, chapters = client.manga(hit["id"])
        except SuwayomiError as e:
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
        log(f"  {src.name:<26} {m.title[:34]:<34} {len(chapters):>4} ch, max {m.max:<6g}"
            f" id={hit['id']:<6} {'[' + m.note + ']' if m.note else ''}")
        return m
    return None


def _trust(series: Series, matches: list[SourceMatch], log) -> None:
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
            log(f"  ! {m.source.name}: {m.note} - not trusted")


def _assign(matches: list[SourceMatch]) -> dict[float, SourceMatch]:
    """Each chapter goes to the best-ranked source that lists it."""
    ranked = sorted((m for m in matches if m.usable), key=SourceMatch.rank)
    out: dict[float, SourceMatch] = {}
    for m in ranked:
        for n in m.numbers:
            out.setdefault(n, m)
    return out


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

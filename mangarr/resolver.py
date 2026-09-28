"""Turn a series into a per-chapter download plan.

    series (AniList/MangaDex/manual) -> search every source with every title
                                     -> accept only exact title matches
                                     -> drop implausible chapter numbers
                                     -> distrust sources whose length is off,
                                        or that add up to more than any series
                                     -> union the chapter numbers
                                     -> drop fractional "chapters" with no pages
                                     -> pick a source for each chapter
"""
import logging
import math
import statistics
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from . import config, limits
from .limits import Cancelled
from .matching import ACCEPTED, AUTHOR_DIFFER, MAX_TITLE, author_level, match_level, oneline
from .model import Series
from .pagecounts import PageCounts
from .suwayomi import Chapter, Client, Source, SuwayomiError, SuwayomiUnreachable, site_key, with_cancel

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
MAX_PLAN_CHAPTERS = 10_000   # the same bound for the union of the trusted sources
PLAIN_DENSITY = 1.5          # numbers per unit of a source's top number in a plain listing (1, 2, 2.5, 3 ...)
MAX_SEARCH_TITLES = 8
MAX_GAP_SPANS = 40
MAX_CHAPTER_NAME = 500

# Searches on one site start at least this far apart, across every resolve
# in the process (a burst of titles got Weeb Central to about three searches
# a second), and further apart on a page-by-page source, whose site refuses
# bursts; such a source is also searched with fewer titles. The EN and ALL
# variants of one extension are one site (suwayomi.site_key).
SEARCH_GAP_SECS = 1.0
GENTLE_SEARCH_GAP_SECS = 3.0
GENTLE_SEARCH_TITLES = 3
SEARCHES = limits.Spacer()
# One resolve searches up to search_parallel sites at once (Settings), each
# on a thread of its own; the sources of one site are searched one after the
# other on the same thread, so a site never sees two searches at once. The
# main thread looks at the cancel this often while it waits for them.
SEARCH_POLL_SECS = 0.25


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
        """Lower is better: health (normal, rate-limited, page by page: see
        Source.tier), then trust in the match, then how reliably the source
        has delivered before (in steps of 10 %, so a few outcomes do not
        reorder sources), then coverage."""
        return (self.source.tier, self.author_ok, self.match, -round(self.reliability, 1), -len(self.chapters))


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
            reliability: dict[str, float] | None = None, should_cancel: Callable[[], bool] | None = None,
            progress: Callable[[str], None] | None = None, counts: PageCounts | None = None) -> Plan:
    """The plan for a series. Several sites are searched at once
    (search_parallel; _search_parallel), and the plan is the same whatever
    order they answer in. A cancel (should_cancel) is noticed between
    sources and cuts the Suwayomi calls in flight short, raising Cancelled
    with nothing decided; `progress` hears how far the search is. With
    `counts`, fractional chapters counted in an earlier pass are not
    counted again while their count holds (see pagecounts.py)."""
    cancel = should_cancel or (lambda: False)
    report = progress or (lambda m: None)
    raw = client
    client = with_cancel(client, should_cancel)   # each search title, each chapter list: never a full timeout
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
    searched = [s for s in sources if not s.unusable]   # disabled in Settings: not searched, not downloaded from
    width = min(int(limits.setting("search_parallel")), len({site_key(s.name) for s in searched}))
    if width > 1:
        results = _search_parallel(raw, series, searched, titles, width, cancel, report)
    else:
        results = []
        for i, src in enumerate(searched, 1):
            if cancel():
                raise Cancelled()
            report(f"searching {src.name} ({i} of {len(searched)} sources)")
            results.append(_search_one(client, src, series, titles, cancel))
    # in the order of the sources, whatever order the searches finished in
    for src, (found, hits) in zip(searched, results, strict=True):
        rejected.extend(hits)
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
    _prune_junk(client, plan, report, counts)
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
    sources often index only that one. Linear in the number of titles, and
    search_titles is computed once (not again for the log line)."""
    titles = series.search_titles
    if len(titles) > MAX_SEARCH_TITLES:
        total = len(titles)
        native = series.native if series.native in titles[MAX_SEARCH_TITLES:] else None
        kept = [t for t in titles if t != native][:MAX_SEARCH_TITLES - (1 if native else 0)]
        titles = kept + ([native] if native else [])
        log.debug("%s: %d titles; searching with %d of them", oneline(series.title), total, len(titles))
    return [t[:MAX_TITLE] for t in titles]


def gentle_titles(series: Series, titles: list[str]) -> list[str]:
    """At most GENTLE_SEARCH_TITLES of `titles` for a page-by-page source,
    whose site refuses bursts: the first ones, and the native title always
    (last, as in capped_search_titles)."""
    if len(titles) <= GENTLE_SEARCH_TITLES:
        return titles
    native = series.native[:MAX_TITLE] if series.native else None
    if native not in titles:
        native = None
    kept = [t for t in titles if t != native][:GENTLE_SEARCH_TITLES - (1 if native else 0)]
    return kept + ([native] if native else [])


def _search_one(client, src: Source, series: Series, titles: list[str],
                should_cancel: Callable[[], bool]) -> tuple[object, list[Rejected]]:
    """(what _search_source returned, the hits it rejected) for one source.
    An unexpected error in one source's search (an odd answer it could not
    read) costs only that source this time: it counts as not searched, so
    its stored entry is kept (see Plan.unreachable), and the other sources
    still count. Cancelled and SuwayomiUnreachable are raised."""
    rejected: list[Rejected] = []
    try:
        found = _search_source(client, src, series, gentle_titles(series, titles) if src.page_warm else titles,
                               rejected, should_cancel)
    except (Cancelled, SuwayomiUnreachable):
        raise
    except Exception as e:
        log.exception("%s: searching %s failed unexpectedly", oneline(series.title), src.name)
        found = f"{type(e).__name__}: {e}"[:80]
    return found, rejected


def _search_parallel(client, series: Series, sources: list[Source], titles: list[str], width: int,
                     cancel: Callable[[], bool], report: Callable[[str], None]) -> list:
    """_search_one for every source, up to `width` sites at once, each site
    on one of `width` threads (the sources of a site, its EN and ALL
    variants, one after the other: never two searches on one site at once,
    and SEARCHES keeps each site's spacing). The sites with the most to do
    start first. Returns the results in the order of `sources`.

    Every thread stops at its next step once one of them meets Suwayomi not
    answering or a cancel, and their Suwayomi calls in flight are cut short
    (each thread asks Suwayomi through a cancellable client); the first
    SuwayomiUnreachable (in source order) is raised once all of them have
    ended, so an outage the searches ran into together counts once, and a
    resolve never leaves a search behind."""
    sites: dict[str, list[int]] = {}
    for i, src in enumerate(sources):
        sites.setdefault(site_key(src.name), []).append(i)

    def cost(group: list[int]) -> float:              # the most seconds of spacing a site's searches can take
        return sum((GENTLE_SEARCH_GAP_SECS * min(len(titles), GENTLE_SEARCH_TITLES)) if sources[i].page_warm
                   else SEARCH_GAP_SECS * len(titles) for i in group)
    todo = sorted(sites.values(), key=lambda g: (-cost(g), g[0]))
    results: list = [None] * len(sources)
    fatal: dict[int, BaseException] = {}
    stop = threading.Event()
    lock, say_lock = threading.Lock(), threading.Lock()
    done = [0]

    def halted() -> bool:
        return stop.is_set() or cancel()
    ops = with_cancel(client, halted)

    def say() -> None:
        with say_lock:                                  # one after the other: the count never goes back
            report(f"searching {len(sources)} sources, {width} at a time ({done[0]} done)")

    def work() -> None:
        while not halted():
            with lock:
                group = todo.pop(0) if todo else None
            if group is None:
                return
            for i in group:
                if halted():
                    return
                try:
                    res = _search_one(ops, sources[i], series, titles, halted)
                except BaseException as e:              # Cancelled, SuwayomiUnreachable, or worse: ends them all
                    with lock:
                        fatal[i] = e
                    stop.set()
                    return
                with lock:
                    results[i] = res
                    done[0] += 1
                say()

    say()
    threads = []
    for k in range(1, width + 1):
        t = threading.Thread(target=work, name=f"mangarr-search-{k}", daemon=True)
        try:
            t.start()
        except RuntimeError as e:                       # a thread limit: go on with the threads there are
            log.warning("could start only %d of %d search thread(s) (%s)", k - 1, width, e)
            break
        threads.append(t)
    if not threads:
        work()                                          # not even one: search here, one site after the other
    try:
        for t in threads:
            while t.is_alive():
                t.join(SEARCH_POLL_SECS)
                if not stop.is_set() and cancel():
                    stop.set()
    finally:
        stop.set()                                      # only matters when this thread was interrupted
        for t in threads:
            t.join()
    for i in sorted(fatal):
        if isinstance(fatal[i], SuwayomiUnreachable):
            raise fatal[i]
    if fatal:
        raise fatal[min(fatal)]
    if cancel() or any(r is None for r in results):
        raise Cancelled()
    return results


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


def _search_source(client, src, series, titles, rejected, should_cancel: Callable[[], bool] | None = None):
    """Best accepted hit on one source, trying each title until one lands.
    Returns SourceMatch, None (no acceptable hit) or str (could not search it
    this time). A source whose site cannot be reached at all (DNS/connection)
    is not asked again for UNREACHABLE_TTL. When Suwayomi itself does not
    answer, SuwayomiUnreachable is raised: that is no verdict on any source,
    so the caller keeps the plan it has. Searches on one site are spaced
    (SEARCHES; the EN and ALL variants of an extension are one site, like
    their download lane); a cancel during that wait raises Cancelled."""
    import time
    seen_ids: set[int] = set()
    known = _unreachable.get(src.id)
    if known and time.monotonic() - known[0] < UNREACHABLE_TTL:
        return known[1] + " (skipped: unreachable earlier)"
    for q in titles:
        if SEARCHES.wait(site_key(src.name), GENTLE_SEARCH_GAP_SECS if src.page_warm else SEARCH_GAP_SECS,
                         should_cancel):
            raise Cancelled()
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
    """Flag sources whose length says they merged in another series, then
    those that would take the plan past MAX_PLAN_CHAPTERS."""
    good = [m for m in matches if m.usable and m.max]
    if not good:
        return
    expected = None
    if series.status == "FINISHED" and series.chapters:
        expected = float(series.chapters)
    elif len(good) >= 3:
        expected = statistics.median(m.max for m in good)
    if expected:
        for m in good:
            if m.max > expected * config.DISAGREE:
                m.note = f"too long: max {m.max:g} vs expected ~{expected:g}"
                log.warning("%s: %s - not trusted", m.source.name, m.note)
    _cap_union(matches)


def _plausibility(numbers: set[float]) -> tuple[float, float]:
    """Sort key for _cap_union, most plausible first: a plain listing (at
    most PLAIN_DENSITY numbers per unit of its top number: 1, 2, 3 with the
    odd .5) before a dense one - fractions packed below the top, which is
    padding or chapters split into .1/.2 parts - and among plain ones, the
    one that reaches least far (a merged-in series only adds numbers
    above). So a source can only come first by listing about as few
    numbers as its top number, and cannot take the budget from the others."""
    top = max(numbers, default=0.0)
    return max(len(numbers) / max(top, 1.0), PLAIN_DENSITY), top


def _cap_union(matches: list[SourceMatch]) -> None:
    """Each source is capped (MAX_CHAPTERS_PER_SOURCE), but two or three
    sources listing different numbers can still add up to a plan no real
    series has - and every number becomes a wanted chapter row. Without a
    known length to judge by (an ongoing series on fewer than three
    sources), the most plausible set is kept: sources in _plausibility()
    order, best-ranked among equals, as long as their union stays within
    MAX_PLAN_CHAPTERS. The others are not trusted."""
    union: set[float] = set()
    usable = [(m, m.numbers) for m in matches if m.usable]
    for m, numbers in sorted(usable, key=lambda mn: (*_plausibility(mn[1]), mn[0].rank())):
        total = len(union) + len(numbers - union)
        if total > MAX_PLAN_CHAPTERS:
            m.note = f"would make the plan {total} chapters, more than any real series ({MAX_PLAN_CHAPTERS})"
            log.warning("%s: %s - not trusted", m.source.name, m.note)
            continue
        union |= numbers


def _assign(matches: list[SourceMatch]) -> dict[float, list[SourceMatch]]:
    """Every usable source that lists each chapter, best-ranked first. The
    first is used; the rest are fallbacks when a download fails."""
    ranked = sorted((m for m in matches if m.usable), key=SourceMatch.rank)
    out: dict[float, list[SourceMatch]] = {}
    for m in ranked:
        for n in m.numbers:
            out.setdefault(n, []).append(m)
    return out


def _prune_junk(client: Client, plan: Plan, progress: Callable[[str], None] | None = None,
                counts: PageCounts | None = None) -> None:
    """Drop fractional chapters that turn out to be a handful of pages:
    notices and ads, not chapters. Every fractional chapter is probed, not
    just single-source ones - aggregators (Bato, Manganato) scrape the same
    upstream and list the same junk, so agreement between them proves nothing.
    With `counts`, a count kept from an earlier pass is used while it holds,
    a chapter whose count failed is kept until its retry is due, and only
    the other chapters are probed."""
    from . import settings
    min_pages = int(settings.get("min_pages"))
    suspects = sorted(n for n in plan.assignment if n != int(n))
    if not suspects:
        return
    pages: dict[float, int | None] = {}
    todo: list[tuple[float, SourceMatch, Chapter]] = []
    for n in suspects:
        m = plan.assignment[n]
        ch = next((c for c in m.chapters if c.number == n), None)
        if ch is None or ch.downloaded:
            continue
        kept, count = counts.lookup(m.manga_id, ch, min_pages) if counts is not None else (False, None)
        if kept:
            pages[n] = count
        else:
            todo.append((n, m, ch))
    if todo or pages:
        waiting = sum(1 for v in pages.values() if v is None)    # a failed count, not due again yet
        log.info("probing %d fractional chapter(s) for junk (< %d pages)%s%s", len(todo), min_pages,
                 f"; {len(pages) - waiting} more counted in an earlier pass" if len(pages) > waiting else "",
                 f"; {waiting} kept until a failed count is tried again" if waiting else "")
    for i, (n, m, ch) in enumerate(todo, 1):
        if progress:
            progress(f"counting the pages of fractional chapters ({i} of {len(todo)})")
        try:
            count = client.page_count(ch.id)
        except SuwayomiUnreachable as e:           # no verdict on the chapter: kept, and counted next time
            log.debug("%s ch %g: pages not counted: %s", m.source.name, n, e)
            count = None
        else:
            if counts is not None:
                count = counts.record(m.manga_id, ch, count)
        log.debug("%s ch %g: %s pages", m.source.name, n, count)
        pages[n] = count
    for n in suspects:
        count = pages.get(n)
        if count is not None and count < min_pages:
            plan.junk[n] = (plan.assignment[n], count)
            del plan.assignment[n]
            plan.candidates.pop(n, None)
    if plan.junk:
        log.info("dropped %d junk chapter(s): %s", len(plan.junk), ranges(sorted(plan.junk)))


def primary(plan: Plan) -> SourceMatch | None:
    """The one source entry to keep in Suwayomi's library for new-chapter
    updates: the best-ranked usable source that reaches the furthest, a
    rate-limited or page-by-page one only when nothing healthier is usable."""
    usable = plan.usable
    if not usable:
        return None
    return sorted(usable, key=lambda m: (m.source.tier, -m.max, m.rank()))[0]


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

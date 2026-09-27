"""Page-by-page fetching for sources whose image server refuses bursts.

Suwayomi downloads a chapter by requesting all of its pages from the source
at once. Some image servers (Comick's) answer such a burst with HTTP 429,
so every chapter fails, yet serve the same pages one at a time. Suwayomi
keeps each page it serves in its cache and builds a download from that
cache. So for a source listed in Settings (page_warm_sources) the
downloader first has every page of the chapter requested through Suwayomi,
one at a time and a few seconds apart (warm_chapter), and only then queues
the chapter.

The spacing is kept per site, across chapters, series and threads
(PagePacer): it starts at the Page Delay setting, grows when the image
server answers busy and eases back slowly while it answers. It lives in
memory only; a restart starts again at the setting. Only paths that look
like Suwayomi's own page paths (suwayomi.PAGE_PATH) are ever requested: a
page list with anything else is downloaded the normal way.
"""
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import limits, metrics
from .suwayomi import PAGE_PATH, SuwayomiError, SuwayomiUnreachable

log = logging.getLogger(__name__)

# Tries per page (busy answers or timeouts). Before a retry: RETRY_FIRST_SECS
# (or the current spacing, when larger), doubling per try, at most RETRY_MAX_SECS.
PAGE_TRIES = 6
RETRY_FIRST_SECS = 5
RETRY_MAX_SECS = 60
# Every busy answer widens the spacing by BUSY_FACTOR (at most DELAY_MAX_SECS);
# every EASE_EVERY pages answered in a row narrow it by EASE_FACTOR, never
# below the Page Delay setting.
DELAY_MAX_SECS = 30
BUSY_FACTOR = 1.5
EASE_EVERY = 5
EASE_FACTOR = 0.85
PAGE_TIMEOUT_SECS = 60
# a page list longer than this is not a chapter
MAX_PAGES = 1000
# a chapter may take DEADLINE_PER_PAGE s a page, within DEADLINE_MIN..DEADLINE_MAX
DEADLINE_PER_PAGE = 20
DEADLINE_MIN = 600
DEADLINE_MAX = 3600
# more pages than max(FAIL_MIN, FAIL_SHARE of the chapter) busy on every try:
# the image server refuses even paced requests
FAIL_MIN = 2
FAIL_SHARE = 0.1

# test seams: the clock and the pause (tests replace these, never time.monotonic itself)
_now = time.monotonic
_pause = limits.pause

_pacers: dict[str, "PagePacer"] = {}
_pacers_lock = threading.Lock()
_bad_url_logged = False                 # an unexpected page URL is an error once per process


@dataclass
class WarmResult:
    """What came of one chapter's page-by-page fetch (see warm_chapter)."""
    state: str                  # ok | partial | refused | deadline | no_pages | cancelled
    pages: int = 0              # pages the chapter has
    fetched: int = 0            # pages the image server delivered
    failed: int = 0             # pages still busy after every try
    busy: int = 0               # busy answers and timeouts, retries included
    recovered: int = 0          # pages delivered on a retry
    secs: float = 0.0
    why: str = ""
    limit: float = 0.0          # how long the chapter was allowed to take

    @property
    def usable(self) -> bool:
        """Worth queueing: every page, or all but a few, is in Suwayomi's cache."""
        return self.state in ("ok", "partial")


def _floor() -> float:
    return limits.setting("page_delay_seconds")


class PagePacer:
    """The spacing of page requests to one site, shared by every chapter,
    series and thread that fetches from it: a request starts at least
    gap() s after the previous one ended (and after the previous start,
    should two callers overlap). The gap is the Page Delay setting, re-read
    at every wait, or more after busy answers."""

    def __init__(self, name: str):
        self.name = name                        # for the metric: the first display name seen
        self._lock = threading.Lock()
        self.delay = 0.0                        # widened by busy answers; below the setting means the setting
        self.last_end: float | None = None
        self.streak = 0                         # pages answered in a row since the last busy one
        self._next = 0.0                        # earliest start of the next request (a slot already taken)

    def gap(self) -> float:
        return max(self.delay, _floor())

    def wait(self, cancel: Callable[[], bool] | None = None) -> bool:
        """Wait for this site's next request slot. True when cancelled meanwhile."""
        gap = self.gap()
        with self._lock:
            now = _now()
            start = max(now, self._next, self.last_end + gap if self.last_end is not None else now)
            self._next = start + gap
        return _pause(start - now, cancel)

    def done(self, status: str) -> None:
        """How a request ended (suwayomi.PageFetch.status): busy answers and
        timeouts widen the spacing, a run of answered pages narrows it."""
        floor = _floor()
        with self._lock:
            self.last_end = _now()
            if status in ("busy", "timeout"):
                self.delay = min(DELAY_MAX_SECS, max(self.delay, floor) * BUSY_FACTOR)
                self.streak = 0
            elif status == "ok":
                self.streak += 1
                if self.streak % EASE_EVERY == 0:
                    self.delay = max(floor, self.delay * EASE_FACTOR)
            gap = max(self.delay, floor)
        metrics.set_page_delay(self.name, gap)


def pacer(name: str) -> PagePacer:
    """The pacer of source `name`'s site: the EN and ALL variants of one
    extension share an image server, so they share one."""
    from .downloader import lanes_key  # here, not at the top: downloader imports this module
    key = lanes_key(name)
    with _pacers_lock:
        p = _pacers.get(key)
        if p is None:
            p = _pacers[key] = PagePacer(name)
        return p


def warm_chapter(client, chapter, source_name: str, cancel: Callable[[], bool] | None = None,
                 report: Callable[[str], None] | None = None, suffix: str = "") -> WarmResult:
    """Have every page of `chapter` requested through Suwayomi, one at a time
    (see the module docstring), so the download that follows is built from
    its cache. `report` hears each step, with `suffix` appended. The result's
    state is
      ok         every page was fetched
      partial    a few pages were busy on every try (the download fetches those itself)
      refused    the image server refuses even paced requests: more pages than that
                 were busy on every try, or the time ran out mostly on busy answers
      deadline   the chapter took longer than it may (DEADLINE_PER_PAGE a page)
                 while its pages did arrive
      no_pages   nothing to fetch page by page (no page list, an unexpected page
                 URL, a page that is gone): download it the normal way
      cancelled  a cancel came; nothing was queued
    Suwayomi itself not answering raises SuwayomiUnreachable."""
    cancel = cancel or (lambda: False)
    report = report or (lambda m: None)
    res = WarmResult("ok")
    t0 = _now()
    try:
        _fetch_pages(client, chapter, source_name, cancel, report, suffix, res)
    except SuwayomiUnreachable:
        raise
    except limits.Cancelled:
        res.state = "cancelled"
    except Exception as e:
        log.exception("%s ch %g: fetching its pages one at a time failed", source_name, chapter.number)
        res.state, res.why = "no_pages", f"fetching pages one at a time failed ({type(e).__name__}: {e})"[:300]
    res.secs = _now() - t0
    if res.usable:
        log.info("%s ch %g: %d/%d pages fetched one by one in %.0f s (%d busy answers, spacing now %.1f s)",
                 source_name, chapter.number, res.fetched, res.pages, res.secs, res.busy,
                 pacer(source_name).gap())
    elif res.state in ("refused", "deadline"):
        log.warning("%s ch %g: %s; %d/%d pages fetched in %.0f s (%d busy answers)", source_name,
                    chapter.number, res.why, res.fetched, res.pages, res.secs, res.busy)
    elif res.state == "cancelled":
        log.debug("%s ch %g: page-by-page fetch cancelled after %d/%d pages", source_name, chapter.number,
                  res.fetched, res.pages)
    return res


def _fetch_pages(client, chapter, source_name: str, cancel, report, suffix: str, res: WarmResult) -> None:
    """warm_chapter's work: fills in `res` as it goes, so a cancel or an
    error keeps the counts."""
    n = chapter.number
    try:
        urls = client.page_urls(chapter.id)
    except SuwayomiUnreachable:
        raise
    except SuwayomiError as e:
        res.state, res.why = "no_pages", f"its page list could not be read ({e})"[:300]
        return
    res.pages = total = len(urls)
    if not urls:
        res.state, res.why = "no_pages", "Suwayomi listed no pages for it"
        return
    if total > MAX_PAGES:
        res.state, res.why = "no_pages", f"its page list has {total} pages, more than a chapter has"
        return
    bad = [u for u in urls if not isinstance(u, str) or not PAGE_PATH.match(u)]
    if bad:
        _note_bad_url(source_name, n, bad[0])
        res.state, res.why = "no_pages", "its page list has a URL that is not a Suwayomi page path"
        return
    res.limit = min(max(total * DEADLINE_PER_PAGE, DEADLINE_MIN), DEADLINE_MAX)
    deadline = _now() + res.limit
    pace = pacer(source_name)
    too_many = max(FAIL_MIN, FAIL_SHARE * total)
    for k, path in enumerate(urls, 1):
        for t in range(1, PAGE_TRIES + 1):
            if cancel():
                res.state = "cancelled"
                return
            if _now() > deadline:
                _out_of_time(res, k, total)
                return
            report(f"{source_name}: chapter {n:g} - fetching page {k} of {total} one at a time{suffix}")
            if pace.wait(cancel):
                res.state = "cancelled"
                return
            if _now() > deadline:               # the spacing took it past the chapter's time
                _out_of_time(res, k, total)
                return
            r = client.fetch_page(path, timeout=PAGE_TIMEOUT_SECS)
            pace.done(r.status)
            metrics.record_page(source_name, r.status)
            log.debug("%s ch %g: page %d of %d try %d -> %s (HTTP %s, %d bytes, %.1f s)", source_name, n, k,
                      total, t, r.status, r.http, r.nbytes, r.secs)
            if r.status == "ok":
                res.fetched += 1
                if t > 1:
                    res.recovered += 1
                break
            if r.status in ("gone", "error"):
                res.state = "no_pages"
                res.why = f"page {k} answered HTTP {r.http}" if r.http else f"page {k} could not be fetched"
                return
            res.busy += 1                       # busy or timeout: the image server's, try again later
            if t < PAGE_TRIES:
                w = min(RETRY_MAX_SECS, max(RETRY_FIRST_SECS, pace.gap()) * 2 ** (t - 1))
                if _now() + w > deadline:       # the retry would come after the chapter's time is up
                    _out_of_time(res, k, total)
                    return
                report(f"{source_name}: chapter {n:g} - page {k} of {total}: image server busy, retry {t} of "
                       f"{PAGE_TRIES - 1} in {w:.0f} s{suffix}")
                if _pause(w, cancel):
                    res.state = "cancelled"
                    return
        else:
            res.failed += 1
            log.warning("%s ch %g: page %d of %d was still busy after %d tries; going on without it",
                        source_name, n, k, total, PAGE_TRIES)
            if res.failed > too_many:
                res.state = "refused"
                res.why = f"{res.failed} pages stayed busy after {PAGE_TRIES} tries each"
                return
    res.state = "partial" if res.failed else "ok"


def _out_of_time(res: WarmResult, k: int, total: int) -> None:
    """The chapter's time is up at page k. Mostly on busy answers, that is
    the image server refusing (a page busy on every try takes minutes of
    retries, so this comes before the failed-page count on all but short
    chapters)."""
    mins = f"{res.limit / 60:.0f} min"
    if res.busy > res.fetched:
        res.state = "refused"
        res.why = f"the image server answered busy {res.busy} times in {mins} (stopped at page {k} of {total})"
    else:
        res.state, res.why = "deadline", f"stopped at page {k} of {total} after {mins}"


def _note_bad_url(source_name: str, n: float, url) -> None:
    """An error the first time in this process (Suwayomi's page paths may
    have changed with an update, and page by page is off until this code
    knows them), debug after that."""
    global _bad_url_logged
    first, _bad_url_logged = not _bad_url_logged, True
    log.log(logging.ERROR if first else logging.DEBUG,
            "%s ch %g: Suwayomi listed a page URL that is not one of its page paths (%r); not fetching pages "
            "one at a time, downloading the normal way%s", source_name, n, str(url)[:100],
            " (said once per start)" if first else "")

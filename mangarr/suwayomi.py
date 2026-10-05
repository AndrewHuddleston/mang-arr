"""Suwayomi GraphQL client. Suwayomi is the download engine; this is the only
module that talks to it."""
import copy
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from . import config, limits, outbound

log = logging.getLogger(__name__)


class SuwayomiError(RuntimeError):
    pass


class QueryError(SuwayomiError):
    """Suwayomi answered, with an error for the query (GraphQL errors: a
    field this version does not know, no such chapter, a source's failure
    it reports), as opposed to no answer or one that says nothing (an HTTP
    error, a proxy's 502, a body that is not JSON)."""


class SuwayomiUnreachable(SuwayomiError):
    """Suwayomi itself did not answer (connection refused, DNS, or a hung
    server), as opposed to one source failing behind it. Callers that loop
    over many series stop early on this instead of paying the timeout again
    for every series."""


class CircuitOpen(SuwayomiUnreachable):
    """Refused without sending anything: Suwayomi failed moments ago and the
    circuit breaker is open, so nothing of this call reached it."""


# Circuit breaker, shared by every Client pointing at the same Suwayomi: after
# a transport failure, calls fail at once for BREAKER_SECS instead of each
# waiting out its own timeouts and retries. The first call after the window
# goes through again (a successful call closes the breaker).
BREAKER_SECS = 60
_down: dict[str, tuple[float, str]] = {}          # api url -> (monotonic time it tripped, why)
_down_lock = threading.Lock()

# These ask the source website for something, so a timeout may only mean the
# site is slow. After one times out, Suwayomi is asked something that touches
# no website (waiting at most PROBE_SECS): only when that goes unanswered too
# is Suwayomi itself down (breaker tripped, SuwayomiUnreachable), so a pass
# stops instead of waiting out every source of every series.
_REMOTE_OPS = {"fetchSourceManga", "fetchMangaAndChapters", "fetchChapterPages"}
PROBE_SECS = 30


def _failure_kind(e: BaseException) -> str:
    """'timeout', 'connect' (refused, reset, DNS), 'http' (Suwayomi answered
    with an HTTP error) or 'other' (e.g. a response that is not JSON)."""
    if isinstance(e, urllib.error.HTTPError):
        return "http"
    reason = e.reason if isinstance(e, urllib.error.URLError) else e
    if isinstance(reason, TimeoutError):          # socket.timeout is TimeoutError on 3.10+
        return "timeout"
    if isinstance(reason, OSError):
        return "connect"
    return "other"


_OPNAME = re.compile(r"\b(fetchSourceManga|fetchMangaAndChapters|fetchChapterPages|downloadStatus|"
                     r"enqueueChapterDownloads|dequeueChapterDownloads|startDownloader|stopDownloader|"
                     r"clearDownloader|updateManga|setSettings|settings|sources|mangas|manga)\b")

# A page image as Suwayomi serves it: /api/v1/manga/<id>/chapter/<index>/page/<n>, with at most
# a short cache-busting query. fetchChapterPages returns these; anything else is never requested.
# (\Z, not $: $ would also accept a trailing newline)
PAGE_PATH = re.compile(r"^/api/v1/manga/\d{1,10}/chapter/\d{1,6}/page/\d{1,5}(\?[A-Za-z0-9=&_.-]{0,100})?\Z")
PAGE_MAX_BYTES = 64 << 20
# answers that mean "busy, try again later" from the image server behind Suwayomi
_PAGE_BUSY = {429, 500, 502, 503, 504}
_PAGE_GONE = {404, 410}


def site_key(name: str) -> str:
    """The site a source name stands for: the EN and ALL variants of one
    extension ('Comick (Unoriginal) (EN)' and '... (ALL)') are one site,
    with one image server."""
    return re.sub(r"\s*\((en|all)\)\s*$", "", name.lower().strip())


@dataclass(frozen=True)
class PageFetch:
    status: str             # ok | busy | gone | error | timeout
    http: int | None        # the HTTP status, when there was one
    nbytes: int             # image bytes read (and thrown away)
    secs: float


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    lang: str
    unusable: bool = False      # searched, never downloaded from (Settings)
    throttled: bool = False     # single-chapter batches, loses close calls (Settings)
    page_warm: bool = False     # pages fetched one by one before a download: slow, used last (Settings)

    @property
    def key(self) -> str:
        return self.name.lower().strip()

    @property
    def tier(self) -> int:
        """0 normal, 1 rate-limited, 2 page by page: a lower tier wins every
        choice between sources that list a chapter."""
        return 2 if self.page_warm else 1 if self.throttled else 0


@dataclass
class Chapter:
    id: int
    number: float
    name: str | None
    scanlator: str | None
    downloaded: bool
    uploaded: str | None = None      # ISO date the source published it, when known


class Client:
    _cancel: Callable[[], bool] | None = None      # set on a cancellable() copy

    def __init__(self, url: str = config.SUWAYOMI_URL):
        self.base = url.rstrip("/")
        self.api = self.base + "/api/graphql"

    def cancellable(self, should_cancel: Callable[[], bool]) -> "Client":
        """This client for one job: every call checks should_cancel before it
        starts, while it waits for Suwayomi and between its retries, and
        raises limits.Cancelled at once instead of running into its timeout.
        The request in flight is left to finish on its own, so a change cut
        short may still land: the downloader takes such queue changes back
        (see downloader._dequeue); reads and lookups are simply dropped."""
        c = copy.copy(self)
        c._cancel = should_cancel
        return c

    # -- transport --------------------------------------------------------

    def gq(self, query: str, variables: dict | None = None, timeout: int = 180, retries: int = 3) -> dict:
        body: dict = {"query": query}
        if variables:
            body["variables"] = variables
        last: BaseException | None = None
        m = _OPNAME.search(query)
        op = m.group(1) if m else "query"
        down = self._check_breaker()
        for attempt in range(1, retries + 1):
            t0 = time.monotonic()
            try:
                req = urllib.request.Request(self.api, json.dumps(body).encode(),
                                             {"Content-Type": "application/json"})
                d = self._send(req, timeout)
                if down:
                    self._breaker_close()
                    down = None
                if "errors" in d:
                    msg = d["errors"][0]["message"].split("\n")[0][:200]
                    log.debug("suwayomi %s %s -> error in %.1fs: %s", op, variables or "",
                              time.monotonic() - t0, msg)
                    raise QueryError(msg)
                log.debug("suwayomi %s %s -> ok in %.1fs", op, variables or "", time.monotonic() - t0)
                return d["data"]
            except (SuwayomiError, limits.Cancelled):
                raise
            except Exception as e:
                last = e
                log.debug("suwayomi %s attempt %d/%d failed after %.1fs: %s", op, attempt, retries,
                          time.monotonic() - t0, e)
                if attempt < retries and limits.pause(5, self._cancel):
                    raise limits.Cancelled() from e
        kind = _failure_kind(last) if last is not None else "other"
        if kind == "timeout" and op in _REMOTE_OPS:
            if timeout >= PROBE_SECS:               # a short (page) call timing out proves nothing either way
                self._check_answers(op, timeout)
            raise SuwayomiError(f"{op} timed out after {timeout} s (the source did not answer in time)")
        if kind in ("connect", "timeout"):
            # A short probe (the health check, a page's status widget) timing
            # out once does not prove Suwayomi is down, so only calls that
            # waited a while trip the breaker; refused/DNS always does.
            if kind == "connect" or timeout >= 30:
                self._breaker_trip(f"{type(last).__name__}: {last}")
            raise SuwayomiUnreachable(f"Suwayomi at {self.api} unreachable: {last}")
        raise SuwayomiError(f"Suwayomi at {self.api} failed: {type(last).__name__}: {last}")

    def _send(self, req: urllib.request.Request, timeout: int) -> dict:
        def call() -> dict:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        return limits.interruptible(call, self._cancel)

    def _check_breaker(self) -> tuple | None:
        """Raise CircuitOpen while the breaker is open. Otherwise returns
        what tripped it last (None when it is closed): a call that then gets
        an answer closes it."""
        with _down_lock:
            down = _down.get(self.api)
        if down and time.monotonic() - down[0] < BREAKER_SECS:
            raise CircuitOpen(f"Suwayomi at {self.api} unreachable: {down[1]} (not retrying for "
                              f"{BREAKER_SECS - (time.monotonic() - down[0]):.0f} s)")
        return down

    def _check_answers(self, op: str, waited: int) -> None:
        """After a source request got no answer: raise SuwayomiUnreachable
        (the breaker is tripped by the probe's own failure) when Suwayomi does
        not answer a query that touches no website either. When it does, the
        website is what is slow, and the caller treats it as that source's
        failure."""
        try:
            self.gq("{ aboutServer { version } }", timeout=PROBE_SECS, retries=1)
        except SuwayomiUnreachable as e:
            raise SuwayomiUnreachable(f"Suwayomi at {self.api} stopped answering: {op} got no answer in {waited} s, "
                                      "and neither did a status query") from e
        except SuwayomiError as e:
            log.debug("suwayomi answered a status query (%s) after %s timed out: the source is slow", e, op)
            return
        log.debug("suwayomi answers after %s timed out: the source is slow", op)

    def _breaker_trip(self, why: str) -> None:
        with _down_lock:
            first = self.api not in _down
            _down[self.api] = (time.monotonic(), why[:200])
        if first:
            log.error("Suwayomi at %s is not answering (%s); calls fail fast for %d s", self.api, why[:200],
                      BREAKER_SECS)

    def _breaker_close(self) -> None:
        with _down_lock:
            was = _down.pop(self.api, None)
        if was:
            log.info("Suwayomi at %s answers again", self.api)

    # -- sources / search -------------------------------------------------

    def sources(self, langs=("en", "all")) -> list[Source]:
        """Installed sources, with the disabled/throttled/page-by-page flags
        from Settings stamped once so a run is consistent even if settings
        change."""
        from . import settings
        v = settings.all_values()
        unusable, throttled = set(v["unusable_sources"]), set(v["throttled_sources"])
        warm = v.get("page_warm_sources")
        warm = set(warm) if isinstance(warm, list) else set()
        try:                                        # plus the ones that rate-limited us recently
            from . import db
            with db.connect() as con:
                throttled |= db.auto_throttled(con)
        except Exception as e:
            log.debug("could not read detected rate limits: %s", e)
        d = self.gq("{ sources { nodes { id displayName lang } } }", timeout=30, retries=2)
        out = []
        for s in d["sources"]["nodes"]:
            if s["lang"] not in langs or s["displayName"] == "Local source":
                continue
            key = s["displayName"].lower().strip()
            out.append(Source(s["id"], s["displayName"], s["lang"], key in unusable, key in throttled, key in warm))
        return out

    def search(self, source: Source, query: str) -> list[dict]:
        """Raw hits: [{id, title, author, status}]. Raises SuwayomiError when
        the source is unreachable (DNS filter, site down)."""
        d = self.gq(
            'mutation($src: LongString!, $q: String!) {'
            ' fetchSourceManga(input: {source: $src, type: SEARCH, page: 1, query: $q})'
            ' { mangas { id title author status } } }',
            {"src": source.id, "q": query}, timeout=90, retries=1)
        return d["fetchSourceManga"]["mangas"]

    def manga(self, manga_id: int) -> tuple[dict, list[Chapter]]:
        """Fetch a source entry and its chapter list, deduped by chapter number."""
        d = self.gq(
            'mutation($id: Int!) {'
            ' fetchMangaAndChapters(input: {id: $id, fetchManga: true, fetchChapters: true})'
            ' { manga { id title author artist status inLibrary }'
            '   chapters { id name chapterNumber scanlator isDownloaded uploadDate } } }',
            {"id": manga_id}, timeout=240, retries=1)
        r = d["fetchMangaAndChapters"]
        return r["manga"], dedupe(r["chapters"])

    def chapters(self, manga_id: int) -> list[Chapter]:
        """Chapter list from Suwayomi's cache (no source fetch)."""
        d = self.gq('query($id: Int!) { manga(id: $id) { chapters { nodes'
                    ' { id name chapterNumber scanlator isDownloaded uploadDate } } } }', {"id": manga_id},
                    timeout=60, retries=2)
        return dedupe(d["manga"]["chapters"]["nodes"])

    def chapters_all(self, manga_id: int) -> list[Chapter]:
        """Every chapter Suwayomi lists for a source entry, from its cache:
        each scanlation group's copy of a number, where chapters() keeps one
        per number. For telling which chapter a downloaded file is."""
        d = self.gq('query($id: Int!) { manga(id: $id) { chapters { nodes'
                    ' { id name chapterNumber scanlator isDownloaded uploadDate } } } }', {"id": manga_id},
                    timeout=60, retries=2)
        return [Chapter(c["id"], float(c["chapterNumber"]), c.get("name"), c.get("scanlator"),
                        bool(c.get("isDownloaded")), _iso_date(c.get("uploadDate")))
                for c in d["manga"]["chapters"]["nodes"] if float(c["chapterNumber"]) >= 0]

    def chapter_url(self, chapter_id: int) -> str | None:
        """The chapter's page on its source's site (Suwayomi's realUrl, from
        the extension; checked by the caller before it is shown), or None
        when Suwayomi has none. Raises QueryError when this Suwayomi does
        not know the field (or the chapter), SuwayomiError otherwise."""
        d = self.gq("query($id: Int!) { chapter(id: $id) { realUrl } }", {"id": chapter_id}, timeout=30, retries=1)
        url = (d.get("chapter") or {}).get("realUrl") if isinstance(d, dict) else None
        return url if isinstance(url, str) and url else None

    def downloaded_ids(self, manga_id: int) -> set[int]:
        d = self.gq('query($id: Int!) { manga(id: $id) { chapters { nodes { id isDownloaded } } } }',
                    {"id": manga_id}, timeout=60, retries=2)
        return {c["id"] for c in d["manga"]["chapters"]["nodes"] if c["isDownloaded"]}

    def page_count(self, chapter_id: int) -> int | None:
        """How many pages a chapter has (fetches the page list from the
        source). None when the source will not say; SuwayomiUnreachable
        when Suwayomi itself does not answer, which says nothing about the
        chapter."""
        try:
            d = self.gq('mutation($id: Int!) { fetchChapterPages(input: {chapterId: $id}) { pages } }',
                        {"id": chapter_id}, timeout=60, retries=1)
            return len(d["fetchChapterPages"]["pages"])
        except SuwayomiUnreachable:
            raise
        except SuwayomiError:
            return None

    def page_urls(self, chapter_id: int) -> list[str]:
        """The chapter's page paths on Suwayomi (fetches the page list from
        the source, like page_count). Raises SuwayomiError. The caller checks
        each against PAGE_PATH before requesting it."""
        d = self.gq('mutation($id: Int!) { fetchChapterPages(input: {chapterId: $id}) { pages } }',
                    {"id": chapter_id}, timeout=60, retries=1)
        try:
            pages = d["fetchChapterPages"]["pages"]
        except (KeyError, TypeError) as e:
            raise SuwayomiError(f"fetchChapterPages answered without a page list ({type(e).__name__})") from e
        if not isinstance(pages, list):
            raise SuwayomiError("fetchChapterPages answered without a page list")
        return [str(p) for p in pages]

    def fetch_page(self, path: str, timeout: int = 60, max_bytes: int = PAGE_MAX_BYTES) -> PageFetch:
        """GET one page image through Suwayomi, which fetches it from the
        source and keeps it in its cache; the bytes are read in pieces and
        thrown away. Only a PAGE_PATH is requested (ValueError otherwise),
        never through gq. Busy answers (429, 5xx) are the image server's, not
        Suwayomi being down, so they neither trip the breaker nor raise. So is
        a timeout, unless Suwayomi then does not answer a status query either
        (as after a source request in gq): SuwayomiUnreachable, so a hung
        Suwayomi is not taken for a refusing image server. A refused
        connection trips the breaker and raises SuwayomiUnreachable, and an
        open breaker fails at once. A cancel (cancellable copy) cuts the
        request short with limits.Cancelled."""
        if not isinstance(path, str) or not PAGE_PATH.match(path):
            raise ValueError(f"not a Suwayomi page path: {str(path)[:100]!r}")
        down = self._check_breaker()
        t0 = time.monotonic()
        try:
            status, nbytes, truncated = limits.interruptible(
                lambda: outbound.drain(self.base + path, timeout=timeout, max_bytes=max_bytes,
                                       what="Suwayomi page URL"), self._cancel)
        except limits.Cancelled:
            raise
        except urllib.error.HTTPError as e:
            try:
                body = e.read(1024)
            except Exception:
                body = b""
            finally:
                e.close()
            secs = time.monotonic() - t0
            kind = "busy" if e.code in _PAGE_BUSY else "gone" if e.code in _PAGE_GONE else "error"
            log.debug("suwayomi page %s -> HTTP %d (%s) in %.1fs: %r", path, e.code, kind, secs, body[:200])
            return PageFetch(kind, e.code, 0, secs)
        except Exception as e:
            secs = time.monotonic() - t0
            kind = _failure_kind(e)
            if kind == "connect":
                self._breaker_trip(f"{type(e).__name__}: {e}")
                raise SuwayomiUnreachable(f"Suwayomi at {self.base} unreachable: {e}") from e
            log.debug("suwayomi page %s -> %s after %.1fs: %s", path, kind, secs, e)
            if kind == "timeout" and timeout >= PROBE_SECS:     # a short one proves nothing either way
                self._check_answers("a page request", timeout)
            return PageFetch("timeout" if kind == "timeout" else "error", None, 0, secs)
        secs = time.monotonic() - t0
        if down:
            self._breaker_close()
        if truncated:
            log.warning("page %s is larger than %d bytes; stopped reading it", path, max_bytes)
        log.debug("suwayomi page %s -> HTTP %d, %d bytes in %.1fs", path, status, nbytes, secs)
        return PageFetch("ok", status, nbytes, secs)

    def mangas_page(self, offset: int, first: int = 500) -> tuple[list[dict], bool]:
        """One page of every entry Suwayomi has cached (every search hit is
        cached, so the whole list can run to tens of thousands): returns
        ([{id, title, downloadCount, source: {displayName}}], more pages)."""
        d = self.gq('query($first: Int!, $offset: Int!) { mangas(first: $first, offset: $offset)'
                    ' { nodes { id title downloadCount source { displayName } } pageInfo { hasNextPage } } }',
                    {"first": first, "offset": offset}, timeout=60, retries=1)
        nodes = d["mangas"]["nodes"]
        return nodes, bool(nodes) and bool((d["mangas"].get("pageInfo") or {}).get("hasNextPage"))

    def max_sources_in_parallel(self) -> int | None:
        """Suwayomi's own 'max sources in parallel': how many sources its
        downloader fetches from at once. None when it cannot be read."""
        try:
            d = self.gq("{ settings { maxSourcesInParallel } }", timeout=30, retries=1)
            return int(d["settings"]["maxSourcesInParallel"])
        except (SuwayomiError, KeyError, TypeError, ValueError) as e:
            log.debug("could not read Suwayomi's max sources in parallel: %s: %s", type(e).__name__, e)
            return None

    def set_max_sources_in_parallel(self, n: int) -> int:
        """Change Suwayomi's global 'max sources in parallel'; returns the
        value it now has. Only ever on the user's request (Settings)."""
        d = self.gq('mutation($n: Int!) { setSettings(input: {settings: {maxSourcesInParallel: $n}})'
                    ' { settings { maxSourcesInParallel } } }', {"n": int(n)}, timeout=30, retries=1)
        return int(d["setSettings"]["settings"]["maxSourcesInParallel"])

    def set_in_library(self, manga_id: int, in_library: bool, retries: int = 3, timeout: int = 60) -> None:
        self.gq('mutation($id: Int!, $v: Boolean!) {'
                ' updateManga(input: {id: $id, patch: {inLibrary: $v}}) { manga { id } } }',
                {"id": manga_id, "v": in_library}, timeout=timeout, retries=retries)

    def downloaded_chapter_ids(self, manga_id: int, timeout: int = 60) -> list[int]:
        """Ids of the chapters Suwayomi holds downloaded files for, from its cache (no source fetch)."""
        d = self.gq('query($id: Int!) { manga(id: $id) { chapters { nodes { id isDownloaded } } } }',
                    {"id": manga_id}, timeout=timeout, retries=1)
        return [c["id"] for c in d["manga"]["chapters"]["nodes"] if c["isDownloaded"]]

    def delete_downloads(self, chapter_ids: list[int], timeout: int = 120) -> None:
        """Delete the downloaded files of these chapters. Suwayomi removes them
        from its download folder itself: it is the only writer there (mang-arr
        mounts that folder read-only)."""
        if chapter_ids:
            self.gq('mutation($ids: [Int!]!) { deleteDownloadedChapters(input: {ids: $ids}) { clientMutationId } }',
                    {"ids": chapter_ids}, timeout=timeout, retries=1)

    # -- downloader -------------------------------------------------------
    # Only our own chapter ids are ever touched: Suwayomi's queue is shared
    # with its own library updates and whatever the user queued in its UI.

    def enqueue(self, chapter_ids: list[int]) -> None:
        self.gq('mutation($ids: [Int!]!) { enqueueChapterDownloads(input: {ids: $ids}) { clientMutationId } }',
                {"ids": chapter_ids}, timeout=60)

    def dequeue(self, chapter_ids: list[int], timeout: int = 30) -> None:
        if chapter_ids:
            self.gq('mutation($ids: [Int!]!) { dequeueChapterDownloads(input: {ids: $ids}) { clientMutationId } }',
                    {"ids": chapter_ids}, timeout=timeout, retries=1)

    def start(self) -> None:
        self.gq("mutation { startDownloader(input: {}) { clientMutationId } }", timeout=60)

    def stop(self) -> None:
        self.gq("mutation { stopDownloader(input: {}) { clientMutationId } }", retries=1)

    def queue(self) -> list[dict]:
        """[{id, state, tries, progress}] for every item in Suwayomi's queue."""
        d = self.gq("{ downloadStatus { queue { chapter { id } state tries progress } } }", timeout=30, retries=2)
        return [{"id": x["chapter"]["id"], "state": x["state"], "tries": x["tries"],
                 "progress": x.get("progress") or 0.0} for x in d["downloadStatus"]["queue"]]


def with_cancel(client, should_cancel: Callable[[], bool] | None):
    """client.cancellable(should_cancel) for a real Client when there is a
    cancel to watch; anything else (no cancel, a test double) unchanged."""
    if should_cancel is None or not isinstance(client, Client):
        return client
    return client.cancellable(should_cancel)


def dedupe(raw: list[dict]) -> list[Chapter]:
    """One entry per chapter number. Comick-style sources list every scanlation
    group separately; keep the group with the widest coverage so a series is
    not stitched from five translations."""
    cov: dict[str, set] = {}
    for c in raw:
        cov.setdefault(c.get("scanlator") or "", set()).add(c["chapterNumber"])
    rank = {s: i for i, s in enumerate(sorted(cov, key=lambda s: (-len(cov[s]), s)))}
    best: dict[float, dict] = {}
    for c in raw:
        n = float(c["chapterNumber"])
        if n < 0:                                   # Suwayomi uses -1 for "unknown"
            continue
        s = c.get("scanlator") or ""
        if n not in best or rank[s] < rank[best[n].get("scanlator") or ""]:
            best[n] = c
    return sorted((Chapter(c["id"], n, c.get("name"), c.get("scanlator"), bool(c.get("isDownloaded")),
                           _iso_date(c.get("uploadDate")))
                   for n, c in best.items()), key=lambda c: c.number)


def _iso_date(ms) -> str | None:
    """Suwayomi gives upload dates as epoch milliseconds (0 = unknown)."""
    try:
        ms = int(ms or 0)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))

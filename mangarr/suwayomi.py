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

from . import config, limits

log = logging.getLogger(__name__)


class SuwayomiError(RuntimeError):
    pass


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
                     r"clearDownloader|updateManga|sources|mangas|manga)\b")


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    lang: str
    unusable: bool = False      # searched, never downloaded from (Settings)
    throttled: bool = False     # single-chapter batches, loses close calls (Settings)

    @property
    def key(self) -> str:
        return self.name.lower().strip()


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
        self.api = url.rstrip("/") + "/api/graphql"

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
        with _down_lock:
            down = _down.get(self.api)
        if down and time.monotonic() - down[0] < BREAKER_SECS:
            raise CircuitOpen(f"Suwayomi at {self.api} unreachable: {down[1]} (not retrying for "
                              f"{BREAKER_SECS - (time.monotonic() - down[0]):.0f} s)")
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
                    raise SuwayomiError(msg)
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
        """Installed sources, with the disabled/throttled flags from Settings
        stamped once so a run is consistent even if settings change."""
        from . import settings
        v = settings.all_values()
        unusable, throttled = set(v["unusable_sources"]), set(v["throttled_sources"])
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
            out.append(Source(s["id"], s["displayName"], s["lang"], key in unusable, key in throttled))
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

    def downloaded_ids(self, manga_id: int) -> set[int]:
        d = self.gq('query($id: Int!) { manga(id: $id) { chapters { nodes { id isDownloaded } } } }',
                    {"id": manga_id}, timeout=60, retries=2)
        return {c["id"] for c in d["manga"]["chapters"]["nodes"] if c["isDownloaded"]}

    def page_count(self, chapter_id: int) -> int | None:
        """How many pages a chapter has (fetches the page list from the
        source). None when the source will not say."""
        try:
            d = self.gq('mutation($id: Int!) { fetchChapterPages(input: {chapterId: $id}) { pages } }',
                        {"id": chapter_id}, timeout=60, retries=1)
            return len(d["fetchChapterPages"]["pages"])
        except SuwayomiError:
            return None

    def mangas_page(self, offset: int, first: int = 500) -> tuple[list[dict], bool]:
        """One page of every entry Suwayomi has cached (every search hit is
        cached, so the whole list can run to tens of thousands): returns
        ([{id, title, downloadCount, source: {displayName}}], more pages)."""
        d = self.gq('query($first: Int!, $offset: Int!) { mangas(first: $first, offset: $offset)'
                    ' { nodes { id title downloadCount source { displayName } } pageInfo { hasNextPage } } }',
                    {"first": first, "offset": offset}, timeout=60, retries=1)
        nodes = d["mangas"]["nodes"]
        return nodes, bool(nodes) and bool((d["mangas"].get("pageInfo") or {}).get("hasNextPage"))

    def set_in_library(self, manga_id: int, in_library: bool, retries: int = 3, timeout: int = 60) -> None:
        self.gq('mutation($id: Int!, $v: Boolean!) {'
                ' updateManga(input: {id: $id, patch: {inLibrary: $v}}) { manga { id } } }',
                {"id": manga_id, "v": in_library}, timeout=timeout, retries=retries)

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

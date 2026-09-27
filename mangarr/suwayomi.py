"""Suwayomi GraphQL client. Suwayomi is the download engine; this is the only
module that talks to it."""
import json
import logging
import re
import time
import urllib.request
from dataclasses import dataclass

from . import config

log = logging.getLogger(__name__)


class SuwayomiError(RuntimeError):
    pass


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
    def __init__(self, url: str = config.SUWAYOMI_URL):
        self.api = url.rstrip("/") + "/api/graphql"

    # -- transport --------------------------------------------------------

    def gq(self, query: str, variables: dict | None = None, timeout: int = 180, retries: int = 3) -> dict:
        body: dict = {"query": query}
        if variables:
            body["variables"] = variables
        last = None
        m = _OPNAME.search(query)
        op = m.group(1) if m else "query"
        for attempt in range(1, retries + 1):
            t0 = time.monotonic()
            try:
                req = urllib.request.Request(self.api, json.dumps(body).encode(),
                                             {"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    d = json.load(r)
                if "errors" in d:
                    msg = d["errors"][0]["message"].split("\n")[0][:200]
                    log.debug("suwayomi %s %s -> error in %.1fs: %s", op, variables or "",
                              time.monotonic() - t0, msg)
                    raise SuwayomiError(msg)
                log.debug("suwayomi %s %s -> ok in %.1fs", op, variables or "", time.monotonic() - t0)
                return d["data"]
            except SuwayomiError:
                raise
            except Exception as e:
                last = e
                log.debug("suwayomi %s attempt %d/%d failed after %.1fs: %s", op, attempt, retries,
                          time.monotonic() - t0, e)
                if attempt < retries:
                    time.sleep(5)
        raise SuwayomiError(f"Suwayomi at {self.api} unreachable: {last}")

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
        d = self.gq("{ sources { nodes { id displayName lang } } }")
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
                    ' { id name chapterNumber scanlator isDownloaded uploadDate } } } }', {"id": manga_id})
        return dedupe(d["manga"]["chapters"]["nodes"])

    def downloaded_ids(self, manga_id: int) -> set[int]:
        d = self.gq('query($id: Int!) { manga(id: $id) { chapters { nodes { id isDownloaded } } } }',
                    {"id": manga_id})
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

    def set_in_library(self, manga_id: int, in_library: bool, retries: int = 3, timeout: int = 60) -> None:
        self.gq('mutation($id: Int!, $v: Boolean!) {'
                ' updateManga(input: {id: $id, patch: {inLibrary: $v}}) { manga { id } } }',
                {"id": manga_id, "v": in_library}, timeout=timeout, retries=retries)

    # -- downloader -------------------------------------------------------
    # Only our own chapter ids are ever touched: Suwayomi's queue is shared
    # with its own library updates and whatever the user queued in its UI.

    def enqueue(self, chapter_ids: list[int]) -> None:
        self.gq('mutation($ids: [Int!]!) { enqueueChapterDownloads(input: {ids: $ids}) { clientMutationId } }',
                {"ids": chapter_ids})

    def dequeue(self, chapter_ids: list[int]) -> None:
        if chapter_ids:
            self.gq('mutation($ids: [Int!]!) { dequeueChapterDownloads(input: {ids: $ids}) { clientMutationId } }',
                    {"ids": chapter_ids}, retries=1)

    def start(self) -> None:
        self.gq("mutation { startDownloader(input: {}) { clientMutationId } }")

    def stop(self) -> None:
        self.gq("mutation { stopDownloader(input: {}) { clientMutationId } }", retries=1)

    def queue(self) -> list[dict]:
        """[{id, state, tries, progress}] for every item in Suwayomi's queue."""
        d = self.gq("{ downloadStatus { queue { chapter { id } state tries progress } } }")
        return [{"id": x["chapter"]["id"], "state": x["state"], "tries": x["tries"],
                 "progress": x.get("progress") or 0.0} for x in d["downloadStatus"]["queue"]]


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

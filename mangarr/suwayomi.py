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
                     r"enqueueChapterDownload|startDownloader|stopDownloader|clearDownloader|"
                     r"updateManga|sources|mangas|manga)\b")


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    lang: str

    @property
    def key(self) -> str:
        return self.name.lower().strip()

    @property
    def unusable(self) -> bool:
        return self.key in config.UNUSABLE_SOURCES

    @property
    def throttled(self) -> bool:
        return self.key in config.THROTTLED_SOURCES


@dataclass
class Chapter:
    id: int
    number: float
    name: str | None
    scanlator: str | None
    downloaded: bool


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
                    log.debug("suwayomi %s %s -> error in %.1fs: %s", op, variables or "", time.monotonic() - t0, msg)
                    raise SuwayomiError(msg)
                log.debug("suwayomi %s %s -> ok in %.1fs", op, variables or "", time.monotonic() - t0)
                return d["data"]
            except SuwayomiError:
                raise
            except Exception as e:
                last = e
                log.debug("suwayomi %s attempt %d/%d failed after %.1fs: %s", op, attempt, retries,
                          time.monotonic() - t0, e)
                time.sleep(5)
        raise SuwayomiError(f"API unreachable: {last}")

    # -- sources / search -------------------------------------------------

    def sources(self, langs=("en", "all")) -> list[Source]:
        d = self.gq("{ sources { nodes { id displayName lang } } }")
        return [Source(s["id"], s["displayName"], s["lang"])
                for s in d["sources"]["nodes"]
                if s["lang"] in langs and s["displayName"] != "Local source"]

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
            '   chapters { id name chapterNumber scanlator isDownloaded } } }',
            {"id": manga_id}, timeout=240, retries=1)
        r = d["fetchMangaAndChapters"]
        return r["manga"], dedupe(r["chapters"])

    def chapters(self, manga_id: int) -> list[Chapter]:
        """Chapter list from Suwayomi's cache (no source fetch)."""
        d = self.gq('query($id: Int!) { manga(id: $id) { chapters { nodes'
                    ' { id name chapterNumber scanlator isDownloaded } } } }', {"id": manga_id})
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

    def set_in_library(self, manga_id: int, in_library: bool) -> None:
        self.gq('mutation($id: Int!, $v: Boolean!) {'
                ' updateManga(input: {id: $id, patch: {inLibrary: $v}}) { manga { id } } }',
                {"id": manga_id, "v": in_library})

    # -- downloader -------------------------------------------------------

    def enqueue(self, chapter_ids: list[int]) -> None:
        for cid in chapter_ids:
            self.gq('mutation($id: Int!) { enqueueChapterDownload(input: {id: $id}) { clientMutationId } }',
                    {"id": cid})

    def start(self) -> None:
        self.gq("mutation { startDownloader(input: {}) { clientMutationId } }")

    def stop(self) -> None:
        self.gq("mutation { stopDownloader(input: {}) { clientMutationId } }")

    def clear(self) -> None:
        self.gq("mutation { clearDownloader(input: {}) { clientMutationId } }")

    def queue(self) -> list[dict]:
        d = self.gq("{ downloadStatus { queue { chapter { id } state tries progress } } }")
        return d["downloadStatus"]["queue"]


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
    return sorted((Chapter(c["id"], float(c["chapterNumber"]), c.get("name"),
                           c.get("scanlator"), bool(c.get("isDownloaded")))
                   for c in best.values()), key=lambda c: c.number)

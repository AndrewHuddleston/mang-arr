"""MangaDex lookup: the second series database.

MangaDex carries the Western webtoons AniList lacks (Let's Play, most
WEBTOON originals) and indexes alternate titles in many languages. It is
consulted when AniList has no exact match. Its community's English chapter
list is also evidence for verdict.py (english_chapters).
"""
import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field

from . import config
from .anilist import retry_after
from .matching import oneline, query_score
from .model import Series

log = logging.getLogger(__name__)

_STATUS = {"ongoing": "RELEASING", "completed": "FINISHED", "hiatus": "HIATUS", "cancelled": "CANCELLED"}
_COUNTRY = {"ja": "JP", "ko": "KR", "zh": "CN", "zh-hk": "CN", "en": "US", "fr": "FR", "es": "ES"}
_TITLE_LANGS = ("en", "ja-ro", "ko-ro", "zh-ro", "ja", "ko", "zh", "zh-hk")
_ALL_RATINGS = [("contentRating[]", r) for r in ("safe", "suggestive", "erotica", "pornographic")]


def _request(path: str, params: list[tuple[str, str]], timeout: float):
    """One GET of the API, decoded; raises what urlopen and json raise."""
    url = f"{config.MANGADEX_URL}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    log.debug("mangadex %s -> ok in %.1fs", path, time.monotonic() - t0)
    return d


def _get(path: str, params: list[tuple[str, str]], retries: int = 3) -> dict:
    last: Exception | None = None
    for _ in range(retries):
        try:
            return _request(path, params, 30)
        except urllib.error.HTTPError as e:
            e.close()                    # the answer's connection: only its status and headers are used
            if e.code == 429:
                wait = retry_after(e.headers, 5)
                log.debug("mangadex rate limited, waiting %gs", wait)
                last = e
                time.sleep(wait)
                continue
            if e.code == 404:
                return {}
            last = e
            time.sleep(3)
        except Exception as e:
            last = e
            time.sleep(3)
    raise RuntimeError(f"MangaDex unreachable: {last}")


def _anilist_link(a: dict) -> int | None:
    """The AniList id the record links to (attributes.links.al), when it is one."""
    links = a.get("links")
    al = str(links.get("al") or "").strip() if isinstance(links, dict) else ""
    return int(al) if len(al) <= 9 and al.isascii() and al.isdigit() and int(al) > 0 else None


def _to_series(m: dict) -> Series:
    a = m.get("attributes") or {}
    title_map = a.get("title") or {}
    alts = a.get("altTitles") or []
    english = title_map.get("en")
    romaji = next((t.get(k) for t in alts for k in ("ja-ro", "ko-ro", "zh-ro") if t.get(k)), None)
    if not english and title_map:
        # non-English primary title: keep it as the "romaji" slot if Latin
        primary = next(iter(title_map.values()))
        if not romaji:
            romaji = primary
    native = next((t.get(k) for t in [title_map, *alts] for k in ("ja", "ko", "zh", "zh-hk") if t.get(k)),
                  None)
    synonyms, seen = [], {english, romaji, native}         # a set: linear however many alt titles come back
    for t in alts:
        for k, v in t.items():
            if v and isinstance(v, str) and k in _TITLE_LANGS and v not in seen:
                seen.add(v)
                synonyms.append(v)
    authors, cover = [], None
    for rel in m.get("relationships") or []:
        ra = rel.get("attributes") or {}
        if rel.get("type") in ("author", "artist") and ra.get("name") and ra["name"] not in authors:
            authors.append(ra["name"])
        if rel.get("type") == "cover_art" and ra.get("fileName"):
            cover = f"https://uploads.mangadex.org/covers/{m['id']}/{ra['fileName']}.256.jpg"
    status = _STATUS.get(a.get("status") or "", None)
    chapters = None
    if status == "FINISHED" and a.get("lastChapter"):
        try:
            chapters = int(float(a["lastChapter"]))
        except ValueError:
            pass
    genres = [((t.get("attributes") or {}).get("name") or {}).get("en") for t in (a.get("tags") or [])
              if (t.get("attributes") or {}).get("group") == "genre"]
    return Series(
        genres=[g for g in genres if g], year=a.get("year"),
        demographic=a.get("publicationDemographic"),
        mangadex_id=m["id"], romaji=romaji, english=english, native=native, synonyms=synonyms,
        format="MANGA", country=_COUNTRY.get(a.get("originalLanguage") or "", None),
        status=status, chapters=chapters,
        adult=(a.get("contentRating") in ("erotica", "pornographic")),
        cover=cover, description=(a.get("description") or {}).get("en"), authors=authors,
        anilist_link=_anilist_link(a),
    )


def search(query: str, limit: int = 12) -> list[Series]:
    params = [("title", query), ("limit", str(limit)), ("order[relevance]", "desc"),
              ("includes[]", "author"), ("includes[]", "artist"), ("includes[]", "cover_art"),
              ("contentRating[]", "safe"), ("contentRating[]", "suggestive"),
              ("contentRating[]", "erotica")]
    d = _get("/manga", params)
    out = [_to_series(m) for m in d.get("data") or []]
    out.sort(key=lambda s: min(query_score(t, query) for t in s.titles) if s.titles else 9)
    return out


def by_id(uuid: str) -> Series | None:
    d = _get(f"/manga/{uuid}", [("includes[]", "author"), ("includes[]", "artist"),
                                 ("includes[]", "cover_art")])
    m = d.get("data")
    return _to_series(m) if m else None


IDS_PER_REQUEST = 100    # MangaDex's cap on ids[] (and limit) per /manga request


def statuses(uuids: list[str]) -> dict[str, tuple[str | None, int | None]]:
    """{uuid: (status, chapter count)} for these records, IDS_PER_REQUEST
    per request, as _to_series reads them (a chapter count only for a
    finished series). A record MangaDex no longer has is left out. Raises
    RuntimeError when MangaDex cannot be reached."""
    out: dict[str, tuple[str | None, int | None]] = {}
    for i in range(0, len(uuids), IDS_PER_REQUEST):
        batch = uuids[i:i + IDS_PER_REQUEST]
        params = [("ids[]", u) for u in batch] + [("limit", str(len(batch))), *_ALL_RATINGS]
        for m in _get("/manga", params).get("data") or []:
            a = m.get("attributes") if isinstance(m, dict) and m.get("id") in batch else None
            if isinstance(a, dict):
                s = _to_series({"id": m["id"], "attributes": a})
                out[m["id"]] = (s.status, s.chapters)
    return out


def anilist_links(uuids: list[str]) -> dict[str, int | None]:
    """{uuid: the AniList id its record links to, or None} for these records,
    IDS_PER_REQUEST per request, whatever their content rating. A record
    MangaDex no longer has is left out. Raises RuntimeError when MangaDex
    cannot be reached."""
    out: dict[str, int | None] = {}
    for i in range(0, len(uuids), IDS_PER_REQUEST):
        batch = uuids[i:i + IDS_PER_REQUEST]
        params = [("ids[]", u) for u in batch] + [("limit", str(len(batch))), *_ALL_RATINGS]
        for m in _get("/manga", params).get("data") or []:
            a = m.get("attributes") if isinstance(m, dict) and m.get("id") in batch else None
            if isinstance(a, dict):
                out[m["id"]] = _anilist_link(a)
    return out


# -- English chapter list --------------------------------------------------
# Evidence for the "stuck behind chapter N" verdict (verdict.py): which
# chapter numbers from N to N+1 the MangaDex community lists in English, with
# their titles and page counts. Read-only and best effort: the requests are
# paced, never wait long (for their turn, on a timeout or on a rate limit),
# the answers are kept for a week, and a lookup never raises - a failure only
# means no evidence.

CHAPTERS_TTL = 7 * 86400        # seconds an answer is kept
FAILED_TTL = 3600               # a lookup that failed is not tried again for an hour
CACHE_SIZE = 256                # answers kept; the least recently used goes first
CHAPTERS_TIMEOUT = 10           # seconds per request, two tries
CHAPTERS_GAP = 1.0              # seconds between the starts of these requests (MangaDex allows about 5 a second)
CHAPTERS_MAX_WAIT = 30          # seconds a request may wait for its turn; longer and the lookup gives up
MAX_NEAR = 100                  # chapters from N to N+1 looked at (one /chapter request takes 100 ids)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass
class MdChapter:
    """One chapter of MangaDex's English list."""
    number: float
    title: str | None = None
    pages: int | None = None    # None when hosted elsewhere (an official site): MangaDex counts 0 or 1 page there


@dataclass
class ChapterList:
    """MangaDex's English chapters of one series around one whole chapter N."""
    manga_id: str | None                        # None: no MangaDex entry found is tied to the series
    count: int = 0                              # English chapters it lists for the whole series
    near: dict = field(default_factory=dict)    # {number: MdChapter} for every one from N up to N+1


class _Cache:
    """Answers kept in memory: each expires after its own ttl, and past
    `size` entries the least recently used is dropped."""

    def __init__(self, size: int):
        self.size = size
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key) -> tuple[bool, object]:
        """(True, value) while `key` is cached, else (False, None)."""
        with self._lock:
            hit = self._d.get(key)
            if hit is None:
                return False, None
            if hit[0] <= time.monotonic():
                del self._d[key]
                return False, None
            self._d.move_to_end(key)
            return True, hit[1]

    def put(self, key, value, ttl: float) -> None:
        with self._lock:
            self._d[key] = (time.monotonic() + ttl, value)
            self._d.move_to_end(key)
            while len(self._d) > self.size:
                self._d.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


_cache = _Cache(CACHE_SIZE)
_pace_lock = threading.Lock()   # guards _next_turn only: never held while waiting or during a request
_next_turn = 0.0                # time.monotonic() from which the next request may start


def _take_turn() -> None:
    """Wait until this request may start: CHAPTERS_GAP after the one before
    it (from any thread), and not before the end of a pause a 429 asked for.
    Raises instead when that is more than CHAPTERS_MAX_WAIT away."""
    global _next_turn
    with _pace_lock:
        now = time.monotonic()
        start = max(now, _next_turn)
        if start - now > CHAPTERS_MAX_WAIT:
            raise RuntimeError(f"MangaDex busy: the next request may start in {start - now:.0f}s")
        _next_turn = start + CHAPTERS_GAP
    if start > now:
        time.sleep(start - now)


def _hold_off(seconds: float) -> None:
    """Start none of these requests for `seconds` (a 429's Retry-After)."""
    global _next_turn
    with _pace_lock:
        _next_turn = max(_next_turn, time.monotonic() + seconds)


def _paced_get(path: str, params: list[tuple[str, str]]) -> dict:
    """A request for these background lookups: two tries, each started in
    its turn and given CHAPTERS_TIMEOUT. A 429 is not waited out here: it
    holds off every lookup for its Retry-After and ends this one at once."""
    last: Exception | None = None
    for _ in range(2):
        _take_turn()
        try:
            d = _request(path, params, CHAPTERS_TIMEOUT)
            return d if isinstance(d, dict) else {}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            if e.code == 429:
                try:
                    wait = retry_after(e.headers, 5)
                except RuntimeError:            # more than anilist.MAX_RETRY_AFTER
                    wait = FAILED_TTL
                _hold_off(wait)
                raise RuntimeError(f"MangaDex rate limited; no requests for {wait:.0f}s") from e
            last = e
        except Exception as e:
            last = e
    raise RuntimeError(f"MangaDex unreachable: {last}")


def _number(raw) -> float | None:
    """A chapter number as MangaDex writes it ('7', '171.14'), or None for
    'none' (a oneshot) and anything else that is not a plain number."""
    try:
        n = float(str(raw)[:20])
    except ValueError:
        return None
    return round(n, 4) if math.isfinite(n) and 0 <= n < 1e6 else None


def _manga_id(s: Series) -> str | None:
    """The series' MangaDex id: its own, or the MangaDex entry that links to
    its AniList id. Never a title match alone - same-titled anthologies and
    spin-offs are exactly what matching.py warns about."""
    if s.mangadex_id:
        return s.mangadex_id if _UUID.match(s.mangadex_id) else None
    if s.anilist_id is None:
        return None
    for title in dict.fromkeys(t[:200] for t in (s.romaji, s.english) if t):
        d = _paced_get("/manga", [("title", title), ("limit", "10"), *_ALL_RATINGS])
        for m in d.get("data") or []:
            a = m.get("attributes") if isinstance(m, dict) else None
            mid = str(m.get("id") or "") if isinstance(m, dict) else ""
            if isinstance(a, dict) and _anilist_link(a) == s.anilist_id and _UUID.match(mid):
                return mid
    return None


def _aggregate(uuid: str) -> dict[float, str]:
    """{chapter number: MangaDex chapter id} of every English chapter."""
    d = _paced_get(f"/manga/{uuid}/aggregate", [("translatedLanguage[]", "en")])
    vols = d.get("volumes") or {}
    out: dict[float, str] = {}
    for v in vols.values() if isinstance(vols, dict) else vols:     # no chapters: [] rather than {}
        chs = v.get("chapters") or {} if isinstance(v, dict) else {}
        for c in chs.values() if isinstance(chs, dict) else chs:
            if isinstance(c, dict):
                n, cid = _number(c.get("chapter")), str(c.get("id") or "")
                if n is not None and _UUID.match(cid):
                    out.setdefault(n, cid)
    return out


def _details(ids: dict[float, str]) -> dict[float, MdChapter]:
    """Title and page count of these chapters ({number: chapter id})."""
    by_id = {cid: n for n, cid in ids.items()}
    out = {n: MdChapter(n) for n in ids}
    if not by_id:
        return out
    d = _paced_get("/chapter", [*(("ids[]", cid) for cid in by_id), ("limit", "100"), *_ALL_RATINGS])
    for c in d.get("data") or []:
        n = by_id.get(c.get("id")) if isinstance(c, dict) else None
        if n is None:
            continue
        a = c.get("attributes") or {}
        title, pages = a.get("title"), a.get("pages")
        out[n] = MdChapter(n, (oneline(title, 200).strip() or None) if isinstance(title, str) else None,
                           pages if type(pages) is int and pages > 0 and not a.get("externalUrl") else None)
    return out


def _chapters_near(s: Series, number: float) -> ChapterList:
    hit, uuid = _cache.get(("id", s.ref))
    if not hit:
        uuid = _manga_id(s)
        _cache.put(("id", s.ref), uuid, CHAPTERS_TTL)
    if not uuid:
        return ChapterList(None)
    agg = _aggregate(uuid)
    number, whole = round(number, 4), math.floor(number)
    near = sorted((n for n in agg if whole <= n < whole + 1),       # N and the asked-for one first
                  key=lambda n: (n != whole, n != number, abs(n - number)))[:MAX_NEAR]
    return ChapterList(uuid, len(agg), dict(sorted(_details({n: agg[n] for n in near}).items())))


def english_chapters(s: Series, number: float, fetch: bool = True) -> ChapterList | None:
    """MangaDex's English chapters of this series numbered from the whole
    chapter under `number` up to the next one (for 7.2: 7, 7.5 ...), or None
    when that cannot be known now: MangaDex unreachable or refusing (tried
    again after FAILED_TTL), a manual series, or - with fetch=False, for a
    caller that must not wait on the network - not looked up yet. A series
    no MangaDex entry is tied to (its own MangaDex id, or one found under its
    titles that links to its AniList id) gives a ChapterList with manga_id
    None."""
    if not math.isfinite(number) or s.manual:
        return None
    key = ("near", s.ref, math.floor(number))
    hit, value = _cache.get(key)
    if hit or not fetch:
        return value
    try:
        out = _chapters_near(s, number)
    except Exception as e:
        log.info("MangaDex chapter list for %s not available: %s: %s", oneline(s.title, 80), type(e).__name__,
                 oneline(e, 200))
        _cache.put(key, None, FAILED_TTL)
        return None
    _cache.put(key, out, CHAPTERS_TTL)
    return out

"""MangaDex lookup: the second series database.

MangaDex carries the Western webtoons AniList lacks (Let's Play, most
WEBTOON originals) and indexes alternate titles in many languages. It is
consulted when AniList has no exact match.
"""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .anilist import retry_after
from .matching import query_score
from .model import Series

log = logging.getLogger(__name__)

_STATUS = {"ongoing": "RELEASING", "completed": "FINISHED", "hiatus": "HIATUS", "cancelled": "CANCELLED"}
_COUNTRY = {"ja": "JP", "ko": "KR", "zh": "CN", "zh-hk": "CN", "en": "US", "fr": "FR", "es": "ES"}
_TITLE_LANGS = ("en", "ja-ro", "ko-ro", "zh-ro", "ja", "ko", "zh", "zh-hk")


def _get(path: str, params: list[tuple[str, str]], retries: int = 3) -> dict:
    url = f"{config.MANGADEX_URL}{path}?{urllib.parse.urlencode(params)}"
    last: Exception | None = None
    for _ in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            log.debug("mangadex %s -> ok in %.1fs", path, time.monotonic() - t0)
            return d
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


def anilist_links(uuids: list[str]) -> dict[str, int | None]:
    """{uuid: the AniList id its record links to, or None} for these records,
    IDS_PER_REQUEST per request, whatever their content rating. A record
    MangaDex no longer has is left out. Raises RuntimeError when MangaDex
    cannot be reached."""
    out: dict[str, int | None] = {}
    for i in range(0, len(uuids), IDS_PER_REQUEST):
        batch = uuids[i:i + IDS_PER_REQUEST]
        params = [("ids[]", u) for u in batch] + [("limit", str(len(batch)))] + \
            [("contentRating[]", r) for r in ("safe", "suggestive", "erotica", "pornographic")]
        for m in _get("/manga", params).get("data") or []:
            a = m.get("attributes") if isinstance(m, dict) and m.get("id") in batch else None
            if isinstance(a, dict):
                out[m["id"]] = _anilist_link(a)
    return out

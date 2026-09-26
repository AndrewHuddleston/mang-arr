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
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "5"))
                log.debug("mangadex rate limited, waiting %ds", wait)
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
    native = next((t.get(k) for t in [title_map, *alts] for k in ("ja", "ko", "zh", "zh-hk") if t.get(k)), None)
    synonyms = []
    for t in alts:
        for k, v in t.items():
            if v and v not in (english, romaji, native) and k in _TITLE_LANGS and v not in synonyms:
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
    return Series(
        mangadex_id=m["id"], romaji=romaji, english=english, native=native, synonyms=synonyms,
        format="MANGA", country=_COUNTRY.get(a.get("originalLanguage") or "", None),
        status=status, chapters=chapters,
        adult=(a.get("contentRating") in ("erotica", "pornographic")),
        cover=cover, description=(a.get("description") or {}).get("en"), authors=authors,
    )


def search(query: str, limit: int = 8) -> list[Series]:
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

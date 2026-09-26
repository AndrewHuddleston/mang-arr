"""AniList lookup: the primary series database.

AniList gives every title a series is known by (romaji, English, native,
synonyms), the country of origin (reading direction), status and - for
finished series - the real chapter count. It covers Japanese, Korean and
Chinese comics well and Western webtoons poorly; see mangadex.py for those.
"""
import json
import logging
import time
import urllib.error
import urllib.request

from . import config
from .matching import query_score
from .model import Series

log = logging.getLogger(__name__)

_FIELDS = """
  id
  title { romaji english native }
  synonyms
  format
  countryOfOrigin
  status
  chapters
  volumes
  isAdult
  popularity
  genres
  startDate { year }
  tags { name category }
  coverImage { large }
  description(asHtml: false)
  staff(perPage: 4, sort: RELEVANCE) { edges { role node { name { full native } } } }
"""

_SEARCH = ("query($q: String, $n: Int) { Page(page: 1, perPage: $n) {"  # noqa: UP031
           " media(search: $q, type: MANGA) { %s } } }" % _FIELDS)
_BY_ID = "query($id: Int) { Media(id: $id, type: MANGA) { %s } }" % _FIELDS  # noqa: UP031


def _post(query: str, variables: dict, retries: int = 3) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    last: Exception | None = None
    for _ in range(retries):
        req = urllib.request.Request(
            config.ANILIST_URL, body,
            {"Content-Type": "application/json", "Accept": "application/json",
             "User-Agent": config.USER_AGENT})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            log.debug("anilist %s -> ok in %.1fs", variables, time.monotonic() - t0)
            return d
        except urllib.error.HTTPError as e:
            if e.code == 429:            # 30 requests/minute
                wait = int(e.headers.get("Retry-After", "10"))
                log.debug("anilist rate limited, waiting %ds", wait)
                last = e
                time.sleep(wait)
                continue
            if e.code == 404:            # unknown id
                return {}
            last = e
            if e.code >= 500:
                time.sleep(3)
                continue
            raise
        except Exception as e:           # network blip
            last = e
            time.sleep(3)
    raise RuntimeError(f"AniList unreachable: {last}")


def _to_series(m: dict) -> Series:
    t = m.get("title") or {}
    authors = []
    for e in (m.get("staff") or {}).get("edges", []):
        n = (e.get("node") or {}).get("name") or {}
        for v in (n.get("full"), n.get("native")):
            if v and v not in authors:
                authors.append(v)
    return Series(
        anilist_id=m["id"],
        romaji=t.get("romaji"), english=t.get("english"), native=t.get("native"),
        synonyms=[s for s in (m.get("synonyms") or []) if s],
        format=m.get("format"), country=m.get("countryOfOrigin"),
        status=m.get("status"), chapters=m.get("chapters"), volumes=m.get("volumes"),
        adult=bool(m.get("isAdult")),
        popularity=m.get("popularity") or 0,
        cover=(m.get("coverImage") or {}).get("large"),
        description=m.get("description"),
        authors=authors,
        genres=[g for g in (m.get("genres") or []) if g],
        year=(m.get("startDate") or {}).get("year"),
        demographic=next((t["name"].lower() for t in (m.get("tags") or [])
                          if t.get("category") == "Demographic" and t.get("name")), None),
    )


def search(query: str, limit: int = 12) -> list[Series]:
    """AniList candidates for a typed title, best match first."""
    d = _post(_SEARCH, {"q": query, "n": limit})
    media = ((d.get("data") or {}).get("Page") or {}).get("media") or []
    out = [_to_series(m) for m in media]
    # Title closeness first, then how many people track it: a query like
    # "Let's Play" matches a dozen one-shots by synonym, and the series the
    # user means is the one with readers.
    out.sort(key=lambda s: (min(query_score(t, query) for t in s.titles) if s.titles else 9,
                            0 if s.format in ("MANGA", "ONE_SHOT") else 1,
                            -s.popularity))
    return out


def by_id(anilist_id: int) -> Series | None:
    d = _post(_BY_ID, {"id": anilist_id})
    m = (d.get("data") or {}).get("Media")
    return _to_series(m) if m else None

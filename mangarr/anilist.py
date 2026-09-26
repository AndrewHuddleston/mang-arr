"""AniList lookup: the series database.

A series is identified by its AniList ID. AniList gives every title the
series is known by (romaji, English, native, synonyms), which is what makes
searching the download sources reliable, plus the country of origin (reading
direction), status and - for finished series - the real chapter count.
"""
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import config
from .matching import query_score

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
  coverImage { large }
  description(asHtml: false)
  staff(perPage: 4, sort: RELEVANCE) { edges { role node { name { full native } } } }
"""

_SEARCH = "query($q: String, $n: Int) { Page(page: 1, perPage: $n) { media(search: $q, type: MANGA) { %s } } }" % _FIELDS
_BY_ID = "query($id: Int) { Media(id: $id, type: MANGA) { %s } }" % _FIELDS


@dataclass
class Series:
    anilist_id: int
    romaji: str | None
    english: str | None
    native: str | None
    synonyms: list[str] = field(default_factory=list)
    format: str | None = None
    country: str | None = None
    status: str | None = None
    chapters: int | None = None
    volumes: int | None = None
    adult: bool = False
    popularity: int = 0
    cover: str | None = None
    description: str | None = None
    authors: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.english or self.romaji or self.native or "?"

    @property
    def titles(self) -> list[str]:
        """Every name the series is known by, best first, no duplicates."""
        out, seen = [], set()
        for t in [self.romaji, self.english, self.native, *self.synonyms]:
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out

    @property
    def search_titles(self) -> list[str]:
        """Titles worth typing into a source search: Latin script ones first;
        the native title last, as only MangaDex-style sources index it."""
        latin = [t for t in self.titles if re.search(r"[A-Za-z]", t)]
        other = [t for t in self.titles if t not in latin]
        return latin + other


def _post(query: str, variables: dict, retries: int = 3) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    last: Exception | None = None
    for _ in range(retries):
        req = urllib.request.Request(
            config.ANILIST_URL, body,
            {"Content-Type": "application/json", "Accept": "application/json",
             "User-Agent": "mang-arr/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:            # 30 requests/minute
                time.sleep(int(e.headers.get("Retry-After", "10")))
                continue
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
    )


def search(query: str, limit: int = 8) -> list[Series]:
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

"""AniList lookup: the primary series database.

AniList gives every title a series is known by (romaji, English, native,
synonyms), the country of origin (reading direction), status and - for
finished series - the real chapter count. It covers Japanese, Korean and
Chinese comics well and Western webtoons poorly; see mangadex.py for those.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request

from . import config
from .matching import query_score
from .model import Series

log = logging.getLogger(__name__)

MAX_RETRY_AFTER = 120           # seconds; a longer wait gives up instead of stalling the job thread

# After BREAKER_AFTER lookups in a row get no usable answer (AniList down or
# hung, or asking us to wait longer than MAX_RETRY_AFTER), lookups fail at
# once for BREAKER_SECS instead of each paying up to 3 x 30 s: callers fall
# back as for any failure (a refresh keeps the stored record, a search tries
# MangaDex). The first lookup after the window goes through again.
BREAKER_AFTER = 2
BREAKER_SECS = 300
_failures = 0                   # lookups in a row that got no usable answer
_skip_until = 0.0               # monotonic time until which lookups fail at once
_breaker_lock = threading.Lock()


def retry_after(headers, default: float) -> float:
    """Seconds to wait before retrying a 429, from its Retry-After header:
    delta-seconds or an HTTP date, `default` when missing or unreadable,
    at least 1. Raises RuntimeError when the server asks for more than
    MAX_RETRY_AFTER, so a pass moves on instead of sleeping for hours."""
    raw = (headers.get("Retry-After") if headers is not None else None) or ""
    try:
        wait = float(raw)
    except ValueError:
        wait = None
        if raw:
            import email.utils
            try:
                when = email.utils.parsedate_to_datetime(raw)
                wait = when.timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                log.debug("unreadable Retry-After %r; waiting %gs", raw[:40], default)
    if wait is None or not wait == wait:                # missing, unreadable or NaN
        wait = default
    if wait > MAX_RETRY_AFTER:
        log.warning("rate limited with Retry-After %s; more than %ds, not waiting", raw[:40], MAX_RETRY_AFTER)
        raise RuntimeError(f"rate limited for {raw[:40]}s (more than {MAX_RETRY_AFTER}s); try again later")
    return max(wait, 1.0)


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
_STATUSES = ("query($ids: [Int], $n: Int) { Page(page: 1, perPage: $n) {"
             " media(id_in: $ids, type: MANGA) { id status chapters } } }")
IDS_PER_PAGE = 50               # AniList's cap on perPage


def _post(query: str, variables: dict, retries: int = 3) -> dict:
    """One AniList query, behind the breaker (see BREAKER_AFTER)."""
    with _breaker_lock:
        left, failures = _skip_until - time.monotonic(), _failures
    if left > 0:
        raise RuntimeError(f"AniList unreachable: {failures} lookups in a row failed, not asked again for "
                           f"{left:.0f} s")
    try:
        d = _send(query, variables, retries)
    except urllib.error.HTTPError:      # it answered (a 4xx): up, whatever was wrong with this query
        _answered()
        raise
    except Exception as e:
        _failed(e)
        raise
    _answered()
    return d


def _failed(e: Exception) -> None:
    global _failures, _skip_until
    with _breaker_lock:
        _failures += 1
        n = _failures
        if n >= BREAKER_AFTER:
            _skip_until = time.monotonic() + BREAKER_SECS
    if n == BREAKER_AFTER:
        log.warning("AniList failed %d lookups in a row (%s); not asking it again for %d min (refreshes keep "
                    "the stored details, searches use MangaDex)", n, e, BREAKER_SECS // 60)
    elif n > BREAKER_AFTER:
        log.debug("AniList still failing (%s); not asking it again for %d min", e, BREAKER_SECS // 60)


def _answered() -> None:
    global _failures, _skip_until
    with _breaker_lock:
        was, _failures, _skip_until = _failures, 0, 0.0
    if was >= BREAKER_AFTER:
        log.info("AniList answers again")


def _send(query: str, variables: dict, retries: int = 3) -> dict:
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
            e.close()                    # the answer's connection: only its status and headers are used
            if e.code == 429:            # 30 requests/minute
                wait = retry_after(e.headers, 10)
                log.debug("anilist rate limited, waiting %gs", wait)
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


def statuses(ids: list[int]) -> dict[int, tuple[str | None, int | None]]:
    """{id: (status, chapter count)} for these AniList ids, IDS_PER_PAGE per
    query: only what a pass needs to see that a finished series goes on
    after all. An id AniList does not know is left out. Raises like by_id."""
    out: dict[int, tuple[str | None, int | None]] = {}
    for i in range(0, len(ids), IDS_PER_PAGE):
        batch = ids[i:i + IDS_PER_PAGE]
        d = _post(_STATUSES, {"ids": batch, "n": len(batch)})
        for m in ((d.get("data") or {}).get("Page") or {}).get("media") or []:
            if isinstance(m, dict) and m.get("id") in batch:
                status, chapters = m.get("status"), m.get("chapters")
                out[m["id"]] = (status if isinstance(status, str) else None,
                                chapters if isinstance(chapters, int) and not isinstance(chapters, bool) else None)
    return out


def by_id(anilist_id: int) -> Series | None:
    d = _post(_BY_ID, {"id": anilist_id})
    m = (d.get("data") or {}).get("Media")
    return _to_series(m) if m else None

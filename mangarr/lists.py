"""Import lists (Sonarr's "Import Lists"): sources of series that are added
automatically - an AniList user's reading list, AniList's trending/popular
charts, or a plain text file of titles at a URL.

A list is fetched on a schedule (sync_hours) or by hand. Every series it
yields that is neither tracked already nor on the exclusion list is queued
as an ordinary add job. A series the user deleted is kept from coming back by
an exclusion on its ref. Adds are capped per sync so a first sync of a long
list does not queue hundreds of jobs; the next sync continues where it left
off.

Each list kind is a fetch(params) -> (series, review_titles) function in
FETCHERS; review_titles are lines a text list could not identify with
confidence and that the user has to add by hand.
"""
import json
import logging
import sqlite3
import time
import urllib.request
from collections.abc import Callable

from . import anilist, config, db, metadata, model
from .model import Series

log = logging.getLogger(__name__)

MAX_ADDS = 25                   # per sync; the next sync picks up the rest
USER_STATUSES = ("CURRENT", "PLANNING", "COMPLETED", "PAUSED", "REPEATING")
TOP_SORTS = ("TRENDING_DESC", "POPULARITY_DESC", "SCORE_DESC", "FAVOURITES_DESC")
COUNTRIES = ("JP", "KR", "CN")
KINDS = {"anilist_user": "AniList user list", "anilist_top": "AniList top charts", "url_text": "Text list at a URL"}
_NOVEL = {"NOVEL", "LIGHT_NOVEL"}
_PAGE = 50                      # AniList's maximum perPage

Fetched = tuple[list[Series], list[str]]

# -- AniList: a user's lists ----------------------------------------------------

_USER_QUERY = ("query($u: String) { MediaListCollection(userName: $u, type: MANGA) {"
               " lists { name status entries { media { " + anilist._FIELDS + " } } } } }")

_TOP_QUERY = ("query($sort: [MediaSort], $page: Int, $per: Int, $country: CountryCode) {"
              " Page(page: $page, perPage: $per) { pageInfo { hasNextPage }"
              " media(sort: $sort, type: MANGA, countryOfOrigin: $country, format_in: [MANGA], isAdult: false)"
              " { " + anilist._FIELDS + " } } }")


def user_entries(data: dict, statuses: list[str] | tuple[str, ...]) -> list[Series]:
    """Series from a MediaListCollection response, in the wanted statuses,
    comics only, each once (a custom list repeats the status lists' media)."""
    coll = (data.get("data") or {}).get("MediaListCollection") or {}
    want = set(statuses)
    out: dict[str, Series] = {}
    for lst in coll.get("lists") or []:
        if lst.get("status") not in want:
            continue
        for e in lst.get("entries") or []:
            m = e.get("media")
            if not m or m.get("format") in _NOVEL:
                continue
            s = anilist._to_series(m)
            out.setdefault(s.ref, s)
    return list(out.values())


def fetch_anilist_user(params: dict) -> Fetched:
    """Everything in the user's manga list with one of the chosen statuses.
    One request returns all lists, so the 30 req/min limit is no concern."""
    username = params["username"]
    d = anilist._post(_USER_QUERY, {"u": username})
    if not d or d.get("data", {}).get("MediaListCollection") is None:
        errs = "; ".join(e.get("message", "?") for e in (d or {}).get("errors") or []) or "not found or private"
        raise ValueError(f"AniList user {username!r}: {errs}")
    return user_entries(d, params.get("statuses") or USER_STATUSES), []


# -- AniList: charts -----------------------------------------------------------

def fetch_anilist_top(params: dict) -> Fetched:
    """The first `limit` entries of an AniList chart (manga format only, no
    adult titles), optionally one country of origin and a minimum chapter
    count (series whose count is not known yet pass the minimum)."""
    limit = int(params.get("limit") or 50)
    min_ch = int(params.get("min_chapters") or 0)
    out: dict[str, Series] = {}
    page, max_pages = 1, (limit + _PAGE - 1) // _PAGE + 2       # a few extra when min_chapters filters
    while len(out) < limit and page <= max_pages:
        # the variable is left out, not sent as null: AniList treats an
        # explicit countryOfOrigin: null as a filter that matches nothing
        variables = {"sort": [params.get("sort") or "TRENDING_DESC"], "page": page, "per": _PAGE}
        if params.get("country"):
            variables["country"] = params["country"]
        d = anilist._post(_TOP_QUERY, variables)
        pg = (d.get("data") or {}).get("Page") or {}
        for m in pg.get("media") or []:
            s = anilist._to_series(m)
            if min_ch and s.chapters is not None and s.chapters < min_ch:
                continue
            out.setdefault(s.ref, s)
            if len(out) >= limit:
                break
        if not (pg.get("pageInfo") or {}).get("hasNextPage"):
            break
        page += 1
    return list(out.values()), []


# -- a text file of titles ----------------------------------------------------

def _get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def parse_titles(text: str) -> list[str]:
    """One title per line; blank lines and lines starting with # are skipped."""
    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if line and not line.startswith("#"):
            out.append(line)
    return out


def fetch_url_text(params: dict) -> Fetched:
    """Every line is looked up like a typed title on the Add page; only a
    confident pick is added, the rest are reported for review. A line that
    is already a reference (anilist:123, mangadex:uuid) is used as is."""
    titles = parse_titles(_get_text(params["url"]))
    series: dict[str, Series] = {}
    review: list[str] = []
    for t in titles:
        try:
            if model.valid_ref(t) and not t.startswith("manual:"):
                s = metadata.by_ref(t)
                if not s or s.title == "?":
                    raise ValueError("nothing found")
            else:
                s, _ = metadata.lookup(t)
        except Exception as e:
            log.warning("list line %r: lookup failed: %s: %s", t, type(e).__name__, e)
            s = None
        if s:
            log.debug("list line %r -> %s (%s)", t, s.title, s.ref)
            series.setdefault(s.ref, s)
        else:
            log.debug("list line %r: no confident match", t)
            review.append(t)
    return list(series.values()), review


FETCHERS: dict[str, Callable[[dict], Fetched]] = {
    "anilist_user": fetch_anilist_user,
    "anilist_top": fetch_anilist_top,
    "url_text": fetch_url_text,
}


def fetch(kind: str, params: dict) -> Fetched:
    try:
        fn = FETCHERS[kind]
    except KeyError:
        raise ValueError(f"unknown list kind {kind!r}") from None
    return fn(params)


# -- params -------------------------------------------------------------------

def validate_params(kind: str, raw: dict) -> dict:
    """The stored params for a kind from a form/JSON dict; raises ValueError
    with a user-facing message."""
    if kind not in KINDS:
        raise ValueError(f"unknown list kind {kind!r}; known: {', '.join(KINDS)}")
    if kind == "anilist_user":
        username = str(raw.get("username") or "").strip()
        if not username:
            raise ValueError("an AniList username is required")
        statuses = raw.get("statuses") or []
        if isinstance(statuses, str):
            statuses = statuses.replace(",", " ").split()
        statuses = [s.upper() for s in statuses if str(s).strip()]
        bad = [s for s in statuses if s not in USER_STATUSES]
        if bad:
            raise ValueError(f"unknown status {bad[0]!r}; known: {', '.join(USER_STATUSES)}")
        return {"username": username, "statuses": statuses or ["CURRENT", "PLANNING"]}
    if kind == "anilist_top":
        sort = str(raw.get("sort") or "TRENDING_DESC").upper()
        if sort not in TOP_SORTS:
            raise ValueError(f"unknown sort {sort!r}; known: {', '.join(TOP_SORTS)}")
        try:
            limit = int(raw.get("limit") or 50)
            min_ch = int(raw.get("min_chapters") or 0)
        except (TypeError, ValueError):
            raise ValueError("limit and minimum chapters must be whole numbers") from None
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        country = str(raw.get("country") or "").upper()
        if country and country not in COUNTRIES:
            raise ValueError(f"country must be one of {', '.join(COUNTRIES)} or blank")
        out = {"sort": sort, "limit": limit, "country": country, "min_chapters": max(0, min_ch)}
        return out
    url = str(raw.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("the URL must start with http:// or https://")
    return {"url": url}


def describe(kind: str, params: dict) -> str:
    """One line for the Lists table."""
    if kind == "anilist_user":
        return f"{params.get('username', '?')}: {', '.join(s.lower() for s in params.get('statuses') or [])}"
    if kind == "anilist_top":
        bits = [str(params.get("sort", "")).replace("_DESC", "").lower(), f"top {params.get('limit', '?')}"]
        if params.get("country"):
            bits.append(params["country"])
        if params.get("min_chapters"):
            bits.append(f">= {params['min_chapters']} ch")
        return ", ".join(bits)
    return str(params.get("url", "?"))


# -- storage ------------------------------------------------------------------

def _row(con, list_id: int):
    return con.execute("SELECT * FROM import_list WHERE id=?", (list_id,)).fetchone()


def all_lists(con: sqlite3.Connection):
    return con.execute("SELECT * FROM import_list ORDER BY name COLLATE NOCASE").fetchall()


def get_list(con: sqlite3.Connection, list_id: int):
    return _row(con, list_id)


def add_list(con: sqlite3.Connection, name: str, kind: str, params: dict, *, enabled: bool = True,
             download: bool = True, monitored: bool = True, sync_hours: float = 24) -> int:
    name = name.strip() or f"{KINDS.get(kind, kind)}: {describe(kind, params)}"
    if not sync_hours or sync_hours <= 0:
        raise ValueError("sync interval must be a positive number of hours")
    cur = con.execute(
        "INSERT INTO import_list (name, kind, params, enabled, download, monitored, sync_hours, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (name[:120], kind, json.dumps(params), int(enabled), int(download), int(monitored), float(sync_hours),
         db.now()))
    log.info("import list added: %s (%s: %s)", name, kind, describe(kind, params))
    return cur.lastrowid


def set_enabled(con: sqlite3.Connection, list_id: int, enabled: bool) -> None:
    con.execute("UPDATE import_list SET enabled=? WHERE id=?", (int(enabled), list_id))


def delete_list(con: sqlite3.Connection, list_id: int) -> None:
    con.execute("DELETE FROM import_list WHERE id=?", (list_id,))


def mark_synced(con: sqlite3.Connection, list_id: int, result: str) -> None:
    con.execute("UPDATE import_list SET last_sync=?, last_result=? WHERE id=?",
                (db.now(), result[:1000], list_id))


def params_of(row) -> dict:
    try:
        return json.loads(row["params"] or "{}")
    except json.JSONDecodeError:
        return {}


def exclusions(con: sqlite3.Connection):
    return con.execute("SELECT * FROM import_list_exclusion ORDER BY created_at DESC").fetchall()


def excluded_refs(con: sqlite3.Connection) -> set[str]:
    return {r["ref"] for r in con.execute("SELECT ref FROM import_list_exclusion")}


def add_exclusion(con: sqlite3.Connection, ref: str, title: str | None = None, reason: str | None = None) -> None:
    ref = ref.strip()
    if not model.valid_ref(ref):
        raise ValueError(f"not a series reference: {ref!r}")
    con.execute("INSERT INTO import_list_exclusion (ref, title, reason, created_at) VALUES (?,?,?,?)"
                " ON CONFLICT(ref) DO UPDATE SET title=COALESCE(excluded.title, title),"
                " reason=COALESCE(excluded.reason, reason)",
                (ref, (title or "").strip() or None, (reason or "").strip() or None, db.now()))
    log.info("import list exclusion: %s (%s)", ref, title or "")


def remove_exclusion(con: sqlite3.Connection, ref: str) -> None:
    con.execute("DELETE FROM import_list_exclusion WHERE ref=?", (ref,))


def row_dict(row) -> dict:
    d = dict(row)
    d["params"] = params_of(row)
    d["summary"] = describe(row["kind"], d["params"])
    d["next_sync"] = next_sync_at(row)
    return d


# -- sync ---------------------------------------------------------------------

def _ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def next_sync_at(row) -> float | None:
    """When the list is next due (epoch seconds); None means "now"."""
    last = _ts(row["last_sync"])
    if last is None:
        return None
    return last + float(row["sync_hours"] or 24) * 3600


def is_due(row, now: float | None = None) -> bool:
    if not row["enabled"]:
        return False
    at = next_sync_at(row)
    return at is None or at <= (now if now is not None else time.time())


def sync(con: sqlite3.Connection, row, submit_add: Callable[[Series, bool, bool], object]) -> str:
    """Fetch one list and hand every new series to submit_add(series,
    download, monitored) - the caller queues the actual add job. Records
    last_sync and a one-line last_result, which is also returned. A fetch
    failure is recorded, logged with the list's name and returned; it never
    raises."""
    name, kind, params = row["name"], row["kind"], params_of(row)
    try:
        series, review = fetch(kind, params)
    except Exception as e:
        msg = f"error: {type(e).__name__}: {e}"[:300]
        log.error("list %s: %s", name, msg)
        mark_synced(con, row["id"], msg)
        return msg
    excluded = excluded_refs(con)
    added = tracked = skipped = deferred = 0
    for s in series:
        if db.get_series_by_ref(con, s.ref):
            tracked += 1
            log.debug("list %s: %s (%s) already tracked", name, s.title, s.ref)
            continue
        if s.ref in excluded:
            skipped += 1
            log.debug("list %s: %s (%s) excluded", name, s.title, s.ref)
            continue
        if added >= MAX_ADDS:
            deferred += 1
            continue
        log.debug("list %s: adding %s (%s)", name, s.title, s.ref)
        submit_add(s, bool(row["download"]), bool(row["monitored"]))
        added += 1
    parts = [f"{len(series)} fetched", f"{added} added"]
    if review:
        parts.append(f"{len(review)} review")
    if tracked:
        parts.append(f"{tracked} already tracked")
    if skipped:
        parts.append(f"{skipped} excluded")
    if deferred:
        parts.append(f"{deferred} deferred (cap {MAX_ADDS} per sync; next sync continues)")
    msg = ", ".join(parts)
    if review:
        msg += "; needs review: " + " | ".join(review[:40]) + (" | ..." if len(review) > 40 else "")
    mark_synced(con, row["id"], msg)
    log.info("list %s: %s", name, msg)
    return msg

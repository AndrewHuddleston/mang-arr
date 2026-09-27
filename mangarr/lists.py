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
import functools
import http.client
import ipaddress
import json
import logging
import socket
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable

from . import anilist, config, db, metadata, model
from .matching import MAX_TITLE, oneline
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

# A text list is fetched from a URL anyone may control, on the single job
# thread, and every line costs AniList/MangaDex requests: all of it is bounded.
MAX_LIST_BYTES = 1_000_000      # a list of 500 titles is ~15 KB
MAX_LIST_LINES = 500            # lines looked up per sync
MAX_LINE = 300                  # characters; longer lines are not titles (same limit as a manual ref)
FETCH_TIMEOUT = 20              # seconds per network operation ...
FETCH_DEADLINE = 60             # ... and for the whole download
MAX_REVIEW_SHOWN = 10           # unidentified lines quoted in last_result ...
MAX_REVIEW_CHARS = 80           # ... each cut to this many characters


class ListFetchError(ValueError):
    """A text list URL could not be fetched within the limits."""


def _check_peer(sock) -> None:
    """Refuse link-local (169.254/16, fe80::/10: cloud metadata services),
    multicast and unspecified addresses, checked on the address actually
    connected to so DNS tricks cannot get around it. Private LAN and
    loopback addresses stay allowed: a list on a NAS is the normal case."""
    try:
        ip = ipaddress.ip_address(sock.getpeername()[0].split("%")[0])
    except (OSError, ValueError, IndexError):
        return
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        sock.close()
        raise ListFetchError(f"refusing to fetch a list from {ip} (link-local, multicast or unspecified address)")


class _Watchdog:
    """Enforces a wall-clock deadline (and Cancel) on a whole fetch: connect,
    TLS handshake, status line, headers and body. The per-operation socket
    timeout alone does not do that - a server sending one header byte every
    few seconds never trips it - so a helper thread watches the clock and
    should_cancel and, when either fires, shuts down every socket the fetch
    opened; the blocked read then fails at once and _get_text reports why."""
    POLL = 0.25                 # seconds between should_cancel checks

    def __init__(self, deadline: float, should_cancel: Callable[[], bool] | None = None):
        self.end = time.monotonic() + deadline
        self.should_cancel = should_cancel
        self.reason = ""        # set once fired: "deadline" or "cancelled"
        self._socks: list = []
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="list-fetch-watchdog", daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._done.set()
        self._thread.join()
        self._close_all()

    def _run(self) -> None:
        while not self._done.wait(min(self.POLL, max(0.0, self.end - time.monotonic()))):
            if time.monotonic() >= self.end:
                self.fire("deadline")
                return
            if self.should_cancel is not None and self.should_cancel():
                self.fire("cancelled")
                return

    def fire(self, reason: str) -> None:
        with self._lock:
            if self.reason:
                return
            self.reason = reason
            socks = list(self._socks)
        for sock in socks:
            self._kill(sock)

    def register(self, sock) -> None:
        """Called with each freshly connected TCP socket, before any TLS
        handshake. A dup of it is kept: TLS wrapping detaches the original
        socket object, but shutting down the dup still ends the connection
        (shutdown acts on the connection, not on one descriptor), and the dup
        cannot be closed and its number reused before __exit__. A socket
        connected after the watchdog fired (connect is bounded by
        FETCH_TIMEOUT) is shut at once."""
        try:
            dup = sock.dup()
        except OSError:
            return
        with self._lock:
            self._socks.append(dup)
            fired = bool(self.reason)
        if fired:
            self._kill(dup)

    @staticmethod
    def _kill(sock) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)     # a blocked recv on the connection returns at once
        except OSError:
            pass

    def _close_all(self) -> None:
        with self._lock:
            socks, self._socks = self._socks, []
        for sock in socks:
            try:
                sock.close()
            except OSError:
                pass


class _WatchedConnection:
    """Mixin: hands every socket the connection opens to its _Watchdog.
    http.client sets _create_connection per instance in __init__, so it is
    wrapped there rather than overridden."""
    def __init__(self, *a, watchdog: _Watchdog | None = None, **kw):
        super().__init__(*a, **kw)
        if watchdog is not None:
            create = self._create_connection

            def create_watched(*ca, **ckw):
                sock = create(*ca, **ckw)
                watchdog.register(sock)
                return sock
            self._create_connection = create_watched


class _GuardedHTTPConnection(_WatchedConnection, http.client.HTTPConnection):
    def connect(self):
        super().connect()
        _check_peer(self.sock)


class _GuardedHTTPSConnection(_WatchedConnection, http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        _check_peer(self.sock)


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, watchdog: _Watchdog | None = None):
        super().__init__()
        self._watchdog = watchdog

    def http_open(self, req):
        return self.do_open(functools.partial(_GuardedHTTPConnection, watchdog=self._watchdog), req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, watchdog: _Watchdog | None = None):
        super().__init__()
        self._watchdog = watchdog

    def https_open(self, req):
        return self.do_open(functools.partial(_GuardedHTTPSConnection, watchdog=self._watchdog), req,
                            context=self._context)


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to http(s) on the same host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if new.scheme not in ("http", "https") or (new.hostname or "").lower() != (old.hostname or "").lower():
            raise ListFetchError(f"list URL redirects to another host or scheme ({oneline(newurl, 120)}); "
                                 "not followed - use the final URL instead")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(watchdog: _Watchdog | None = None):
    return urllib.request.build_opener(_GuardedHTTPHandler(watchdog), _GuardedHTTPSHandler(watchdog),
                                       _SameHostRedirects)


def _get_text(url: str, max_bytes: int = MAX_LIST_BYTES, deadline: float = FETCH_DEADLINE,
              should_cancel: Callable[[], bool] | None = None) -> str:
    """The body at an http(s) URL as text, read in chunks: more than
    max_bytes is an error, and so is taking longer than `deadline` seconds
    for the whole fetch - connect, headers and body - or should_cancel()
    turning true (checked a few times a second throughout, see _Watchdog).
    So a huge or trickling response can neither exhaust memory nor hold the
    job thread, and Cancel works while the list downloads."""
    if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
        raise ListFetchError("the URL must start with http:// or https://")
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    buf = bytearray()
    with _Watchdog(deadline, should_cancel) as wd:
        try:
            with _opener(wd).open(req, timeout=FETCH_TIMEOUT) as r:
                while True:
                    chunk = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
                    if wd.reason:           # fired between reads, or the read ended because it fired
                        break
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) > max_bytes:
                        log.warning("list %s: response larger than %d bytes; refused", oneline(url, 120), max_bytes)
                        raise ListFetchError(f"list is larger than {max_bytes // 1000} KB")
        except ListFetchError:
            raise
        except Exception:
            if not wd.reason:
                raise
            # the watchdog shut the socket: whatever the read raised is a symptom
        if wd.reason == "cancelled":
            log.info("list %s: download cancelled", oneline(url, 120))
            raise ListFetchError("list download cancelled")
        if wd.reason:
            log.warning("list %s: download took longer than %ss (connect, headers and body); abandoned",
                        oneline(url, 120), deadline)
            raise ListFetchError(f"list download took longer than {deadline}s")
    return buf.decode("utf-8", "replace")


def parse_titles(text: str, max_lines: int | None = None) -> list[str]:
    """One title per line; blank lines and lines starting with # are skipped,
    and so are repeats and lines longer than MAX_LINE (logged). With
    max_lines, at most that many titles are returned (logged)."""
    out: list[str] = []
    seen: set[str] = set()
    too_long = 0
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        if len(line) > MAX_LINE:
            too_long += 1
            continue
        if line in seen:
            continue
        seen.add(line)
        if max_lines is not None and len(out) >= max_lines:
            log.warning("text list has more than %d titles; only the first %d are used", max_lines, max_lines)
            break
        out.append(line)
    if too_long:
        log.warning("text list: %d line(s) longer than %d characters skipped (not titles)", too_long, MAX_LINE)
    return out


def fetch_url_text(params: dict, should_cancel: Callable[[], bool] | None = None,
                   progress: Callable[[str], None] | None = None) -> Fetched:
    """Every line is looked up like a typed title on the Add page; only a
    confident pick is added, the rest are reported for review. A line that
    is already a reference (anilist:123, mangadex:uuid) is used as is. At
    most MAX_LIST_LINES lines are looked up (rate limited: a long list takes
    a while, and `progress` hears which line it is on); should_cancel is
    checked during the download and between lines."""
    titles = parse_titles(_get_text(params["url"], should_cancel=should_cancel), MAX_LIST_LINES)
    series: dict[str, Series] = {}
    review: list[str] = []
    for i, t in enumerate(titles):
        if should_cancel and should_cancel():
            log.info("text list sync cancelled after %d of %d line(s)", i, len(titles))
            break
        if progress:
            progress(f"looking up line {i + 1} of {len(titles)}")
        try:
            if model.valid_ref(t) and not t.startswith("manual:"):
                s = metadata.by_ref(t)
                if not s or s.title == "?":
                    raise ValueError("nothing found")
            else:
                s, _ = metadata.lookup(t)
        except Exception as e:
            log.warning("list line %r: lookup failed: %s: %s", t[:MAX_TITLE], type(e).__name__, oneline(e, 300))
            s = None
        if s:
            log.debug("list line %r -> %s (%s)", t, oneline(s.title), s.ref)
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


def fetch(kind: str, params: dict, should_cancel: Callable[[], bool] | None = None,
          progress: Callable[[str], None] | None = None) -> Fetched:
    try:
        fn = FETCHERS[kind]
    except KeyError:
        raise ValueError(f"unknown list kind {kind!r}") from None
    if fn is fetch_url_text:                                     # the only fetcher that loops for long
        return fn(params, should_cancel=should_cancel, progress=progress)
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
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("the URL must start with http:// or https:// and name a host")
    if len(url) > 2000:
        raise ValueError("the URL is longer than 2000 characters")
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


def sync(con: sqlite3.Connection, row, submit_add: Callable[[Series, bool, bool], object],
         should_cancel: Callable[[], bool] | None = None, progress: Callable[[str], None] | None = None) -> str:
    """Fetch one list and hand every new series to submit_add(series,
    download, monitored) - the caller queues the actual add job; `progress`
    hears how far a long fetch got. Records last_sync and a one-line
    last_result, which is also returned. A fetch failure is recorded, logged
    with the list's name and returned; it never raises. last_sync is stamped
    before the fetch too, so a sync that crashes the process is not retried
    right after the restart."""
    name, kind, params = row["name"], row["kind"], params_of(row)
    mark_synced(con, row["id"], "sync in progress (or interrupted)")
    con.commit()
    try:
        series, review = fetch(kind, params, should_cancel, progress)
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
        # quoted lines are cut short: they are whatever the URL served
        shown = [oneline(t, MAX_REVIEW_CHARS) for t in review[:MAX_REVIEW_SHOWN]]
        msg += "; needs review: " + " | ".join(shown) + (" | ..." if len(review) > MAX_REVIEW_SHOWN else "")
    mark_synced(con, row["id"], msg)
    log.info("list %s: %s", name, msg)
    return msg

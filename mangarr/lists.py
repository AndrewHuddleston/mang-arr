"""Import lists (Sonarr's "Import Lists"): sources of series that are added
automatically - an AniList user's reading list, AniList's trending/popular
charts, or a plain text file of titles at a URL.

A list is fetched on a schedule (sync_hours) or by hand. Every series it
yields that is neither tracked already nor on the exclusion list is queued
as an ordinary add job. A series the user deleted is kept from coming back by
an exclusion on its ref. Adds are capped per sync so a first sync of a long
list does not queue hundreds of jobs; the next sync continues where it left
off.

Each list kind is a fetch(params) -> Fetched(series, review, skipped, quoted)
function in FETCHERS; review are lines of a text list that look like titles
but could not be identified with confidence (the user adds them by hand) -
the lines themselves, or only "line N" when the list did not prove to be a
title list (see fetch_url_text) - and skipped counts lines that do not look
like titles at all.
"""
import functools
import http.client
import ipaddress
import json
import logging
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import NamedTuple

from . import anilist, config, db, metadata, model
from .matching import oneline, query_score
from .model import Series

log = logging.getLogger(__name__)

MAX_ADDS = 25                   # per sync; the next sync picks up the rest
USER_STATUSES = ("CURRENT", "PLANNING", "COMPLETED", "PAUSED", "REPEATING")
TOP_SORTS = ("TRENDING_DESC", "POPULARITY_DESC", "SCORE_DESC", "FAVOURITES_DESC")
COUNTRIES = ("JP", "KR", "CN")
KINDS = {"anilist_user": "AniList user list", "anilist_top": "AniList top charts", "url_text": "Text list at a URL"}
_NOVEL = {"NOVEL", "LIGHT_NOVEL"}
_PAGE = 50                      # AniList's maximum perPage


class Fetched(NamedTuple):
    series: list[Series]
    review: list[str]           # look like titles, no confident match: listed for the user
    skipped: int = 0            # text list lines that are not titles: neither looked up nor shown
    quoted: bool = True         # review holds the lines; False: only "line N" (see fetch_url_text)


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
    return Fetched(user_entries(d, params.get("statuses") or USER_STATUSES), [])


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
    return Fetched(list(out.values()), [])


# -- a text file of titles ----------------------------------------------------

# A text list is fetched from a URL anyone may control, on the single job
# thread, and every line costs AniList/MangaDex requests: all of it is bounded.
MAX_LIST_BYTES = 1_000_000      # a list of 500 titles is ~15 KB
MAX_LIST_LINES = 500            # lines looked up per sync
MAX_LINE = 300                  # characters; longer lines are not titles (same limit as a manual ref)
FETCH_TIMEOUT = 20              # seconds per network operation ...
FETCH_DEADLINE = 60             # ... and for the whole download
PROBE_LINES = 5                 # lines looked up before one must be a series AniList or MangaDex knows
MAX_REVIEW_SHOWN = 10           # unidentified lines quoted in last_result ...
MAX_REVIEW_CHARS = 80           # ... each cut to this many characters


class ListFetchError(ValueError):
    """A text list URL could not be fetched within the limits, or what it
    returned is not a title list. The message is shown on /lists and by the
    API, so it never quotes what the server sent."""


NOT_A_LIST = "the URL did not return a title list"
_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")
_LOCAL_NAMES = ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback")


def _refused(ip) -> str | None:
    """Why no list may be fetched from this address, or None. Loopback
    (mang-arr itself, its container, Docker's DNS), link-local (169.254/16,
    fe80::/10: cloud metadata services), multicast and unspecified ones are
    refused. Private LAN addresses stay allowed: a list on a NAS is the
    normal case."""
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_link_local:
        return "a link-local address"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_unspecified or (ip.version == 4 and ip in _THIS_NETWORK):
        return "an unspecified address"
    return None


def _refused_host(host: str) -> str | None:
    """Why a list URL's host is refused before anything is fetched: a name
    of this machine, or an address _refused() rejects in any form the
    resolver accepts (127.1, 2130706433, 0x7f.0.0.1, [::ffff:127.0.0.1]).
    Other names are not resolved here - that would hold the form on a slow
    DNS server and prove nothing about the fetch later, as DNS answers
    change - but on every fetch, before anything is connected to (_connect)."""
    h = host.strip().lower().rstrip(".")
    if h in _LOCAL_NAMES or h.endswith(".localhost"):
        return "a name of this machine"
    try:
        ip = ipaddress.ip_address(h.split("%")[0])
    except ValueError:
        try:
            ip = ipaddress.IPv4Address(socket.inet_aton(h))     # the legacy numeric forms
        except (OSError, ValueError):
            return None
    return _refused(ip)


def _connect(address, timeout=None, source_address=None) -> socket.socket:
    """socket.create_connection() for a list fetch: the host is resolved
    here, addresses _refused() rejects are dropped, and only the addresses
    checked are connected to - so no second DNS answer or redirect gets
    around the check. A refused address is never connected to, not even to
    see whether the port is open: the error is the same for an open port
    and a closed one, and cannot be used to scan mang-arr's own machine."""
    host, port = address[0], address[1]
    usable, refused = [], None
    for family, kind, proto, _, sockaddr in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        try:
            ip = ipaddress.ip_address(str(sockaddr[0]).split("%")[0])
        except ValueError:
            continue
        why = _refused(ip)
        if why:
            refused = refused or (ip, why)
            continue
        usable.append((family, kind, proto, sockaddr))
    if not usable:
        if refused is None:
            raise OSError(f"no usable address for {oneline(host, 80)}")
        ip, why = refused
        log.warning("list fetch refused: %s resolves to %s (%s); not connected", oneline(host, 80), ip, why)
        raise ListFetchError(f"refusing to fetch a list from {ip} ({why})")
    err: OSError | None = None
    for family, kind, proto, sockaddr in usable:         # in the resolver's order, as create_connection does
        sock = socket.socket(family, kind, proto)
        try:
            if isinstance(timeout, (int, float)):
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as e:
            err = e
            sock.close()
    raise err if err is not None else OSError("connect failed")


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


class _GuardedConnection:
    """Mixin: every socket the connection opens comes from _connect (its
    address checked before the connect) and is handed to its _Watchdog
    right after the TCP connect, before any TLS handshake. http.client sets
    _create_connection per instance in __init__, so it is replaced there
    rather than overridden."""
    def __init__(self, *a, watchdog: _Watchdog | None = None, **kw):
        super().__init__(*a, **kw)

        def create_guarded(address, timeout=None, source_address=None, **_):
            sock = _connect(address, timeout, source_address)
            if watchdog is not None:
                watchdog.register(sock)
            return sock
        self._create_connection = create_guarded


class _GuardedHTTPConnection(_GuardedConnection, http.client.HTTPConnection):
    pass


class _GuardedHTTPSConnection(_GuardedConnection, http.client.HTTPSConnection):
    pass


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
            # only where it points, not the whole Location: its path and query are the server's text
            raise ListFetchError(f"list URL redirects to another host or scheme ({oneline(new.scheme, 10)}://"
                                 f"{oneline(new.hostname, 60)}); not followed - use the final URL instead")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(watchdog: _Watchdog | None = None):
    return urllib.request.build_opener(_GuardedHTTPHandler(watchdog), _GuardedHTTPSHandler(watchdog),
                                       _SameHostRedirects)


# A text file is served with whatever type the server guesses from its name
# (text/plain, text/markdown, text/tab-separated-values, none at all, or
# octet-stream / x-download / force-download from a NAS or a file host), so
# only types that are plainly something else are refused by their header;
# the body is judged next (list_titles).
_NOT_TEXT_MAJOR = ("image/", "audio/", "video/", "font/", "model/", "multipart/")
_NOT_TEXT_TYPES = {"application/pdf", "application/zip", "application/gzip", "application/x-gzip",
                   "application/x-tar", "application/x-7z-compressed", "application/vnd.rar",
                   "application/x-rar-compressed", "application/x-bzip2", "application/x-xz", "application/zstd",
                   "application/wasm", "application/javascript", "text/javascript", "text/css",
                   "application/x-www-form-urlencoded"}


def _not_text(content_type: str) -> str | None:
    """What a response of this Content-Type is, in plain words, when it is
    plainly not a text file; None when it may be one."""
    ctype = content_type.split(";")[0].strip().lower()
    for key, what in (("json", "JSON"), ("html", "a web page"), ("xml", "XML")):
        if key in ctype:
            return what
    if ctype.startswith(_NOT_TEXT_MAJOR) or ctype in _NOT_TEXT_TYPES:
        return "not plain text"
    return None


def _fetch_error(e: Exception) -> str:
    """A failed fetch in plain words, without anything the server sent (an
    HTTP reason phrase, the banner of a service that does not speak HTTP)."""
    if isinstance(e, urllib.error.HTTPError):
        return f"the server answered HTTP {e.code}"
    reason = e.reason if isinstance(e, urllib.error.URLError) else e
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "the server did not answer in time"
    if isinstance(reason, socket.gaierror):
        return "the host name could not be resolved"
    if isinstance(reason, ConnectionRefusedError):
        return "the connection was refused"
    if isinstance(reason, http.client.HTTPException):
        return "the server did not answer with valid HTTP"
    if isinstance(reason, OSError):
        return f"could not connect ({type(reason).__name__})"
    return f"download failed ({type(reason).__name__})"


def _get_text(url: str, max_bytes: int = MAX_LIST_BYTES, deadline: float = FETCH_DEADLINE,
              should_cancel: Callable[[], bool] | None = None) -> str:
    """The body at an http(s) URL as text, read in chunks: more than
    max_bytes is an error, and so is taking longer than `deadline` seconds
    for the whole fetch - connect, headers and body - or should_cancel()
    turning true (checked a few times a second throughout, see _Watchdog).
    So a huge or trickling response can neither exhaust memory nor hold the
    job thread, and Cancel works while the list downloads. A response that
    says it is JSON, a web page or another non-text type is refused before
    its body is read. Every failure is a ListFetchError in plain words."""
    if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
        raise ListFetchError("the URL must start with http:// or https://")
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    buf = bytearray()
    with _Watchdog(deadline, should_cancel) as wd:
        try:
            with _opener(wd).open(req, timeout=FETCH_TIMEOUT) as r:
                what = _not_text(r.headers.get("Content-Type") or "")
                if what:
                    log.warning("list %s: the response is %s (%s), not a text list; refused", oneline(url, 120),
                                what, oneline(r.headers.get("Content-Type"), 60))
                    raise ListFetchError(f"{NOT_A_LIST} (it sent {what})")
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
        except Exception as e:
            if not wd.reason:
                log.debug("list %s: %s: %s", oneline(url, 120), type(e).__name__, oneline(e, 200))
                raise ListFetchError(_fetch_error(e)) from e
            # the watchdog shut the socket: whatever the read raised is a symptom
        if wd.reason == "cancelled":
            log.info("list %s: download cancelled", oneline(url, 120))
            raise ListFetchError("list download cancelled")
        if wd.reason:
            log.warning("list %s: download took longer than %ss (connect, headers and body); abandoned",
                        oneline(url, 120), deadline)
            raise ListFetchError(f"list download took longer than {deadline}s")
    return buf.decode("utf-8", "replace")


# What JSON, markup, config files, logs and API answers are made of and a
# title never is. Titles do use ':', '"', '!', '?', ';', '&', '+', '=', '%',
# '[]' and '//' (Re:Zero, "Oshi no Ko", Steins;Gate, Love=Game, [Oshi no
# Ko], .hack//G.U.), and even '<...>' (<Infinite Dendrogram>), so none of
# those alone counts. Every check is linear in the line - a pattern that
# could start anywhere in a run of word characters only starts where the
# run does - and runs only when the line has what a match must contain.
_TAG = re.compile(r"</[A-Za-z]|<[!?]|<[A-Za-z][\w-]*(?:\s[^<>]*=|\s*/?>)")   # </p>, <!doctype, <?xml, <br>, <a href=
_JUNK_START = re.compile(                                       # matched at the start of the line
    r"(?:export\s+)?(?:[A-Z][A-Z0-9_]*|[a-z_][a-z0-9_.-]*)\s*="  # KEY=value, key=value (env, ini, query strings)
    r"|[a-z][a-z0-9]*_[\w-]*\s*:"                               # snake_case_key: value
    r"|[\w.-]+:$")                                              # a bare 'section:' line
_JUNK = (                                                       # (what a match contains, pattern) searched anywhere
    ("", re.compile(r"[{}\\\x00-\x08\x0b-\x1f\x7f]")),          # braces, backslashes, control characters
    ("<", _TAG),                                                # tags
    ('"', re.compile(r'"[^"]*"\s*:')),                          # a JSON member: "key": ...
    ("://", re.compile(r"(?<![\w+.-])[A-Za-z][\w+.-]*://")),    # a URL or connection string
    ("@", re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+\.\w")),      # an e-mail address
    (".", re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),   # an IPv4 address
)
_TOKEN = re.compile(r"[A-Za-z0-9+/_=.-]{33,}")                  # no word of a title is this long ...
_DIGIT = re.compile(r"\d")
_NUMBER_TITLE = re.compile(r"\d{1,4}")                          # 86, 2001: a title with no letters


def _junk(line: str) -> bool:
    """Whether the line has something of JSON, markup, a config file, a log
    or an API answer in it (see above)."""
    if ("=" in line or ":" in line) and _JUNK_START.match(line):
        return True
    if any(needle in line and rx.search(line) for needle, rx in _JUNK):
        return True
    # three ':' in one word: /etc/passwd, MAC and IPv6 addresses (Re:Zero has one)
    return line.count(":") >= 3 and any(w.count(":") >= 3 for w in line.split())


def looks_like_title(line: str) -> bool:
    """Whether one stripped line of a text list could be a title (or an
    anilist:/mangadex: reference). Other lines are neither looked up nor
    shown. This only keeps what is plainly not a title (an API answer, a
    config file) away from the providers: no look at a line tells a title
    from 'host: db' - fetch_url_text leaves that to AniList and MangaDex."""
    if model.valid_ref(line) and not line.startswith("manual:"):
        return True
    if not any(ch.isalpha() for ch in line):
        return bool(_NUMBER_TITLE.fullmatch(line))
    if _junk(line):
        return False
    # ... that also has digits: a key, a hash, base64 or a token
    return not any(_DIGIT.search(t) for t in _TOKEN.findall(line))


def _body_kind(text: str) -> str | None:
    """What a response body is when it is plainly not a list of lines -
    'JSON', 'a web page or XML', 'binary data' - or None. Judged on its
    start and a sample, never quoted."""
    head = text.lstrip("\ufeff \t\r\n")
    sample = text[:8192]
    if "\x00" in sample or sample.count("\ufffd") > len(sample) // 10:
        return "binary data"
    if head.startswith("{"):
        return "JSON"
    if head.startswith("["):                            # or a title such as "[Oshi no Ko]"
        try:
            if isinstance(json.loads(text), (list, dict)):
                return "JSON"
        except ValueError:
            pass
    if _TAG.match(head):                                # not <Infinite Dendrogram>, a title
        return "a web page or XML"
    return None


class Line(NamedTuple):
    no: int                     # line number in the file, from 1
    text: str


def _split(text: str, max_lines: int | None = None) -> tuple[list[Line], int]:
    """(titles, lines skipped as not titles): one title per line, a tab
    counting as a space; blank lines and lines starting with # are skipped,
    and so are repeats, lines longer than MAX_LINE and lines that do not
    look like titles (counts logged, the lines never). A repeated line is
    judged once. With max_lines, at most that many titles."""
    out: list[Line] = []
    seen: set[str] = set()
    too_long = junk = 0
    for no, line in enumerate(text.splitlines(), 1):
        line = line.replace("\t", " ").strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if len(line) > MAX_LINE:
            too_long += 1
            continue
        if line in seen:
            continue
        seen.add(line)
        if not looks_like_title(line):
            junk += 1
            continue
        if max_lines is not None and len(out) >= max_lines:
            log.warning("text list has more than %d titles; only the first %d are used", max_lines, max_lines)
            break
        out.append(Line(no, line))
    if too_long:
        log.warning("text list: %d line(s) longer than %d characters skipped (not titles)", too_long, MAX_LINE)
    if junk:
        log.warning("text list: %d line(s) that do not look like titles skipped (not looked up, not shown)", junk)
    return out, too_long + junk


def parse_titles(text: str, max_lines: int | None = None) -> list[str]:
    """The titles of a text list (see _split)."""
    return [ln.text for ln in _split(text, max_lines)[0]]


def list_titles(text: str, max_lines: int | None = None) -> tuple[list[Line], int]:
    """(title lines, lines skipped) of a response that should be a text list.
    Raises ListFetchError with NOT_A_LIST - and nothing of the body - when it
    is JSON, a web page, XML or binary data, or when more of its lines are
    not titles than are: then it is some other document (a status page, a
    config file), and even its title-like lines are not used."""
    kind = _body_kind(text)
    if kind:
        log.warning("text list: the response is %s, not a list of titles; refused", kind)
        raise ListFetchError(f"{NOT_A_LIST} (it looks like {kind})")
    titles, skipped = _split(text, max_lines)
    if skipped > len(titles):
        log.warning("text list: %d line(s) are not titles and only %d are; not a title list, refused", skipped,
                    len(titles))
        raise ListFetchError(f"{NOT_A_LIST} (most of its lines are not titles)")
    return titles, skipped


def _unknown(looked: int, failed: int) -> ListFetchError:
    """The error for a list none of whose lines looked up is a series
    AniList or MangaDex knows. It quotes none of them."""
    if failed >= looked:
        log.warning("text list: none of %d line(s) could be looked up (AniList and MangaDex unreachable?)", looked)
        return ListFetchError("the titles could not be looked up at AniList or MangaDex; try again later")
    log.warning("text list: none of the %d line(s) looked up is a series AniList or MangaDex knows; not taken "
                "for a title list: nothing more looked up, nothing quoted", looked)
    return ListFetchError(f"{NOT_A_LIST}: none of the {looked} line(s) looked up is a series AniList or MangaDex "
                          "knows" + (f" ({failed} could not be looked up)" if failed else ""))


def fetch_url_text(params: dict, should_cancel: Callable[[], bool] | None = None,
                   progress: Callable[[str], None] | None = None) -> Fetched:
    """Every line that looks like a title is looked up like a typed title on
    the Add page; only a confident pick is added, the rest are reported for
    review. A line that is already a reference (anilist:123, mangadex:uuid)
    is used as is. At most MAX_LIST_LINES lines are looked up (rate
    limited: a long list takes a while, and `progress` hears which line it
    is on); should_cancel is checked during the download and between lines.

    The URL may be any server on the LAN, and no look at a line tells a
    title from a line of some other text answer ('host: db' / 'Re:Zero'),
    so AniList and MangaDex decide. A line is known when it is a confident
    pick or the exact title of some series there. Until one line is known,
    at most PROBE_LINES are looked up; when none is, the sync fails and
    quotes nothing. Lines without a confident pick are quoted for review
    only when at least half of the lines looked up are known - the body has
    then proved to be a title list - and are otherwise given only by line
    number. Lines are logged by number, never quoted."""
    lines, skipped = list_titles(_get_text(params["url"], should_cancel=should_cancel), MAX_LIST_LINES)
    series: dict[str, Series] = {}
    unmatched: list[Line] = []
    looked = known = failed = 0
    cancelled = False
    for ln in lines:
        if should_cancel and should_cancel():
            log.info("text list sync cancelled after %d of %d line(s)", looked, len(lines))
            cancelled = True
            break
        if not known and looked >= PROBE_LINES:
            raise _unknown(looked, failed)
        if progress:
            progress(f"looking up line {looked + 1} of {len(lines)}")
        looked += 1
        cands: list[Series] = []
        try:
            if model.valid_ref(ln.text) and not ln.text.startswith("manual:"):
                s = metadata.by_ref(ln.text)
                s = s if s and s.title != "?" else None
            else:
                s, cands = metadata.lookup(ln.text)
        except Exception as e:
            failed += 1
            log.warning("list line %d: lookup failed: %s: %s", ln.no, type(e).__name__, oneline(e, 300))
            s = None
        if s:
            known += 1
            log.debug("list line %d -> %s (%s)", ln.no, oneline(s.title), s.ref)
            series.setdefault(s.ref, s)
            continue
        if any(query_score(t, ln.text) == 0 for c in cands for t in c.titles):
            known += 1                  # several series of exactly that title: a title all the same
        log.debug("list line %d: no confident match (%d candidate(s))", ln.no, len(cands))
        unmatched.append(ln)
    if looked and not known and not cancelled:
        raise _unknown(looked, failed)
    quoted = known > 0 and known * 2 >= looked
    if unmatched and not quoted:
        log.warning("text list: only %d of %d line(s) looked up are series AniList or MangaDex knows; the %d "
                    "without a match are reported by line number, not quoted", known, looked, len(unmatched))
    review = [ln.text if quoted else f"line {ln.no}" for ln in unmatched]
    return Fetched(list(series.values()), review, skipped, quoted)


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
    why = _refused_host(parts.hostname)
    if why:
        log.info("import list URL refused: %s is %s", oneline(parts.hostname, 80), why)
        raise ValueError(f"the URL's host is {why}: lists are fetched from the internet or the LAN, not from "
                         "mang-arr's own machine, link-local (cloud metadata) or multicast addresses")
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
        series, review, *more = fetch(kind, params, should_cancel, progress)
    except Exception as e:
        # a ListFetchError is already plain words (and quotes nothing the server sent)
        msg = (f"error: {e}" if isinstance(e, ListFetchError) else f"error: {type(e).__name__}: {e}")[:300]
        log.error("list %s: %s", name, msg)
        mark_synced(con, row["id"], msg)
        return msg
    not_titles = more[0] if more else 0
    quoted = more[1] if len(more) > 1 else True
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
    if not_titles:
        parts.append(f"{not_titles} line(s) skipped (not titles)")
    if tracked:
        parts.append(f"{tracked} already tracked")
    if skipped:
        parts.append(f"{skipped} excluded")
    if deferred:
        parts.append(f"{deferred} deferred (cap {MAX_ADDS} per sync; next sync continues)")
    msg = ", ".join(parts)
    if review:
        # lines are only quoted from a list that proved to be one
        # (fetch_url_text), and cut short all the same: they are whatever
        # the URL served
        shown = [oneline(t, MAX_REVIEW_CHARS) for t in review[:MAX_REVIEW_SHOWN]]
        msg += "; needs review: " + " | ".join(shown) + (" | ..." if len(review) > MAX_REVIEW_SHOWN else "")
        if not quoted:
            msg += " (not quoted: fewer than half of the lines looked up are series AniList or MangaDex knows)"
    mark_synced(con, row["id"], msg)
    log.info("list %s: %s", name, msg)
    return msg

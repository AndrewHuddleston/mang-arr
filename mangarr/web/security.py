"""Web security helpers used by app.py's middleware and login routes: the
request body limit, the Host allowlist (DNS rebinding), the Origin/Referer
check (CSRF), signed and revocable session cookies, password checks with a
per-address failure throttle, and the security response headers. No routes
here; app.py wires them up (see the comment above its middleware).
"""
import base64
import binascii
import functools
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import threading
import time
import urllib.parse

from fastapi import HTTPException, Request
from fastapi.responses import Response

from .. import db, settings

log = logging.getLogger(__name__)

SESSION_COOKIE = "mangarr_session"
SESSION_DAYS = 30
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
MAX_BODY = 1 << 20                           # request bodies above this get 413 (1 MB)
# bigger bodies for specific routes: the backup upload (MANGARR_MAX_UPLOAD_MB, default 2 GB)
BODY_LIMITS: dict[str, int] = {"/system/backups/upload": int(os.environ.get("MANGARR_MAX_UPLOAD_MB", "2048")) << 20}
# names that cannot be an attacker's public DNS name: LAN-only suffixes (RFC 6762 appendix G, RFC 8375),
# router and container names (FRITZ!Box, Docker) and Tailscale MagicDNS (*.ts.net, controlled by Tailscale)
LAN_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localdomain", ".home", ".corp", ".intranet",
                ".private", ".fritz.box", ".docker", ".ts.net")
DEFAULT_PORTS = {"http": 80, "https": 443}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src * data:; "
       "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'")
MAX_REVOKED = 200                            # sessions signed out one by one; more -> sign everyone out


def clip(value, n: int = 64) -> str:
    """Attacker-supplied text for a log line: repr (no line breaks), at most n characters."""
    text = str(value)
    return repr(text[:n] + ("..." if len(text) > n else ""))


@functools.lru_cache(maxsize=4)
def _trusted_proxies(spec: str) -> tuple:
    """MANGARR_TRUSTED_PROXIES: comma-separated addresses or networks of reverse proxies."""
    nets = []
    for part in spec.split(","):
        if part.strip():
            try:
                nets.append(ipaddress.ip_network(part.strip(), strict=False))
            except ValueError:
                log.warning("MANGARR_TRUSTED_PROXIES: %s is not an address or network; ignored", clip(part))
    return tuple(nets)


def _is_trusted(addr: str, nets: tuple) -> bool:
    try:
        ip = ipaddress.ip_address(addr.strip())
    except ValueError:
        return False
    return any(ip.version == n.version and ip in n for n in nets)


def client_ip(request: Request) -> str:
    """The address failed-password throttling and log lines are keyed on:
    the peer, or, when the peer is a reverse proxy listed in
    MANGARR_TRUSTED_PROXIES, the last X-Forwarded-For hop that is not a
    trusted proxy (hops further left are client-supplied and not trusted).
    Without it every client behind a proxy shares the proxy's address, so
    other people's failures would lock the owner out."""
    peer = request.client.host if request.client else "?"
    nets = _trusted_proxies(os.environ.get("MANGARR_TRUSTED_PROXIES", ""))
    if not nets or not _is_trusted(peer, nets):
        return peer
    hops = [h.strip() for h in ",".join(request.headers.getlist("x-forwarded-for")).split(",") if h.strip()]
    for hop in reversed(hops):
        if not _is_trusted(hop, nets):
            try:
                return str(ipaddress.ip_address(hop))
            except ValueError:
                return peer                      # garbled: throttle on the proxy's address
    return peer


class LogBudget:
    """At most PER_MINUTE warning lines per client address and minute, so an
    unauthenticated flood cannot fill the disk or rotate the real history out
    of the log. Says once per minute that it is holding lines back."""
    PER_MINUTE = 20

    def __init__(self):
        self._lock = threading.Lock()
        self._d: dict[str, list[int]] = {}      # address -> [minute, lines]

    def allow(self, key: str) -> bool:
        minute = int(time.monotonic() // 60)
        with self._lock:
            if len(self._d) > 4096:
                self._d.clear()
            e = self._d.setdefault(key, [minute, 0])
            if e[0] != minute:
                e[:] = [minute, 0]
            e[1] += 1
            if e[1] == self.PER_MINUTE + 1:
                log.warning("more refused requests from %s this minute; not logging them", key)
            return e[1] <= self.PER_MINUTE


log_budget = LogBudget()


class Throttle:
    """Password attempts per client address, for the login form and Basic
    auth alike. The first FREE failures within WINDOW seconds are only
    slowed down; after that the address is refused (429, no password check)
    for a delay that doubles with each further failure (2 s, 4 s ... up to
    MAX_DELAY). A success clears it. Memory is bounded.

    An attempt is reserved (reserve()) before the slow password check and
    counts as a failure from that moment, so attempts still in flight count
    against the limit: a burst of parallel guesses gets no more checks than
    the same guesses one after another. A failure then needs nothing more,
    and a request dropped halfway (client gone) cannot leave a reservation
    behind."""
    FREE, WINDOW, MAX_DELAY, MAX_KEYS = 5, 900.0, 900.0, 4096

    def __init__(self):
        self._lock = threading.Lock()
        self._d: dict[str, list[float]] = {}     # address -> [attempts, first attempt at, blocked until]

    def _entry(self, key: str, now: float) -> list[float] | None:
        e = self._d.get(key)
        if e and now - e[1] > self.WINDOW and now >= e[2]:
            del self._d[key]                     # quiet for a whole window: start over
            return None
        return e

    def retry_after(self, key: str) -> int:
        """Seconds this address must wait before its next password attempt (0 = go ahead)."""
        with self._lock:
            now = time.monotonic()
            e = self._entry(key, now)
            return max(0, int(e[2] - now + 0.999)) if e else 0

    def reserve(self, key: str) -> int:
        """Claim one password attempt for this address, atomically, before
        checking the password: 0 = go ahead (it counts as a failure until
        succeeded() clears the address), else the seconds the address must
        still wait (nothing claimed, no check allowed)."""
        with self._lock:
            now = time.monotonic()
            e = self._entry(key, now)
            if e and now < e[2]:
                return max(1, int(e[2] - now + 0.999))
            if e is None:
                e = self._d[key] = [0, now, 0.0]
            e[0] += 1
            if e[0] > self.FREE:
                e[2] = now + min(self.MAX_DELAY, 2.0 ** (e[0] - self.FREE))
            if len(self._d) > self.MAX_KEYS:     # forget the oldest
                for k in sorted(self._d, key=lambda k: self._d[k][1])[:len(self._d) - self.MAX_KEYS]:
                    del self._d[k]
            return 0

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._d.pop(key, None)


throttle = Throttle()


class BodyLimit:
    """Refuse request bodies over MAX_BODY (BODY_LIMITS per path) with 413: at
    once when Content-Length says so, else as soon as the streamed bytes pass
    the limit. Without it an anonymous multipart POST to /login spools any
    amount to disk, and JSON bodies are held in memory whole."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        limit = BODY_LIMITS.get(path, MAX_BODY)
        length = None
        for k, val in scope.get("headers") or []:
            if k == b"content-length":
                try:
                    length = int(val)
                except ValueError:
                    length = None
        too_large = Response("mang-arr: request body too large\n", 413, media_type="text/plain")
        if length is not None and length > limit:
            log.warning("refused a %d-byte request body to %s (limit %d bytes)", length, clip(path, 200), limit)
            return await too_large(scope, receive, send)
        seen, over, started = 0, False, False

        async def limited_receive():
            nonlocal seen, over
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    if not over:
                        log.warning("refused a streamed request body to %s over %d bytes", clip(path, 200), limit)
                    over = True
                    raise HTTPException(413, "request body too large")
            return message

        async def tracking_send(message):
            nonlocal started
            if over:                             # whatever the app answers to a cut-off body (often a 400
                if not started and message["type"] == "http.response.start":    # "error parsing the body")
                    started = True
                    await too_large(scope, receive, send)
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BaseException:
            # the 413 raised above can come back wrapped (BaseHTTPMiddleware task groups): answer it here
            if not over:
                raise
            if not started:
                await too_large(scope, receive, send)


def hostname(host_header: str) -> str:
    """'Mangarr.LAN:6789' -> 'mangarr.lan', '[::1]:6789' -> '::1'."""
    return settings.bare_host(host_header)


def allowed_hosts(v: dict) -> set[str]:
    """MANGARR_ALLOWED_HOSTS (comma-separated) plus the allowed_hosts setting,
    as bare names (settings.host_name): an entry typed with a port, or one
    stored so by an older version, still matches."""
    env = os.environ.get("MANGARR_ALLOWED_HOSTS", "").split(",")
    return {settings.host_name(h) for h in [*env, *(v.get("allowed_hosts") or [])]} - {""}


def host_allowed(host_header: str, extra: set[str]) -> bool:
    """DNS rebinding defence: a rebound page reaches us under the attacker's
    host name, so only names that cannot be an attacker's public DNS name are
    answered: IP literals, localhost, single-label names (mangarr), LAN
    suffixes (LAN_SUFFIXES: .local .lan .home .fritz.box .ts.net ...) and the names in
    `extra` ('*' allows everything, '.example.com' a whole domain)."""
    host = hostname(host_header)
    if not host:
        return not host_header                   # no Host at all (HTTP/1.0 tools); a garbled one is refused
    if "*" in extra or host in extra:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host in ("localhost", "fritz.box") or host.endswith(".localhost") or "." not in host \
            or host.endswith(LAN_SUFFIXES):
        return True
    return any(e.startswith(".") and host.endswith(e) for e in extra)


def _host_port(netloc: str, scheme: str) -> tuple[str, int | None]:
    try:
        u = urllib.parse.urlsplit(f"//{netloc.strip()}")
        return (u.hostname or "").rstrip("."), u.port or DEFAULT_PORTS.get(scheme)
    except ValueError:
        return "", None


def same_origin(request: Request) -> tuple[bool, str]:
    """CSRF check for state-changing requests: (ok, why it was refused, for
    the log). Refused unless the request comes from mang-arr's own pages:

    - Sec-Fetch-Site, which browsers set and pages cannot, decides when
      present: only 'same-origin' and 'none' (the user's own action) pass.
      'same-site' is refused too: another port on this host (Suwayomi on
      :4567, Komga on :25600) or a sibling subdomain is same-site, and the
      SameSite=Lax session cookie goes along with its requests.
    - Without it (older browsers) the Origin, else the Referer, must name
      this server's host and port: the Host header, or X-Forwarded-Host
      behind a reverse proxy (a page cannot add either X- header without a
      CORS preflight). A Host (or X-Forwarded-Host) without a port stands for
      the port X-Forwarded-Port names, else the scheme's default (80/443):
      a proxy that drops the port (nginx `$host`) must say which it was, or
      a page on another port of the same host name would pass.
    - A request with none of these headers is not a browser form (curl,
      scripts) and goes on to the normal login check."""
    src = request.headers.get("origin") or request.headers.get("referer")
    site = request.headers.get("sec-fetch-site", "").strip().lower()
    if site:
        if site in ("same-origin", "none"):
            return True, ""
        return False, f"the browser says it came from another site (Sec-Fetch-Site {clip(site)}, " \
                      f"Origin/Referer {clip(src or '-', 200)})"
    if not src:
        return True, ""
    refused = f"Origin/Referer {clip(src, 200)} is not this server"
    try:
        u = urllib.parse.urlsplit(src)
    except ValueError:
        return False, refused
    if u.scheme not in ("http", "https") or not u.netloc:
        return False, refused                    # 'null' (sandboxed frames, no-referrer pages), file:, ...
    want = _host_port(u.netloc, u.scheme)
    fport = request.headers.get("x-forwarded-port", "").split(",")[0].strip()
    for h in (request.headers.get("host", ""), request.headers.get("x-forwarded-host", "").split(",")[0]):
        if not h:
            continue
        name, port = _host_port(h, "")           # port None: this header carries none
        if port is None:
            port = int(fport) if fport.isdigit() else DEFAULT_PORTS.get(u.scheme)
        if (name, port) == want:
            return True, ""
    return False, refused


def key_ok(v: dict, given: str | None) -> bool:
    key = str(v.get("api_key") or "")
    return bool(given and key and hmac.compare_digest(given.encode("utf-8", "replace"), key.encode("utf-8")))


def _sign(v: dict, payload: str) -> str:
    return hmac.new(str(v.get("session_secret") or "").encode(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session(v: dict) -> str:
    """'user|epoch|session id|expiry|signature', signed with the install's
    random session secret. Bumping session_epoch (password, user or API key
    change, 'Sign out everywhere') invalidates every cookie issued before."""
    exp = int(time.time()) + SESSION_DAYS * 86400
    payload = f"{v['auth_user']}|{int(v['session_epoch'] or 0)}|{secrets.token_hex(8)}|{exp}"
    return f"{payload}|{_sign(v, payload)}"


def parse_session(v: dict, cookie: str | None) -> tuple[str, int] | None:
    """(session id, expiry) of a valid session cookie, else None."""
    if not cookie or not v.get("session_secret") or not v.get("auth_user"):
        return None
    try:
        user, epoch, sid, exp, sig = cookie.rsplit("|", 4)
        epoch_n, exp_n = int(epoch), int(exp)
    except ValueError:                           # malformed or old-format cookie: just not signed in
        return None
    if not hmac.compare_digest(sig.encode("utf-8", "replace"), _sign(v, f"{user}|{epoch}|{sid}|{exp}").encode()):
        return None
    if user != v["auth_user"] or epoch_n != int(v["session_epoch"] or 0) or exp_n < time.time():
        return None
    if any(str(r).split(":", 1)[0] == sid.lower() for r in v.get("revoked_sessions") or []):
        return None
    return sid, exp_n


def session_ok(v: dict, cookie: str | None) -> bool:
    return parse_session(v, cookie) is not None


def set_session(request: Request, resp: Response, v: dict, persistent: bool = True) -> None:
    """persistent=False: a browser-session cookie (gone when the browser
    closes, like the Basic credentials it stands in for)."""
    https = request.url.scheme == "https" or \
        request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"
    resp.set_cookie(SESSION_COOKIE, make_session(v), max_age=SESSION_DAYS * 86400 if persistent else None,
                    httponly=True, samesite="lax", secure=https)


_verified: dict[str, float] = {}             # sha256(stored hash, user, password) -> until
_verified_lock = threading.Lock()


def _memo(v: dict, user: str, password: str) -> str:
    stored = str(v.get("auth_password") or "")
    return hashlib.sha256("\0".join((stored, user, password)).encode("utf-8", "surrogatepass")).hexdigest()


def remembered(v: dict, user: str, password: str) -> bool:
    """These credentials were checked and right in the last few minutes (no
    hash). Only a correct password is ever remembered, so a hit is never a
    guess: a Basic client without cookies need not reserve an attempt for
    every request (throttle.reserve), which parallel requests would run out."""
    if not (user and password and v.get("auth_password") and v.get("auth_user")):
        return False
    memo, now = _memo(v, user, password), time.monotonic()
    with _verified_lock:
        return _verified.get(memo, 0) > now


def credentials_ok(v: dict, user: str, password: str) -> bool:
    """Username + password against the login (blocking: PBKDF2, so call it
    from a worker thread). Successes are remembered for a few minutes so a
    Basic-auth browser does not pay the hash on every request. An empty
    password never matches. A password an older version stored in clear is
    re-stored as a hash on its first successful use."""
    stored, want_user = str(v.get("auth_password") or ""), str(v.get("auth_user") or "")
    if not (user and password and stored and want_user):
        return False
    if remembered(v, user, password):
        return True
    memo, now = _memo(v, user, password), time.monotonic()
    user_ok = hmac.compare_digest(user.encode("utf-8", "surrogatepass"), want_user.encode("utf-8"))
    ok = settings.verify_password(stored, password) and user_ok
    if ok:
        with _verified_lock:
            if len(_verified) > 64:
                _verified.clear()
            _verified[memo] = now + 300
        if not settings.is_hashed(stored):
            try:
                with db.connect() as con:
                    settings.ensure_security(con)
            except Exception as e:
                log.error("could not re-store the web password as a hash: %s: %s", type(e).__name__, e)
    return ok


def basic_credentials(header: str) -> tuple[str, str] | None:
    if not header.startswith("Basic "):
        return None
    try:
        given = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return None
    user, sep, password = given.partition(":")
    return (user, password) if sep else None


def basic_ok(v: dict, header: str) -> bool:
    creds = basic_credentials(header)
    return bool(creds) and credentials_ok(v, *creds)


def too_many(wait: int, what: str) -> Response:
    return Response(f"mang-arr: too many failed {what}; try again in {wait} s\n", 429, media_type="text/plain",
                    headers={"Retry-After": str(wait)})


def security_headers(path: str, response: Response) -> None:
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "same-origin")
    if not path.startswith("/api/docs"):         # Swagger UI loads its own inline script and CDN files
        h.setdefault("Content-Security-Policy", CSP)


def safe_next(target: str | None) -> str:
    """Where to go after signing in: a local path only. '//evil.example/x'
    (protocol-relative), '/\\evil', 'https://...' and anything with control
    characters become '/'."""
    t = str(target or "")
    if not t.startswith("/") or t.startswith(("//", "/\\")) or any(ord(c) < 32 or ord(c) == 127 for c in t):
        return "/"
    try:
        u = urllib.parse.urlsplit(t)
    except ValueError:
        return "/"
    return "/" if (u.scheme or u.netloc) else t

"""Outbound HTTP to user-configured endpoints (notification channels, Komga)
and to GitHub for the update check, with the guard rails plain urllib lacks:

- http:// and https:// only. urlopen would also open file:, ftp: and data:
  URLs, so a settings value such as komga_url=file:///etc/passwd# would read
  local files. Checked on every call, not only when a setting is saved,
  because values can also arrive from the environment or a restored backup.
- Redirects are refused unless the caller asks for them. urllib copies
  every header (X-Api-Key, Authorization, X-Gotify-Key, ...) to whatever
  host a redirect names, and turns a redirected POST into a body-less GET
  whose 200 looked like a delivered notification. A refused redirect raises
  an HTTPError whose reason names the new location, so the person can put
  that URL in Settings. When redirects are allowed (the update check) they
  must stay on http(s), and credential headers are dropped when the host
  changes.
- A hard wall-clock deadline for the whole request. The socket timeout
  alone applies to each read, so a server that trickles one byte at a time
  could hold a thread for ever. A watchdog shuts the socket down once the
  deadline passes (or the moment it appears, if connecting took longer);
  the caller gets TimeoutError.
- Response bodies are capped (max_bytes).
"""
import http.client
import logging
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

SCHEMES = ("http", "https")
# headers that may follow a redirect to a different host; everything else
# (API keys, tokens, cookies) stays with the host the person configured
_PORTABLE_HEADERS = {"user-agent", "accept", "content-type"}


def check_url(url: str, what: str = "URL") -> str:
    """The URL if it is an absolute http(s) URL with a host; ValueError otherwise."""
    p = urllib.parse.urlsplit(str(url or "").strip())
    if p.scheme.lower() not in SCHEMES or not p.hostname:
        raise ValueError(f"{what} must be an http:// or https:// URL with a host name")
    return url


class Watchdog:
    """Shut a socket down once a monotonic deadline passes, which makes any
    blocked send/recv on it return at once. get_sock is called at expiry, so
    it can follow a socket that was replaced (TLS wrap, STARTTLS).

    If there is no socket yet at the deadline (a slow DNS lookup or a
    multi-address connect is still running) the watchdog keeps looking
    until cancelled and shuts the socket down as soon as it appears, so a
    connection that completes late cannot then run on under the per-read
    timeout alone."""
    POLL = 0.05     # seconds between looks for a socket that appears after the deadline

    def __init__(self, deadline: float, get_sock):
        self.deadline = deadline
        self._get_sock = get_sock
        self._cancelled = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mangarr-watchdog", daemon=True)

    def start(self) -> "Watchdog":
        self._thread.start()
        return self

    def cancel(self) -> None:
        self._cancelled.set()

    def _run(self) -> None:
        if self._cancelled.wait(max(0.0, self.deadline - time.monotonic())):
            return
        while not self._expire():
            if self._cancelled.wait(self.POLL):
                return

    def _expire(self) -> bool:
        """Shut the socket down; False when there is no socket (yet)."""
        sock = self._get_sock()
        if sock is None:
            return False
        try:
            # the plain-socket method, so an SSL socket's state is not touched from this thread
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        except OSError:
            pass
        return True


class _DeadlineMixin:
    """HTTP(S)Connection that never runs past its request's deadline."""
    deadline: float = 0.0
    _wd_sock = None
    _watchdog: Watchdog | None = None

    def connect(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("deadline passed before connecting")
        if isinstance(self.timeout, (int, float)):
            self.timeout = min(self.timeout, left)
        self._watchdog = Watchdog(self.deadline, lambda: self._wd_sock or self.sock).start()
        super().connect()
        self._wd_sock = self.sock   # urllib drops self.sock before the body is read; keep our own reference
        if time.monotonic() >= self.deadline:
            # DNS or the TCP connect itself used up the time (e.g. a dead IPv6
            # address tried first): give up now rather than start the request
            self.close()
            raise TimeoutError("deadline passed while connecting")

    def disarm(self) -> None:
        if self._watchdog:
            self._watchdog.cancel()


class _HTTPConnection(_DeadlineMixin, http.client.HTTPConnection):
    pass


class _HTTPSConnection(_DeadlineMixin, http.client.HTTPSConnection):
    pass


def _factory(cls, req):
    def make(host, **kw):
        conn = cls(host, **kw)
        conn.deadline = req.mangarr_deadline
        req.mangarr_conns.append(conn)
        return conn
    return make


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_factory(_HTTPConnection, req), req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        kw = {"context": self._context}
        if getattr(self, "_check_hostname", None) is not None:     # Python 3.10/3.11 only
            kw["check_hostname"] = self._check_hostname
        return self.do_open(_factory(_HTTPSConnection, req), req, **kw)


class _RedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        shown = newurl if len(newurl) <= 200 else newurl[:200] + "..."
        if not getattr(req, "mangarr_follow", False):
            log.warning("refused to follow HTTP %d from %s to %s", code, _host(req.full_url), shown)
            raise urllib.error.HTTPError(req.full_url, code, f"redirected to {shown}; use that URL instead",
                                         headers, fp)
        if urllib.parse.urlsplit(newurl).scheme.lower() not in SCHEMES:
            log.warning("refused a redirect from %s to a non-http(s) URL: %s", _host(req.full_url), shown)
            raise urllib.error.HTTPError(req.full_url, code, f"redirect to a non-http(s) URL refused: {shown}",
                                         headers, fp)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if _host(newurl) != _host(req.full_url):
            dropped = [k for k in new.headers if k.lower() not in _PORTABLE_HEADERS]
            for k in dropped:
                del new.headers[k]
            if dropped:
                log.info("redirect to another host (%s): not forwarding %s", _host(newurl), ", ".join(dropped))
        new.mangarr_follow = True
        new.mangarr_deadline = req.mangarr_deadline
        new.mangarr_conns = req.mangarr_conns
        return new


def _host(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{(p.hostname or '').lower()}:{p.port or ''}"


# Built by hand, not with build_opener(), so there is no file:, ftp: or data:
# handler to fall back on.
_OPENER = urllib.request.OpenerDirector()
for _h in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(), _HTTPHandler(), _HTTPSHandler(),
           _RedirectHandler(), urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
    _OPENER.add_handler(_h)


def fetch(url: str, data: bytes | None = None, headers: dict | None = None, method: str | None = None,
          timeout: float = 20.0, deadline: float | None = None, follow_redirects: bool = False,
          max_bytes: int = 1 << 20, what: str = "URL") -> tuple[int, bytes]:
    """(status, body) for one request. `timeout` bounds each socket
    operation, `deadline` (time.monotonic() value; default now + timeout)
    bounds the whole call. Raises ValueError for a non-http(s) URL or an
    oversized answer, urllib.error.HTTPError for 3xx (unless
    follow_redirects), 4xx and 5xx, TimeoutError past the deadline."""
    check_url(url, what)
    start = time.monotonic()
    if deadline is None:
        deadline = start + timeout
    left = deadline - start
    if left <= 0:
        raise TimeoutError("no time left for this request")
    req = urllib.request.Request(url, data, headers or {}, method=method)
    req.mangarr_deadline = deadline
    req.mangarr_follow = follow_redirects
    req.mangarr_conns = []
    try:
        with _OPENER.open(req, timeout=min(timeout, left)) as r:
            status = r.status
            body = r.read(max_bytes + 1)
    except (urllib.error.HTTPError, ValueError):
        raise
    except Exception as e:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"no complete answer within {deadline - start:.0f} s") from e
        raise
    finally:
        for conn in req.mangarr_conns:
            conn.disarm()
    if time.monotonic() >= deadline:          # the watchdog cut the body short
        raise TimeoutError(f"no complete answer within {deadline - start:.0f} s")
    if len(body) > max_bytes:
        log.warning("answer from %s is larger than %d bytes; not reading it", _host(url), max_bytes)
        raise ValueError(f"answer larger than {max_bytes} bytes")
    return status, body

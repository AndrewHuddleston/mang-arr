"""Komga: ask it to rescan after chapters land, so new files show up in
minutes instead of at its next scheduled scan. Optional: needs komga_url and
komga_api_key in Settings (Komga: account menu -> API keys)."""
import json
import logging
import threading
import urllib.error

from . import outbound, settings

log = logging.getLogger(__name__)


def configured() -> bool:
    v = settings.all_values()
    return bool(v["komga_url"] and v["komga_api_key"])


MAX_BYTES = 8 << 20      # a library list is a few KB; never read an endless answer into memory


def _call(method: str, path: str, timeout: int = 20):
    """(status, parsed JSON). Only http(s) komga_url values are used, the
    request is capped at `timeout` seconds in total, and a redirect is not
    followed (it would carry the API key to another host, and turn the scan
    POST into a GET): it is reported as an HTTP error naming the new URL."""
    v = settings.all_values()
    url = v["komga_url"].rstrip("/") + path
    status, body = outbound.fetch(url, method=method, headers={"X-API-Key": v["komga_api_key"],
                                                               "Accept": "application/json"},
                                  timeout=timeout, max_bytes=MAX_BYTES, what="the Komga URL")
    return status, (json.loads(body) if body else None)


def libraries() -> list[dict]:
    status, data = _call("GET", "/api/v1/libraries")
    return data or []


def scan(library_id: str | None = None) -> bool:
    """Trigger a scan of one library (or every library). True on success."""
    if not configured():
        log.debug("komga scan skipped: not configured")
        return False
    v = settings.all_values()
    ids = [library_id or v["komga_library_id"]] if (library_id or v["komga_library_id"]) else None
    try:
        if ids is None:
            ids = [lib["id"] for lib in libraries()]
        for lid in ids:
            _call("POST", f"/api/v1/libraries/{lid}/scan")
        log.info("komga: scan requested for %d librar%s", len(ids), "y" if len(ids) == 1 else "ies")
        return True
    except urllib.error.HTTPError as e:
        log.error("komga scan failed: HTTP %d %s (check the API key and URL)", e.code, e.reason)
    except Exception as e:
        log.error("komga scan failed: %s: %s", type(e).__name__, e)
    return False


# A scan Komga did not answer is tried once more: at the next import (which
# asks for a scan anyway when it links something), or after RETRY_SECONDS,
# whichever comes first. Only once: Komga also scans on its own schedule.
RETRY_SECONDS = 600
_retry_lock = threading.Lock()
_retry: dict = {"due": False, "timer": None}


def scan_retrying(library_id: str | None = None) -> bool:
    """scan(); when Komga does not answer (and is configured), one more try
    later (take_retry, retry_now). True on success."""
    ok = scan(library_id) if library_id else scan()
    if ok:
        take_retry()                    # this scan covers a retry that was waiting
        return True
    if configured():
        with _retry_lock:
            if not _retry["due"]:
                _retry["due"] = True
                t = threading.Timer(RETRY_SECONDS, retry_now)
                t.daemon = True
                _retry["timer"] = t
                t.start()
                log.warning("komga did not answer the scan request; trying once more at the next import or in "
                            "%d min", RETRY_SECONDS // 60)
    return False


def take_retry() -> bool:
    """Whether a retry was waiting; it is not any more (its timer is
    stopped): the caller scans now."""
    with _retry_lock:
        due, t = _retry["due"], _retry["timer"]
        _retry["due"], _retry["timer"] = False, None
    if t is not None:
        t.cancel()
    return due


def retry_now() -> bool | None:
    """The one retry of a scan Komga did not answer; None when none waits."""
    with _retry_lock:
        if not _retry["due"]:
            return None
        t = _retry["timer"]
        _retry["due"], _retry["timer"] = False, None
    if t is not None:
        t.cancel()                      # called by hand, not by the timer: it has nothing left to do
    ok = scan()
    if not ok:
        log.warning("komga did not answer the retried scan either; it shows the change at its own next scan")
    return ok


def test() -> tuple[bool, str]:
    try:
        libs = libraries()
        return True, f"ok: {len(libs)} librar{'y' if len(libs) == 1 else 'ies'}: " + \
            ", ".join(f"{lib['name']} ({lib['id']})" for lib in libs)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} {e.reason}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

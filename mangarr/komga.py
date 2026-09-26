"""Komga: ask it to rescan after chapters land, so new files show up in
minutes instead of at its next scheduled scan. Optional: needs komga_url and
komga_api_key in Settings (Komga: account menu -> API keys)."""
import json
import logging
import urllib.error
import urllib.request

from . import settings

log = logging.getLogger(__name__)


def configured() -> bool:
    v = settings.all_values()
    return bool(v["komga_url"] and v["komga_api_key"])


def _call(method: str, path: str, timeout: int = 20):
    v = settings.all_values()
    url = v["komga_url"].rstrip("/") + path
    req = urllib.request.Request(url, method=method, headers={"X-API-Key": v["komga_api_key"],
                                                              "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return r.status, (json.loads(body) if body else None)


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


def test() -> tuple[bool, str]:
    try:
        libs = libraries()
        return True, f"ok: {len(libs)} librar{'y' if len(libs) == 1 else 'ies'}: " + \
            ", ".join(f"{lib['name']} ({lib['id']})" for lib in libs)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} {e.reason}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

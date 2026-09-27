"""Update check: is there a newer release on GitHub? Checked at most once a
day, never blocks a page (the result is cached; a failed check is logged at
DEBUG and retried later). Shown as a banner in the UI and in
/api/v1/system/status."""
import json
import logging
import re
import threading
import time

from . import __version__, config, outbound

log = logging.getLogger(__name__)

RELEASES_URL = "https://api.github.com/repos/AndrewHuddleston/mang-arr/releases/latest"
INTERVAL = 24 * 3600

_state: dict = {"latest": None, "url": None, "checked_at": 0.0, "error": None}
_lock = threading.Lock()


def _parse(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def newer_available() -> bool:
    latest = _state["latest"]
    return bool(latest) and _parse(latest) > _parse(__version__)


def status() -> dict:
    return {"current": __version__, "latest": _state["latest"], "url": _state["url"],
            "updateAvailable": newer_available(), "checkedAt": _state["checked_at"] or None,
            "error": _state["error"]}


def check(force: bool = False) -> dict:
    """Fetch the latest release tag if the cache is older than INTERVAL."""
    with _lock:
        if not force and time.time() - _state["checked_at"] < INTERVAL:
            return status()
        _state["checked_at"] = time.time()
    try:
        headers = {"User-Agent": config.USER_AGENT, "Accept": "application/vnd.github+json"}
        # GitHub answers a renamed repository with a redirect, so this one follows them (http(s) only,
        # no credentials to forward); 15 s in total and a 1 MB cap keep a slow or odd answer harmless
        _, body = outbound.fetch(RELEASES_URL, headers=headers, timeout=15, follow_redirects=True, max_bytes=1 << 20)
        d = json.loads(body)
        tag = str(d.get("tag_name") or "").lstrip("v")[:40]
        url = d.get("html_url")
        if not (isinstance(url, str) and url.startswith("https://")):     # shown as a link in the UI banner
            url = None
        with _lock:
            _state.update(latest=tag or None, url=url, error=None)
        if newer_available():
            log.info("update available: %s (running %s) %s", tag, __version__, url)
        else:
            log.debug("update check: latest %s, running %s", tag, __version__)
    except Exception as e:
        with _lock:
            _state["error"] = f"{type(e).__name__}: {e}"
        log.debug("update check failed: %s", _state["error"])
    return status()


def start_background(first_delay: float = 60.0) -> None:
    def loop():
        time.sleep(first_delay)
        while True:
            check()
            time.sleep(3600)
    threading.Thread(target=loop, name="mangarr-updates", daemon=True).start()

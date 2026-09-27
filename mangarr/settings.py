"""Runtime settings: stored in the database, editable in the UI, with the
environment/config defaults as fallback. Modules read values at use time
through get(), so a change on the Settings page applies to the next job.

    key                  type   default (from config)
    refresh_hours        float  REFRESH_HOURS
    min_pages            int    MIN_PAGES
    unusable_sources     list   UNUSABLE_SOURCES   (lower-cased source names)
    throttled_sources    list   THROTTLED_SOURCES
    pushover_token       str    PUSHOVER_TOKEN
    pushover_user        str    PUSHOVER_USER
    webhook_url          str    WEBHOOK_URL
    komga_url            str    ""     e.g. http://komga:25600
    komga_api_key        str    ""     triggers a library scan after imports
    komga_library_id     str    ""     optional: scan only this library
    auth_user            str    ""     basic auth for the web UI (empty = off)
    auth_password        str    ""
"""
import json
import logging
import sqlite3
import threading
import time

from . import config

log = logging.getLogger(__name__)

DEFAULTS: dict[str, object] = {
    "refresh_hours": config.REFRESH_HOURS,
    "download_in_order": True,       # per series, strictly in chapter order; a stuck chapter holds only its series
    "recheck_finished_days": 7.0,   # a finished series with nothing missing is re-checked this often
    "min_pages": config.MIN_PAGES,
    "throttled_delay_seconds": 8.0,  # pause between chapters on a rate-limited source (avoids 429 -> long backoff)
    "unusable_sources": sorted(config.UNUSABLE_SOURCES),
    "throttled_sources": sorted(config.THROTTLED_SOURCES),
    "pushover_token": config.PUSHOVER_TOKEN or "",
    "pushover_user": config.PUSHOVER_USER or "",
    "webhook_url": config.WEBHOOK_URL or "",
    "discord_webhook": "",
    "slack_webhook": "",
    "telegram_token": "",
    "telegram_chat_id": "",
    "ntfy_url": "",
    "ntfy_token": "",
    "gotify_url": "",
    "gotify_token": "",
    "smtp_host": "",
    "smtp_port": "",
    "smtp_security": "starttls",   # starttls | ssl | none
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_to": "",
    "notifiarr_api_key": "",
    "notifiarr_channel": "",
    "apprise_url": "",
    "notify_on_new": True,
    "notify_on_added": False,
    "notify_on_failed": True,
    "notify_on_health": True,
    "komga_url": "",
    "komga_api_key": "",
    "komga_library_id": "",
    "auth_method": "forms",  # forms (login page) | basic (browser prompt); active only when auth_user is set
    "auth_user": "",
    "auth_password": "",
    "api_key": "",           # X-Api-Key for the JSON API when a web login is set; generated on first start
}
SECRET_KEYS = {"pushover_token", "pushover_user", "komga_api_key", "auth_password", "discord_webhook", "slack_webhook",
               "telegram_token", "ntfy_token", "gotify_token", "smtp_password", "notifiarr_api_key"}
MASK = "********"        # what the UI shows for a stored secret; submitting it unchanged keeps the value


def masked(values: dict) -> dict:
    """The values for a form: secrets replaced by MASK when set."""
    out = dict(values)
    for k in SECRET_KEYS:
        if out.get(k):
            out[k] = MASK
    return out


def ensure_api_key(con: sqlite3.Connection) -> str:
    """Create the API key on first start; returns it."""
    import secrets as _secrets
    v = all_values(con)
    if not v["api_key"]:
        set_many(con, {"api_key": _secrets.token_hex(16)})
        v = all_values(con)
    return str(v["api_key"])

_cache: dict[str, object] = {}
_loaded_at = 0.0
_lock = threading.Lock()
TTL = 5.0        # seconds between re-reads; the UI and jobs share one file


def _load(con: sqlite3.Connection) -> dict[str, object]:
    out = dict(DEFAULTS)
    try:
        for r in con.execute("SELECT key, value FROM setting"):
            if r["key"] in DEFAULTS:
                try:
                    out[r["key"]] = json.loads(r["value"])
                except json.JSONDecodeError:
                    log.warning("setting %s has unreadable value; using default", r["key"])
    except sqlite3.OperationalError:          # table not there yet (first migrate)
        pass
    return out


def refresh(con: sqlite3.Connection) -> None:
    global _cache, _loaded_at
    with _lock:
        _cache = _load(con)
        _loaded_at = time.monotonic()


def all_values(con: sqlite3.Connection | None = None) -> dict[str, object]:
    global _cache, _loaded_at
    with _lock:
        stale = time.monotonic() - _loaded_at > TTL or not _cache
    if stale:
        try:
            if con is None:
                from . import db
                with db.connect() as c:
                    refresh(c)
            else:
                refresh(con)
        except Exception as e:                # unreadable DB: run on defaults, say so once
            global _warned
            if not _warned:
                log.warning("settings unavailable (%s: %s); using defaults", type(e).__name__, e)
                _warned = True
            with _lock:
                _cache = dict(DEFAULTS)
                _loaded_at = time.monotonic()
    with _lock:
        return dict(_cache)


_warned = False


def get(key: str):
    return all_values().get(key, DEFAULTS[key])


def set_many(con: sqlite3.Connection, values: dict[str, object]) -> None:
    """Store values. A secret submitted as MASK (the form's placeholder for a
    stored secret) keeps its current value; anything else, including an
    empty field, is stored as given."""
    current = all_values(con)
    for k, v in values.items():
        if k not in DEFAULTS:
            raise KeyError(k)
        if k in SECRET_KEYS and isinstance(v, str):
            if v.strip() == MASK or (v == current.get(k)):
                continue
            v = v.strip()
        v = _coerce(k, v)
        con.execute("INSERT INTO setting (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v)))
        log.info("setting %s = %s", k, "***" if k in SECRET_KEYS and v else v)
    con.commit()
    refresh(con)


def _coerce(key: str, v):
    d = DEFAULTS[key]
    if isinstance(d, bool):
        return str(v).lower() in ("1", "true", "on", "yes")
    if isinstance(d, float):
        from .limits import RANGES, bound  # imported here: limits imports this module
        f = float(v)
        if key in RANGES:
            # Clamped with a warning, not refused: the Settings form posts
            # every field back, so an out-of-range value stored by an older
            # version (or an out-of-range env default) must not make every
            # later save fail until the user spots and edits that field.
            out = bound(key, f)
            if out != f:                        # also true for NaN
                log.warning("setting %s = %r is outside %g..%g; saved as %g", key, v, *RANGES[key], out)
            return out
        return f
    if isinstance(d, int):
        return int(v)
    if isinstance(d, list):
        if isinstance(v, str):
            v = [s for s in v.replace("\n", ",").split(",")]
        return sorted({str(s).strip().lower() for s in v if str(s).strip()})
    return str(v).strip()


def source_flags(name: str) -> tuple[bool, bool]:
    """(unusable, throttled) for a Suwayomi source display name."""
    k = name.lower().strip()
    vals = all_values()
    return k in vals["unusable_sources"], k in vals["throttled_sources"]

"""Runtime settings: stored in the database, editable in the UI, with the
environment/config defaults as fallback. Modules read values at use time
through get(), so a change on the Settings page applies to the next job.

    key                  type   default (from config)
    refresh_hours        float  REFRESH_HOURS
    min_pages            int    MIN_PAGES
    unusable_sources     list   UNUSABLE_SOURCES   (lower-cased source names)
    throttled_sources    list   THROTTLED_SOURCES
    page_warm_sources    list   PAGE_WARM_SOURCES  fetched page by page (image server refuses bursts)
    download_lanes       int    3      sources downloading at once in a pass (1-8; Suwayomi's cap wins)
    search_parallel      int    5      sites searched at once while a series is resolved (1-8)
    full_search_days     float  7      days between searches of every source for a series (0 = every pass)
    page_delay_seconds   float  2.5    starting and minimum gap between page requests (page by page)
    download_in_order    bool   True   strict download in order, per series
    auto_skip_side_stories bool False  skip a blocking chapter judged a side story or covered (stuck.py)
    series_folder_format str    {Series Title}                              names of new series folders ...
    chapter_file_format  str    Chapter {Chapter:000.0}{ - Chapter Title}   ... and of new chapter files
    colon_replacement    str    underscore   what a ':' becomes: underscore, delete, dash, space_dash, smart
    replace_illegal_characters bool True     ? * " < > | become '_' (off: removed)
    chapter_title_max_chars int 80     a chapter title in a file name is cut to this many characters (1-255)
    drop_number_only_titles bool False leave out titles that are only numbers ("Vol.3 chapter 13")
    pushover_token       str    PUSHOVER_TOKEN
    pushover_user        str    PUSHOVER_USER
    webhook_url          str    WEBHOOK_URL
    komga_url            str    ""     e.g. http://komga:25600
    komga_api_key        str    ""     triggers a library scan after imports
    komga_library_id     str    ""     optional: scan only this library
    auth_user            str    ""     web login (empty = off)
    auth_password        str    ""     stored as a salted PBKDF2 hash, never in clear
    api_key              str    ""     X-Api-Key; generated on first start, rotatable
    allowed_hosts        list   []     extra Host names the web UI answers to

The naming settings (NAMING_KEYS, see naming.py) name files and folders made
from then on; existing ones keep their names until they are renamed
(renamer.py). A naming value that cannot be used is refused with the reason,
never mended or clamped: nothing is stored then.

Secrets (SECRET_KEYS) are masked in the UI and the API and logged as ***.
A secret is bound to its destination: when komga_url, gotify_url, ntfy_url
or smtp_host changes and the paired secret is not re-entered, the stored
secret is cleared instead of being sent to the new destination.

Reads fail closed: when the database cannot be read, the last good values
stay in use; if there never were any, available() is False and the web UI
refuses requests (503) instead of running with login switched off.
"""
import base64
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import threading
import time
import urllib.parse
from collections.abc import Callable

from . import config, naming

log = logging.getLogger(__name__)

DEFAULTS: dict[str, object] = {
    "refresh_hours": config.REFRESH_HOURS,
    "download_in_order": True,       # per series, strictly in chapter order; a stuck chapter holds only its series
    "auto_skip_side_stories": False,  # skip a stuck chapter judged a side story or covered, high confidence only
    "recheck_finished_days": 7.0,   # a finished series with nothing missing is re-checked this often
    # every source is searched by title for a series this often; passes in between read the chapter
    # lists of the entries they know (0 = search every source every pass)
    "full_search_days": 7.0,
    "min_pages": config.MIN_PAGES,
    "throttled_delay_seconds": 8.0,  # pause between chapters on a rate-limited source (avoids 429 -> long backoff)
    "download_lanes": 3,             # sources downloading at once in a pass, one series each (see limits.RANGES)
    "search_parallel": 5,            # sites searched at once while a series is resolved, one search each
    "page_delay_seconds": 2.5,       # gap between page requests on a page-by-page source (grows when it is busy)
    # how new library folders and files are named (naming.py); existing ones change only through a rename
    **{k: getattr(naming.DEFAULTS, k) for k in naming.OPTION_KEYS},
    "unusable_sources": sorted(config.UNUSABLE_SOURCES),
    "throttled_sources": sorted(config.THROTTLED_SOURCES),
    "page_warm_sources": sorted(config.PAGE_WARM_SOURCES),
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
    "allowed_hosts": [],     # Host names besides IPs, localhost and LAN names (see web.app, MANGARR_ALLOWED_HOSTS)
    # internal: never shown, never set from a form or the API (INTERNAL_KEYS)
    "session_secret": "",    # signs login cookies; random per install
    "session_epoch": 0,      # bumped to sign every session out (logout-all, password / user / API key change)
    "revoked_sessions": [],  # "sid:expiry" of sessions signed out one by one
    "leftover_queue_ids": [],  # chapter ids mang-arr may have left in Suwayomi's queue (downloader.leftovers)
}
SECRET_KEYS = {"pushover_token", "pushover_user", "komga_api_key", "auth_password", "discord_webhook", "slack_webhook",
               "telegram_token", "ntfy_token", "gotify_token", "smtp_password", "notifiarr_api_key",
               # the key itself and URLs whose path is the credential (webhook ids, ntfy topics, Apprise keys)
               "api_key", "webhook_url", "apprise_url", "ntfy_url", "session_secret"}
INTERNAL_KEYS = {"session_secret", "session_epoch", "revoked_sessions", "leftover_queue_ids"}
NAMING_KEYS = naming.OPTION_KEYS
ID_LIST_KEYS = {"leftover_queue_ids"}      # lists of integer ids, kept in the order given
MASK = "********"        # what the UI shows for a stored secret; submitting it unchanged keeps the value
# a secret belongs to the destination it was entered for: change the destination
# without re-entering the secret and the secret is cleared, never sent on
DESTINATION_SECRETS = {"komga_url": ("komga_api_key",), "gotify_url": ("gotify_token",),
                       "ntfy_url": ("ntfy_token",), "smtp_host": ("smtp_password",)}
# changing any of these signs every browser session out
SESSION_KEYS = ("auth_user", "auth_password", "api_key")
Allowed = Callable[[dict], bool]    # still_allowed(stored values): may this change still be made? (see write)


def masked(values: dict) -> dict:
    """The values for a form: secrets replaced by MASK when set, internal
    keys left out."""
    out = {k: v for k, v in values.items() if k not in INTERNAL_KEYS}
    for k in SECRET_KEYS:
        if out.get(k):
            out[k] = MASK
    return out


def ensure_api_key(con: sqlite3.Connection) -> str:
    """Create the API key on first start; returns it."""
    v = all_values(con)
    if not v["api_key"]:
        set_many(con, {"api_key": secrets.token_hex(16)})
        v = all_values(con)
    return str(v["api_key"])


def ensure_security(con: sqlite3.Connection) -> None:
    """Run at startup: an API key and a session-signing secret exist, and a
    password stored in clear by an older version (or a restored backup) is
    replaced by its hash."""
    ensure_api_key(con)
    v = all_values(con)
    if not v["session_secret"]:
        # a new secret invalidates every older cookie, so the epoch and revocation list are
        # (re)written too: that also repairs unreadable ones (see _load)
        try:
            epoch = int(v["session_epoch"] or 0)
        except (TypeError, ValueError):
            epoch = 0
        for k, val in (("session_secret", secrets.token_hex(32)), ("session_epoch", epoch), ("revoked_sessions", [])):
            _store(con, k, val)
        con.commit()
        refresh(con)
    pw = str(v["auth_password"] or "")
    if pw and not is_hashed(pw):
        _store(con, "auth_password", hash_password(pw))
        con.commit()
        refresh(con)
        log.info("web login password was stored in clear; replaced it with a salted hash")


def reset_login(con: sqlite3.Connection) -> None:
    """MANGARR_RESET_LOGIN=1 at startup: clear the web login (for an owner
    locked out: forgotten password, or a username left without one) and sign
    every session out. Written directly, so it also repairs unreadable values."""
    for k, v in (("auth_user", ""), ("auth_password", ""), ("session_secret", secrets.token_hex(32)),
                 ("revoked_sessions", [])):
        _store(con, k, v)
    con.commit()
    refresh(con)
    log.warning("MANGARR_RESET_LOGIN is set: the web login was cleared and every session signed out; anyone who can "
                "reach mang-arr can use it now. Set a new login in Settings -> Security and remove "
                "MANGARR_RESET_LOGIN, or the login is cleared again at the next start")


def rotate_api_key(con: sqlite3.Connection, still_allowed: Allowed | None = None) -> str:
    """A new random API key (Settings -> Security -> Regenerate). Signs every
    browser session out too. still_allowed: see write()."""
    return str(write(con, {"api_key": secrets.token_hex(16)}, still_allowed=still_allowed)[1]["api_key"])


def logout_everywhere(con: sqlite3.Connection, still_allowed: Allowed | None = None) -> None:
    """Invalidate every session cookie issued so far. still_allowed: see write()."""
    v = all_values(con)
    set_many(con, {"session_epoch": int(v["session_epoch"] or 0) + 1, "revoked_sessions": []}, internal=True,
             still_allowed=still_allowed)
    log.info("every web session signed out")


# -- passwords: salted PBKDF2-SHA256 (stdlib), "pbkdf2_sha256$iterations$salt$hash" --------------------

PBKDF2_ITERATIONS = 120_000
_HASH_PREFIX = "pbkdf2_sha256$"


def is_hashed(stored: str) -> bool:
    return str(stored).startswith(_HASH_PREFIX)


def hash_password(password: str, iterations: int | None = None) -> str:
    n = iterations or PBKDF2_ITERATIONS
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, n)
    return f"{_HASH_PREFIX}{n}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(stored: str, given: str) -> bool:
    """Constant-time check of a password against the stored hash (or, for a
    value an older version stored in clear, against that). An empty stored
    or given password never matches."""
    stored, given = str(stored or ""), str(given or "")
    if not stored or not given:
        return False
    if not is_hashed(stored):
        return hmac.compare_digest(stored.encode("utf-8"), given.encode("utf-8"))
    try:
        _, n, salt, want = stored.split("$", 3)
        dk = hashlib.pbkdf2_hmac("sha256", given.encode("utf-8"), base64.b64decode(salt), int(n))
        return hmac.compare_digest(dk, base64.b64decode(want))
    except (ValueError, TypeError) as e:
        log.error("stored web password hash is unreadable (%s); login refused until it is set again", e)
        return False

_cache: dict[str, object] = {}
_loaded_at = 0.0
_good = False     # _cache holds values really read from the database
_lock = threading.Lock()
TTL = 5.0        # seconds between re-reads; the UI and jobs share one file


UNREADABLE_USER = "(unreadable)"   # auth_user when the stored login cannot be decoded: login on, nobody signs in


def _load(con: sqlite3.Connection) -> dict[str, object]:
    """The stored values over the defaults. Any error reading the table
    propagates: treating an unreadable table as 'nothing set' would switch
    the web login off (fail open). Likewise a corrupt login or session value
    never falls back to its default: an unreadable auth_user/auth_password
    leaves a login nobody can pass with a password (API key and
    MANGARR_RESET_LOGIN still work), an unreadable session value signs every
    session out (default epoch 0 would revive revoked cookies)."""
    out = dict(DEFAULTS)
    bad = []
    for r in con.execute("SELECT key, value FROM setting"):
        if r["key"] in DEFAULTS:
            try:
                out[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                bad.append(r["key"])
                log.warning("setting %s has unreadable value; using default", r["key"])
    if {"auth_user", "auth_password"} & set(bad):
        out["auth_user"], out["auth_password"] = UNREADABLE_USER, ""
        log.error("the stored web login is unreadable: password sign-in refused until it is set again "
                  "(use the API key, or restart with MANGARR_RESET_LOGIN=1)")
    if {"session_secret", "session_epoch", "revoked_sessions"} & set(bad):
        out["session_secret"] = ""
        log.error("stored session data is unreadable: every web session is signed out")
    return out


def stored(con: sqlite3.Connection) -> dict[str, object]:
    """The values stored in this database now (all_values() may answer from
    its cache): for a decision that must see the latest write, or for another
    database file such as a backup."""
    return _load(con)


def refresh(con: sqlite3.Connection) -> None:
    global _cache, _loaded_at, _good
    values = _load(con)
    with _lock:
        _cache, _loaded_at, _good = values, time.monotonic(), True


def stale() -> bool:
    """True when the next all_values() will read the database."""
    with _lock:
        return time.monotonic() - _loaded_at > TTL or not _cache


def available() -> bool:
    """False when settings have never been read successfully: the login
    state is unknown, so the web UI must refuse requests."""
    with _lock:
        return bool(_good and _cache)


def all_values(con: sqlite3.Connection | None = None) -> dict[str, object]:
    global _cache, _loaded_at, _warned
    if stale():
        try:
            if con is None:
                from . import db
                with db.connect() as c:
                    refresh(c)
            else:
                refresh(con)
            if _warned:
                log.warning("settings readable again")
                _warned = False
        except Exception as e:                # unreadable DB: keep the last good values, never the defaults
            with _lock:
                have_good = bool(_good and _cache)
                if not have_good:
                    _cache = dict(DEFAULTS)   # for callers that only need a default; available() stays False
                _loaded_at = time.monotonic()  # retry after TTL, not on every call
            if not _warned:
                log.warning("settings unavailable (%s: %s); %s", type(e).__name__, e,
                            "keeping the last values read" if have_good else
                            "the web UI refuses requests until the database can be read")
                _warned = True
    with _lock:
        return dict(_cache)


_warned = False


def get(key: str):
    return all_values().get(key, DEFAULTS[key])


class NotAllowed(PermissionError):
    """A settings write refused because the caller's sign-in no longer holds
    (write()'s still_allowed): nothing was stored."""


# One settings write at a time in this process, and none while a restore swaps the database
# (backup.restore holds it from reading the current login until the swap), so neither undoes the other.
write_lock = threading.RLock()
LOGIN_ON_NOTICE = ("login switched on, so the API key was regenerated (the old one was readable while there was "
                   "no login)")


def set_many(con: sqlite3.Connection, values: dict[str, object], internal: bool = False,
             still_allowed: Allowed | None = None) -> list[str]:
    """Store values; returns notices for the user. See write()."""
    return write(con, values, internal, still_allowed)[0]


def write(con: sqlite3.Connection, values: dict[str, object], internal: bool = False,
          still_allowed: Allowed | None = None) -> tuple[list[str], dict[str, object]]:
    """Store values. A secret submitted as MASK (the form's placeholder for a
    stored secret) keeps its current value; anything else, including an
    empty field, is stored as given. Everything is validated before anything
    is written (ValueError / KeyError, nothing stored). Returns notices for
    the user (e.g. a secret cleared because its destination changed) and the
    values as this write left them. INTERNAL_KEYS can only be set with
    internal=True.

    Reading, deciding and writing happen in one IMMEDIATE transaction, so no
    other write lands in between. still_allowed(stored values) is asked
    inside it: a web request's sign-in is checked again there, against what
    is stored now (NotAllowed when it no longer holds). A request can wait a
    while between the login check and its handler, and a login switched on,
    or a key replaced, meanwhile must not let it through.

    Switching a login on also replaces the API key (unless this write sets a
    new one) and the session secret: both were readable by anyone while
    there was no login (GET /api/v1/settings, a database download), and a
    copy must not open the new login."""
    with write_lock:
        began = not con.in_transaction
        if began:
            con.execute("BEGIN IMMEDIATE")
        try:
            current = _load(con)
            if still_allowed is not None and not still_allowed(current):
                raise NotAllowed("the sign-in this change was made with no longer holds")
            new, notices = _changes(con, current, values, internal)
            con.commit()
        except BaseException:
            if began:
                con.rollback()
            raise
        refresh(con)
    return notices, {**current, **new}


def _changes(con: sqlite3.Connection, current: dict, values: dict, internal: bool) -> tuple[dict, list[str]]:
    """write()'s body, inside its transaction: validate, then store what changed."""
    new: dict[str, object] = {}
    for k, v in values.items():
        if k not in DEFAULTS or (k in INTERNAL_KEYS and not internal):
            raise KeyError(k)
        if v is None:
            raise ValueError(f"{k}: a value is required (send \"\" to clear it)")
        if k in NAMING_KEYS:
            v = _naming_value(k, v)                     # refused when it cannot be used, never mended
            if v != current.get(k) or type(v) is not type(current.get(k)):
                new[k] = v
            continue
        if k in SECRET_KEYS and isinstance(v, str):
            if v.strip() == MASK or (v == current.get(k)):
                continue
            if k != "auth_password":                    # passwords are taken exactly as typed
                v = v.strip()
        elif v == current.get(k) or (isinstance(v, str) and v.strip() == current.get(k)):
            # sent back as stored: nothing to check or write. The form posts every field, so a value an
            # older version stored unchecked (a Komga URL without http://) must not block every save.
            continue
        v = _coerce(k, v)
        if k == "auth_password":
            if (not v and not current.get(k)) or (v and verify_password(str(current.get(k) or ""), str(v))):
                continue                                # same password again: keep the hash (and the sessions)
        elif v == current.get(k):
            continue                                    # unchanged: no write, no log line
        new[k] = v
    if current.get("auth_user") == UNREADABLE_USER and "auth_user" in new:
        new.setdefault("auth_password", "")              # rewrite the unreadable pair together (see _load)
    notices = _unbind_moved_secrets(current, values, new)
    if any(k in new for k in ("auth_user", "auth_password", "auth_method")):
        _validate({**current, **new})
    if any(k in new for k in NAMING_KEYS):
        _validate_naming({**current, **new}, new)
    if "auth_password" in new and new["auth_password"]:
        new["auth_password"] = hash_password(str(new["auth_password"]))
    if not current.get("auth_user") and new.get("auth_user"):
        if "api_key" not in new:                         # a key set in this same write was never readable
            new["api_key"] = secrets.token_hex(16)
            notices.append(LOGIN_ON_NOTICE)
        new["session_secret"] = secrets.token_hex(32)   # a cookie forged with the old one must not pass
        if LOGIN_ON_NOTICE in notices:
            log.warning("settings: %s; the session secret was replaced too", LOGIN_ON_NOTICE)
        else:
            log.info("settings: login switched on with a new API key; the session secret was replaced (the old "
                     "one was readable while there was no login)")
    if not internal and any(k in new for k in SESSION_KEYS):
        new["session_epoch"] = int(current.get("session_epoch") or 0) + 1
        new["revoked_sessions"] = []
        log.info("login or API key changed: every web session is signed out")
    for k, v in new.items():
        _store(con, k, v)
        if k not in INTERNAL_KEYS:
            log.info("setting %s = %s", k, _loggable(k, v))
    return new, notices


def _store(con: sqlite3.Connection, k: str, v) -> None:
    con.execute("INSERT INTO setting (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v)))


def _loggable(k: str, v) -> str:
    """What the log may show: never a secret, and for other URLs only
    scheme://host (paths and query strings often carry tokens)."""
    if k in SECRET_KEYS:
        return "***" if v else "(cleared)"
    if k.endswith("_url") and isinstance(v, str) and v:
        return redact_url(v)
    return str(v)


def redact_url(url: str) -> str:
    """scheme://host[:port]/... without user info, path or query."""
    try:
        u = urllib.parse.urlsplit(url)
        host = u.hostname or ""
        if u.port:
            host = f"{host}:{u.port}"
        return f"{u.scheme}://{host}" + ("/..." if (u.path.strip("/") or u.query) else "") if u.scheme else "..."
    except ValueError:
        return "..."


def bare_host(host: str) -> str:
    """A Host header value as a name: lower case, no port, no trailing dot.
    'Mangarr.LAN:6789' -> 'mangarr.lan', '[::1]:6789' -> '::1'."""
    h = (host or "").strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else ""
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h.rstrip(".")


def host_name(entry: str) -> str:
    """An Allowed Host Names entry in the form the Host check compares:
    what bare_host() makes of the Host header, so an entry typed with a port
    or pasted as a URL still matches. 'manga.example.com:8443' and
    'https://manga.example.com/' -> 'manga.example.com'; '*' and a leading
    '.' (a whole domain) are kept."""
    h = str(entry).strip()
    if "://" in h:
        h = h.split("://", 1)[1]
    for sep in "/?#":
        h = h.split(sep, 1)[0]
    return bare_host(h)


def _unbind_moved_secrets(current: dict, submitted: dict, new: dict) -> list[str]:
    """When a destination changes and its secret was not re-entered (left as
    the MASK or not sent), clear the secret so it is never sent to the new
    destination. Adds the clears to `new`; returns what to tell the user."""
    notices = []
    for dest, keys in DESTINATION_SECRETS.items():
        if dest not in new:
            continue                                    # destination unchanged
        for k in keys:
            given = submitted.get(k)
            if current.get(k) and k not in new and (given is None or str(given).strip() == MASK):
                new[k] = ""
                msg = f"{dest} changed, so the stored {k} was cleared: enter it again for the new destination"
                log.warning("settings: %s", msg)
                notices.append(msg)
    return notices


def _validate(v: dict) -> None:
    """Cross-field rules on the merged values."""
    user = str(v.get("auth_user") or "")
    if user:
        if ":" in user or any(ord(c) < 32 or ord(c) == 127 for c in user):
            raise ValueError("auth_user: the username cannot contain ':' or control characters")
        if not v.get("auth_password"):
            raise ValueError("auth_password: a password is required when a username is set "
                             "(clear the username to turn the login off)")
    if v.get("auth_method") not in ("forms", "basic"):
        raise ValueError("auth_method: must be 'forms' or 'basic'")


_ON, _OFF = ("1", "true", "on", "yes"), ("0", "false", "off", "no", "")
NAMING_LABELS = {"series_folder_format": "Series Folder Format", "chapter_file_format": "Chapter File Format",
                 "colon_replacement": "Colon Replacement", "replace_illegal_characters": "Replace Illegal Characters",
                 "chapter_title_max_chars": "Chapter Title Length", "drop_number_only_titles": "Number-only Titles"}


def _naming_value(key: str, v):
    """A naming setting as its type, from a form field or JSON: a format as
    typed (not trimmed: text around the tokens is part of the name), on/off,
    a whole number. ValueError when it is not that; whether the value can be
    used is checked on all naming settings together (_validate_naming)."""
    d = DEFAULTS[key]
    label = NAMING_LABELS[key]
    if isinstance(d, bool):
        if isinstance(v, bool):
            return v
        text = str(v).strip().lower() if isinstance(v, (str, int)) else None
        if text in _ON or text in _OFF:
            return text in _ON
        raise ValueError(f"{key}: {label} must be on or off")
    if isinstance(d, int):
        try:
            if isinstance(v, bool) or not isinstance(v, (str, int, float)):
                raise ValueError
            n = int(v.strip()) if isinstance(v, str) else v
            if n != int(n):
                raise ValueError
            return int(n)
        except (ValueError, OverflowError):
            raise ValueError(f"{key}: {label} must be a whole number from 1 to {naming.TITLE_MAX}") from None
    if not isinstance(v, str):
        raise ValueError(f"{key}: {label} must be text")
    return v.strip().lower() if key == "colon_replacement" else v


def _validate_naming(merged: dict, new: dict) -> None:
    """The naming settings as they would be after this write must be usable
    (naming.check_options); ValueError with every reason otherwise. Only
    what this write changes is held to it, so a value an older version or a
    restored backup left unusable does not block saving the others."""
    labels = {NAMING_LABELS[k] for k in new if k in NAMING_KEYS}
    errors = [m for m in naming.check_options(naming.as_options(merged))
              if any(m.startswith(label) for label in labels)]
    if errors:
        log.warning("rejected naming settings: %s", " ".join(errors))
        raise ValueError(" ".join(errors))


_naming_warned: set = set()


def naming_options(con: sqlite3.Connection | None = None, values: dict | None = None) -> naming.Options:
    """The naming settings as naming.Options, for naming a new file or
    folder. Stored values that cannot be used (written by hand, or by a
    restored backup) never stop an import: the defaults are used instead,
    and the log says so once. See checked_naming_options for a rename."""
    options, errors = checked_naming_options(con, values)
    if errors:
        key = tuple(errors)
        if key not in _naming_warned:
            _naming_warned.add(key)
            log.error("the stored naming settings cannot be used (%s); new files and folders get the default "
                      "names until they are corrected in Settings -> Media Management", " ".join(errors))
        return naming.DEFAULTS
    return options


def checked_naming_options(con: sqlite3.Connection | None = None,
                           values: dict | None = None) -> tuple[naming.Options, list[str]]:
    """(the naming settings, what is wrong with them: [] when usable)."""
    values = values if values is not None else all_values(con)
    try:
        options = naming.as_options({k: values[k] for k in NAMING_KEYS if k in values})
        return options, naming.check_options(options)
    except Exception as e:          # a value of a type check_options cannot even look at
        return naming.DEFAULTS, [f"{type(e).__name__}: {e}"]


# Settings holding a URL that mang-arr sends requests to: http(s) only (urllib
# would also open file:, ftp: and data: URLs). outbound.fetch checks again at
# use time, for values that arrive from the environment or a restored backup.
URL_KEYS = {"komga_url", "webhook_url", "apprise_url", "discord_webhook", "slack_webhook", "ntfy_url", "gotify_url"}


def invalid_urls(values: dict) -> set[str]:
    """URL settings whose stored value is not an http(s) URL (an older
    version stored it unchecked): requests to it fail until it is corrected,
    so the Settings page points them out."""
    from .outbound import check_url
    bad = set()
    for k in URL_KEYS:
        v = str(values.get(k) or "").strip()
        if v:
            try:
                check_url(v, k)
            except ValueError:
                bad.add(k)
    return bad


def _coerce(key: str, v):
    if key == "smtp_security":           # fail closed: an unknown mode used to mean a plaintext login
        v = str(v).strip().lower() or "starttls"
        if v not in ("starttls", "ssl", "none"):
            log.warning("rejected smtp_security %r: must be starttls, ssl or none", v)
            raise ValueError("smtp_security must be starttls, ssl or none")
        return v
    if key in URL_KEYS and str(v).strip():
        from .outbound import check_url
        try:
            return check_url(str(v).strip(), key)
        except ValueError:
            log.warning("rejected %s: not an http:// or https:// URL", key)   # value not logged: may be secret
            raise
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
        from .limits import RANGES, bound
        if key in RANGES:                       # clamped like the floats above; not a number is still refused
            f = float(v)
            out = int(round(bound(key, f)))
            if out != f:
                log.warning("setting %s = %r is outside %g..%g or not a whole number; saved as %d", key, v,
                            *RANGES[key], out)
            return out
        return int(v)
    if key in ID_LIST_KEYS:
        if not isinstance(v, (list, tuple)):
            raise ValueError(f"{key}: expected a list of ids")
        return list(dict.fromkeys(int(i) for i in v))
    if isinstance(d, list):
        if isinstance(v, str):
            v = [s for s in v.replace("\n", ",").split(",")]
        elif not isinstance(v, (list, tuple, set)):
            raise ValueError(f"{key}: expected a list or comma-separated text")
        items = {str(s).strip().lower() for s in v if str(s).strip()}
        if key == "allowed_hosts":                  # stored as compared: no port, scheme or path
            names = {h: host_name(h) for h in items}
            for typed, name in names.items():
                if typed != name:
                    log.info("allowed_hosts: %r stored as %r (only the name is compared, never the port)",
                             typed, name)
            items = set(names.values()) - {""}
        return sorted(items)
    if not isinstance(v, (str, int, float)):
        raise ValueError(f"{key}: expected text")
    if key == "auth_password":
        return str(v)
    return str(v).strip()


def source_flags(name: str) -> tuple[bool, bool]:
    """(unusable, throttled) for a Suwayomi source display name."""
    k = name.lower().strip()
    vals = all_values()
    return k in vals["unusable_sources"], k in vals["throttled_sources"]

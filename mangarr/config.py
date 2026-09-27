"""Runtime settings. Environment variables override the defaults."""
import logging
import math
import os

log = logging.getLogger(__name__)


def env_number(name: str, default: float, lo: float, hi: float, integer: bool = False) -> float:
    """A number from the environment, read once at start-up. A value that is
    not a number (a typo like '90d', '1G' or '6h') falls back to the default,
    and one outside lo..hi is clamped to it, both with a warning: a bad
    variable must neither stop the app from starting (the container would
    restart in a loop) nor turn into a value that breaks it (no backups kept,
    a NaN interval). Every numeric MANGARR_* variable goes through here."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        v = float(raw)
    except ValueError:
        v = math.nan
    if not math.isfinite(v):
        log.warning("%s=%r is not a number; using the default %g", name, raw, default)
        return default
    out = min(max(v, lo), hi)
    if out != v:
        log.warning("%s=%r is outside %g..%g; using %g", name, raw, lo, hi, out)
    if integer:
        if out != int(out):
            log.warning("%s=%r is not a whole number; using %d", name, raw, round(out))
        return int(round(out))
    return out


USER_AGENT = "mang-arr/0.1 (+https://github.com/AndrewHuddleston/mang-arr)"

SUWAYOMI_URL = os.environ.get("MANGARR_SUWAYOMI_URL", "http://localhost:4567")
ANILIST_URL = "https://graphql.anilist.co"
MANGADEX_URL = "https://api.mangadex.org"

DATA_DIR = os.environ.get("MANGARR_DATA", "/var/lib/mangarr")
DB_PATH = os.environ.get("MANGARR_DB", os.path.join(DATA_DIR, "mangarr.db"))
LOCK_PATH = os.environ.get("MANGARR_LOCK", os.path.join(DATA_DIR, "download.lock"))
LOG_FILE = os.environ.get("MANGARR_LOG_FILE")          # e.g. /var/lib/mangarr/mangarr.log
LOG_LEVEL = os.environ.get("MANGARR_LOG_LEVEL", "INFO")

# Where Suwayomi writes chapters: <STAGING_ROOT>/<Source>/<Series>/*.cbz.
# Never renamed - Suwayomi treats it as its record of what is downloaded.
STAGING_ROOT = os.environ.get("MANGARR_STAGING", os.path.join(DATA_DIR, "staging"))
# The clean per-series tree Komga reads: <LIBRARY_ROOT>/<Series>/Chapter 012.0.cbz.
# Must be on the same filesystem AND mount as STAGING_ROOT for hard links.
LIBRARY_ROOT = os.environ.get("MANGARR_LIBRARY", os.path.join(DATA_DIR, "library"))

# The worker re-checks every monitored series this often, starting this many
# minutes after launch. (The interval range is the one limits.RANGES enforces.)
REFRESH_HOURS = env_number("MANGARR_REFRESH_HOURS", 6.0, 0.25, 168.0)
FIRST_REFRESH_MIN = env_number("MANGARR_FIRST_REFRESH_MIN", 5.0, 0.0, 1440.0)

# Notifications: Pushover, and/or a generic JSON webhook.
PUSHOVER_TOKEN = os.environ.get("MANGARR_PUSHOVER_TOKEN")
PUSHOVER_USER = os.environ.get("MANGARR_PUSHOVER_USER")
WEBHOOK_URL = os.environ.get("MANGARR_WEBHOOK_URL")

# Sources that list chapters but cannot deliver images from this network, or
# that rate-limit so hard under bulk load that they are worthless for a
# backfill. They are neither searched nor downloaded from (Settings -> Sources).
# Names are Suwayomi display names, lower-cased.
UNUSABLE_SOURCES = {
    s.strip().lower() for s in os.environ.get(
        "MANGARR_UNUSABLE_SOURCES",
        "comick (unoriginal) (en),mangakakalot (en),readcomiconline (en)").split(",") if s.strip()}

# Sources that work but throttle per request. They lose every close call and
# get the smallest batches, but chapters nobody else has are still taken.
THROTTLED_SOURCES = {
    s.strip().lower() for s in os.environ.get(
        "MANGARR_THROTTLED_SOURCES", "").split(",") if s.strip()}

# If the highest chapter number seen by one source is more than this factor
# above what the series should have (AniList count, or the median of the other
# accepted sources), that source has merged another series under the same
# title and its chapters are not trusted.
DISAGREE = 1.5

# A fractional chapter (1.1, 29.5) is probed before it is wanted. Real
# chapters run 20-130 pages; Manganato and Bato list dozens of 1-5 page
# notice images as "Chapter 1.1", "Chapter 2.3" and so on.
MIN_PAGES = 8

# Download pacing per source: how many chapters to queue in Suwayomi at once,
# and how long to keep backing off (doubling from 60s) before giving up on a
# source - shorter when another source can supply the same chapters.
BATCH_DEFAULT = 4
BATCH_THROTTLED = 1
BACKOFF_MAX = 300
BACKOFF_MAX_WITH_FALLBACK = 60

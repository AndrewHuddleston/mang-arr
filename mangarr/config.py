"""Runtime settings. Environment variables override the defaults."""
import os

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
# minutes after launch.
REFRESH_HOURS = float(os.environ.get("MANGARR_REFRESH_HOURS", "6"))
FIRST_REFRESH_MIN = float(os.environ.get("MANGARR_FIRST_REFRESH_MIN", "5"))

# Notifications: Pushover, and/or a generic JSON webhook.
PUSHOVER_TOKEN = os.environ.get("MANGARR_PUSHOVER_TOKEN")
PUSHOVER_USER = os.environ.get("MANGARR_PUSHOVER_USER")
WEBHOOK_URL = os.environ.get("MANGARR_WEBHOOK_URL")

# Sources that list chapters but cannot deliver images from this network, or
# that rate-limit so hard under bulk load that they are worthless for a
# backfill. They are still searched, so a series that exists only there is
# reported as "exists, unreachable" rather than "not found", but no chapter is
# ever assigned to them. Names are Suwayomi display names, lower-cased.
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

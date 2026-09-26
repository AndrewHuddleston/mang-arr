"""Runtime settings. Environment variables override the defaults."""
import os

SUWAYOMI_URL = os.environ.get("MANGARR_SUWAYOMI_URL", "http://localhost:4567")
ANILIST_URL = "https://graphql.anilist.co"
DB_PATH = os.environ.get("MANGARR_DB", "/var/lib/mangarr/mangarr.db")

# Sources that list chapters but cannot deliver images from this network, or
# that rate-limit so hard under bulk load that they are worthless for a
# backfill. They are still searched, so a series that exists only there is
# reported as "exists, unreachable" rather than "not found", but no chapter is
# ever assigned to them. Names are Suwayomi display names, lower-cased.
UNUSABLE_SOURCES = {
    "comick (unoriginal) (en)",   # 429s on every image under bulk load
    "mangakakalot (en)",          # mangakakalot.gg is filtered upstream
    "readcomiconline (en)",       # does not resolve at all
}

# Sources that work but throttle per request. They lose every close call and
# get the smallest batches, but chapters nobody else has are still taken.
THROTTLED_SOURCES = {
    "manganato (en)",
}

# If the highest chapter number seen by one source is more than this factor
# above what the series should have (AniList count, or the median of the other
# accepted sources), that source has merged another series under the same
# title and its chapters are not trusted.
DISAGREE = 1.5

# A fractional chapter (1.1, 29.5) that only one source lists is probed before
# it is wanted. Real chapters run 20-130 pages; Manganato lists dozens of
# 1-5 page notice images as "Chapter 1.1", "Chapter 2.3" and so on.
MIN_PAGES = 8

# Download pacing per source: how many chapters to queue in Suwayomi at once.
BATCH_DEFAULT = 4
BATCH_THROTTLED = 1

"""Runtime settings. Environment variables override the defaults."""
import os

USER_AGENT = "mang-arr/0.1 (+https://github.com/AndrewHuddleston/mang-arr)"

SUWAYOMI_URL = os.environ.get("MANGARR_SUWAYOMI_URL", "http://localhost:4567")
ANILIST_URL = "https://graphql.anilist.co"
MANGADEX_URL = "https://api.mangadex.org"
DB_PATH = os.environ.get("MANGARR_DB", "/var/lib/mangarr/mangarr.db")

# Where Suwayomi writes chapters: <STAGING_ROOT>/<Source>/<Series>/*.cbz.
# Never renamed - Suwayomi treats it as its record of what is downloaded.
STAGING_ROOT = os.environ.get("MANGARR_STAGING", "/mnt/movie_silo/books/manga")
# The clean per-series tree Komga reads: <LIBRARY_ROOT>/<Series>/Chapter 012.0.cbz
LIBRARY_ROOT = os.environ.get("MANGARR_LIBRARY", "/mnt/movie_silo/books/library")

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
        "MANGARR_THROTTLED_SOURCES", "manganato (en)").split(",") if s.strip()}

# If the highest chapter number seen by one source is more than this factor
# above what the series should have (AniList count, or the median of the other
# accepted sources), that source has merged another series under the same
# title and its chapters are not trusted.
DISAGREE = 1.5

# A fractional chapter (1.1, 29.5) is probed before it is wanted. Real
# chapters run 20-130 pages; Manganato and Bato list dozens of 1-5 page
# notice images as "Chapter 1.1", "Chapter 2.3" and so on.
MIN_PAGES = 8

# Download pacing per source: how many chapters to queue in Suwayomi at once.
BATCH_DEFAULT = 4
BATCH_THROTTLED = 1

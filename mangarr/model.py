"""The one thing every module agrees on: what a series is."""
import logging
import re
from dataclasses import dataclass, field

from .matching import oneline

log = logging.getLogger(__name__)

REF_RE = re.compile(r"^(anilist:\d{1,9}|mangadex:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                    r"|manual:.{1,300})$")
_LATIN = re.compile(r"[A-Za-z]")

# Aliases are typed on the Add page or sent to the API, or come from a
# provider (AniList lists 20-40 synonyms for some series), and every one is a
# title that matching works through: a series keeps at most this many ...
MAX_ALIASES = 50
MAX_ALIAS_LEN = 300     # ... of at most this many characters (the manual-ref limit)


@dataclass
class Series:
    anilist_id: int | None = None       # AniList media id
    mangadex_id: str | None = None      # MangaDex uuid
    romaji: str | None = None
    english: str | None = None
    native: str | None = None
    synonyms: list[str] = field(default_factory=list)
    format: str | None = None           # MANGA, ONE_SHOT, NOVEL ...
    country: str | None = None          # JP, KR, CN, US ...
    status: str | None = None           # RELEASING, FINISHED, HIATUS, CANCELLED
    chapters: int | None = None         # total, when the series is finished and known
    volumes: int | None = None
    adult: bool = False
    popularity: int = 0
    cover: str | None = None
    description: str | None = None
    authors: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    year: int | None = None
    demographic: str | None = None      # shounen, shoujo, seinen, josei (when known)

    def __post_init__(self):
        # every way a Series is made (Add form, API, import lists, provider
        # records, a stored row) passes through here, so the cap holds for all
        self.synonyms = cap_aliases(self.synonyms, self.title)

    @property
    def ref(self) -> str:
        """Stable identity string: anilist:123, mangadex:uuid, manual:Title."""
        if self.anilist_id is not None:
            return f"anilist:{self.anilist_id}"
        if self.mangadex_id:
            return f"mangadex:{self.mangadex_id}"
        return f"manual:{self.title}"

    @property
    def manual(self) -> bool:
        return self.anilist_id is None and not self.mangadex_id

    @property
    def title(self) -> str:
        return self.english or self.romaji or self.native or "?"

    @property
    def titles(self) -> list[str]:
        """Every name the series is known by, best first, no duplicates."""
        out, seen = [], set()
        for t in [self.romaji, self.english, self.native, *self.synonyms]:
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out

    @property
    def search_titles(self) -> list[str]:
        """Titles worth typing into a source search: Latin script ones first;
        the native title last, as only MangaDex-style sources index it.
        One pass over the titles: linear in their number."""
        latin, other = [], []
        for t in self.titles:
            (latin if _LATIN.search(t) else other).append(t)
        return latin + other

    @property
    def language(self) -> str | None:
        """Original language, from the country of origin."""
        return {"JP": "Japanese", "KR": "Korean", "CN": "Chinese", "TW": "Chinese", "US": "English",
                "GB": "English", "FR": "French", "ES": "Spanish"}.get(self.country or "")

    @property
    def kind(self) -> str:
        """What people call it: manga, manhwa, manhua, webtoon, comic."""
        if self.format == "ONE_SHOT":
            return "one-shot"
        return {"JP": "manga", "KR": "manhwa", "CN": "manhua", "TW": "manhua"}.get(self.country or "", "comic")

    @property
    def right_to_left(self) -> bool:
        """Japanese manga reads right-to-left; webtoons/manhwa do not."""
        return self.country == "JP" and self.format != "WEBTOON"


def cap_aliases(aliases, series_title: str = "?") -> list[str]:
    """The aliases worth keeping, in their order: stripped, each once (case
    ignored), none longer than MAX_ALIAS_LEN, at most MAX_ALIASES. The rest
    are dropped without an error - a long synonym list is not the user's
    mistake - and logged at DEBUG. Linear in the input."""
    out: list[str] = []
    seen: set[str] = set()
    too_long = surplus = 0
    for a in aliases or []:
        if not isinstance(a, str):
            continue
        a = a.strip()
        if not a:
            continue
        if len(a) > MAX_ALIAS_LEN:
            too_long += 1
            continue
        key = a.lower()
        if key in seen:
            continue
        seen.add(key)
        if len(out) >= MAX_ALIASES:
            surplus += 1
            continue
        out.append(a)
    if too_long or surplus:
        log.debug("%s: kept %d alias(es); dropped %d longer than %d characters and %d over the cap of %d",
                  oneline(series_title, 80), len(out), too_long, MAX_ALIAS_LEN, surplus, MAX_ALIASES)
    return out


def manual(title: str, *aliases: str) -> Series:
    """A series no database has (some Western webtoons). Its identity is the
    typed title; strict matching still applies to source hits."""
    return Series(english=title.strip(), synonyms=list(aliases))


def valid_ref(ref: str | None) -> bool:
    return bool(ref) and bool(REF_RE.match(ref))

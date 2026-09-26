"""The one thing every module agrees on: what a series is."""
import re
from dataclasses import dataclass, field

REF_RE = re.compile(r"^(anilist:\d{1,9}|mangadex:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                    r"|manual:.{1,300})$")


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
        the native title last, as only MangaDex-style sources index it."""
        latin = [t for t in self.titles if re.search(r"[A-Za-z]", t)]
        other = [t for t in self.titles if t not in latin]
        return latin + other

    @property
    def right_to_left(self) -> bool:
        """Japanese manga reads right-to-left; webtoons/manhwa do not."""
        return self.country == "JP" and self.format != "WEBTOON"


def manual(title: str, *aliases: str) -> Series:
    """A series no database has (some Western webtoons). Its identity is the
    typed title; strict matching still applies to source hits."""
    return Series(english=title.strip(), synonyms=[a.strip() for a in aliases if a.strip()])


def valid_ref(ref: str | None) -> bool:
    return bool(ref) and bool(REF_RE.match(ref))

"""Series identity: find the one database record a typed title means.

AniList first, MangaDex when AniList has no exact match. A pick is only
confident when exactly one candidate matches the query exactly (or one
exact candidate clearly leads on popularity); otherwise the caller gets the
candidates and asks the user.

Fallbacks, in order, all still requiring an exact title match at the end:
  * novels are never candidates for a comic
  * a trailing '(Author)' disambiguator is dropped for the search and then
    used to pick between same-titled series by author
  * very long titles are searched by their first words (the databases'
    fuzzy search gives up on 15-word titles)
"""
from . import anilist, mangadex, model
from .matching import disambiguator, name_tokens, query_score, strip_disambiguator
from .model import Series

_NOVEL = {"NOVEL", "LIGHT_NOVEL"}
_LONG = 8          # words; beyond this, search with a prefix


def _exact(cands: list[Series], query: str) -> list[Series]:
    return [s for s in cands if s.format not in _NOVEL
            and any(query_score(t, query) == 0 for t in s.titles)]


def _pick(cands: list[Series], query: str, author_hint: str | None) -> Series | None:
    ex = _exact(cands, query)
    if not ex:
        return None
    if len(ex) == 1:
        return ex[0]
    if author_hint:
        hint = name_tokens(author_hint)
        by_author = [s for s in ex if any(hint & name_tokens(a) for a in s.authors)]
        if len(by_author) == 1:
            return by_author[0]
    ex.sort(key=lambda s: -s.popularity)
    if ex[0].popularity >= 5 * max(1, ex[1].popularity):
        return ex[0]
    return None


def _safe(fn, query):
    try:
        return fn(query)
    except RuntimeError:
        return []


def _search_all(query: str) -> tuple[list[Series], list[Series]]:
    return _safe(anilist.search, query), _safe(mangadex.search, query)


def lookup(query: str) -> tuple[Series | None, list[Series]]:
    """(confident pick or None, candidates shown to the user)."""
    query = query.strip()
    hint = disambiguator(query)
    base = strip_disambiguator(query) if hint else query
    queries = [query]
    if base != query:
        queries.append(base)
    words = base.split()
    if len(words) > _LONG:
        queries.append(" ".join(words[:_LONG]))

    seen: dict[str, Series] = {}
    for q in queries:
        al = _safe(anilist.search, q)
        for s in al:
            seen.setdefault(s.ref, s)
        for target in (query, base):
            pick = _pick(al, target, hint)
            if pick:
                return pick, list(seen.values())
        md = _safe(mangadex.search, q)
        for s in md:
            seen.setdefault(s.ref, s)
        for target in (query, base):
            pick = _pick(md, target, hint)
            if pick:
                return pick, list(seen.values())
    cands = list(seen.values())
    cands.sort(key=lambda s: (min(query_score(t, base) for t in s.titles) if s.titles else 9,
                              -s.popularity))
    return None, cands


def by_ref(ref: str) -> Series | None:
    """anilist:123 | mangadex:uuid | manual:Title"""
    kind, _, value = ref.partition(":")
    if kind == "anilist":
        return anilist.by_id(int(value))
    if kind == "mangadex":
        return mangadex.by_id(value)
    if kind == "manual":
        return model.manual(value)
    raise ValueError(f"unknown series reference {ref!r}")

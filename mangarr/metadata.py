"""Series identity: find the one database record a typed title means.

AniList first, MangaDex when AniList has no exact match. A pick is only
confident when exactly one candidate matches the query exactly (or one
exact candidate clearly leads on popularity); otherwise the caller gets the
candidates and asks the user.
"""
from . import anilist, mangadex, model
from .matching import query_score
from .model import Series


def _exact(cands: list[Series], query: str) -> list[Series]:
    return [s for s in cands if any(query_score(t, query) == 0 for t in s.titles)]


def lookup(query: str) -> tuple[Series | None, list[Series]]:
    """(confident pick or None, candidates shown to the user)."""
    al = _safe(anilist.search, query)
    ex = _exact(al, query)
    if len(ex) == 1:
        return ex[0], al
    if len(ex) > 1:
        ex.sort(key=lambda s: -s.popularity)
        if ex[0].popularity >= 5 * max(1, ex[1].popularity):
            return ex[0], al
        return None, al
    md = _safe(mangadex.search, query)
    ex = _exact(md, query)
    if len(ex) == 1:
        return ex[0], md + al
    return None, (al + md)


def _safe(fn, query):
    try:
        return fn(query)
    except RuntimeError:
        return []


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

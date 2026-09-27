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
import logging

from . import anilist, mangadex, model
from .matching import MAX_TITLE, disambiguator, name_tokens, oneline, plain_quotes, query_score, strip_disambiguator
from .model import Series

log = logging.getLogger(__name__)

_NOVEL = {"NOVEL", "LIGHT_NOVEL"}
_LONG = 8          # words; beyond this, search with a prefix


class LookupError_(RuntimeError):
    """A metadata provider could not be used (network, bad id)."""


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


def _safe(provider: str, fn, query, failed: list[str]):
    """A provider failure is logged, noted in `failed` and treated as 'no
    candidates from it'; the other provider still gets a chance. The query
    is logged only at DEBUG: it may be a line of an import list URL's
    answer, and the log is readable on the System page."""
    try:
        return fn(query)
    except Exception as e:
        log.warning("%s lookup failed: %s: %s", provider, type(e).__name__, oneline(e, 300))
        log.debug("%s lookup that failed was for %r", provider, query[:200])
        failed.append(provider)
        return []


def lookup(query: str, unreached: list[str] | None = None) -> tuple[Series | None, list[Series]]:
    """(confident pick or None, candidates shown to the user). The query is
    typed, or a line of an import list: anything past MAX_TITLE characters
    is cut (logged) before it is matched or sent to the providers.

    Raises LookupError_ when no pick was found and no provider answered at
    all: "no candidates" would be untrue. When only one of them did not
    answer, the other's candidates are returned, and a caller that must not
    take the title for one neither knows (an import list deciding whether
    its lines are titles) passes a list as `unreached`: the names of the
    providers that were not asked are added to it."""
    query = (query or "").strip()
    if len(query) > MAX_TITLE:
        log.info("lookup query cut to %d characters: %s", MAX_TITLE, oneline(query, 80))
        query = query[:MAX_TITLE].strip()
    query = plain_quotes(query)       # the providers' search misses "It’s Mine", finds "It's Mine"
    hint = disambiguator(query)
    base = strip_disambiguator(query) if hint else query
    queries = [query]
    if base != query:
        queries.append(base)
    words = base.split()
    if len(words) > _LONG:
        queries.append(" ".join(words[:_LONG]))

    seen: dict[str, Series] = {}
    failed: list[str] = []
    asked = 0
    for q in queries:
        asked += 1
        al = _safe("AniList", anilist.search, q, failed)
        for s in al:
            seen.setdefault(s.ref, s)
        for target in (query, base):
            pick = _pick(al, target, hint)
            if pick:
                return pick, list(seen.values())
        asked += 1
        md = _safe("MangaDex", mangadex.search, q, failed)
        for s in md:
            seen.setdefault(s.ref, s)
        for target in (query, base):
            pick = _pick(md, target, hint)
            if pick:
                return pick, list(seen.values())
    if failed and len(failed) == asked:
        raise LookupError_(f"{' and '.join(dict.fromkeys(failed))} could not be reached")
    if unreached is not None:
        unreached.extend(p for p in dict.fromkeys(failed) if p not in unreached)
    cands = list(seen.values())
    cands.sort(key=lambda s: (min(query_score(t, base) for t in s.titles) if s.titles else 9,
                              -s.popularity))
    return None, cands


def by_ref(ref: str) -> Series | None:
    """anilist:123 | mangadex:uuid | manual:Title. Raises ValueError for a
    malformed ref and LookupError_ when the provider cannot be reached."""
    if not model.valid_ref(ref):
        raise ValueError(f"not a series reference: {ref!r}")
    kind, _, value = ref.partition(":")
    try:
        if kind == "anilist":
            return anilist.by_id(int(value))
        if kind == "mangadex":
            return mangadex.by_id(value)
    except Exception as e:
        raise LookupError_(f"{kind} lookup for {value} failed: {type(e).__name__}: {e}") from e
    return model.manual(value)


def statuses(refs: list[str]) -> dict[str, tuple[str | None, int | None]]:
    """(status, chapter count) as the provider has them now, per series
    reference: every AniList one in a query per 50, every MangaDex one in a
    request per 100, no source searched. Manual series have no provider and
    are left out; so are the series of a provider that cannot be reached
    (logged): the caller keeps what it stored."""
    out: dict[str, tuple[str | None, int | None]] = {}
    kinds: dict[str, list[str]] = {}
    for ref in refs:
        if model.valid_ref(ref):
            kind, _, value = ref.partition(":")
            kinds.setdefault(kind, []).append(value)
    for kind, lookup, key in (("anilist", lambda v: anilist.statuses([int(x) for x in v]), int),
                              ("mangadex", mangadex.statuses, str)):
        values = kinds.get(kind)
        if not values:
            continue
        try:
            found = lookup(values)
        except Exception as e:
            log.warning("could not check the status of %d series on %s: %s: %s", len(values),
                        "AniList" if kind == "anilist" else "MangaDex", type(e).__name__, oneline(str(e), 200))
            continue
        for v in values:
            if key(v) in found:
                out[f"{kind}:{v}"] = found[key(v)]
    return out

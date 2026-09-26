"""Download a plan through Suwayomi, one source at a time, paced per source.

Suwayomi has a single global download queue, so sources are worked
sequentially. Within a source the batch size adapts: it shrinks to 1 and backs
off when the source throttles, and grows back when downloads succeed. A source
that keeps failing is abandoned for this run; its chapters stay wanted.
"""
import time

from . import config
from .resolver import Plan, SourceMatch
from .suwayomi import Client


def download(client: Client, plan: Plan, log=print, only: set[float] | None = None) -> dict:
    """Returns {chapter_number: 'ok' | 'failed'} for every chapter attempted."""
    wanted = set(plan.wanted()) if only is None else set(only)
    by_source: dict[int, list[tuple[float, SourceMatch]]] = {}
    for n in sorted(wanted):
        m = plan.assignment.get(n)
        if m is not None:
            by_source.setdefault(m.manga_id, []).append((n, m))
    results: dict[float, str] = {}
    for manga_id, items in by_source.items():
        m = items[0][1]
        chapters = {c.number: c for c in m.chapters}
        todo = [chapters[n] for n, _ in items if n in chapters]
        batch = config.BATCH_THROTTLED if m.source.throttled else config.BATCH_DEFAULT
        log(f"  {m.source.name}: {len(todo)} chapter(s) [{_ranges([c.number for c in todo])}]")
        ok, failed = _download_source(client, manga_id, todo, batch, log)
        for n in ok:
            results[n] = "ok"
        for n in failed:
            results[n] = "failed"
    return results


def _download_source(client, manga_id, todo, batch, log):
    ok, failed = [], []
    i, size, backoff = 0, batch, 0
    while i < len(todo):
        chunk = todo[i:i + size]
        client.clear()
        client.enqueue([c.id for c in chunk])
        client.start()
        stalled = _wait(client)
        have = client.downloaded_ids(manga_id)
        got = [c for c in chunk if c.id in have]
        if stalled and not got:
            size = 1
            backoff = min(300, (backoff or 30) * 2)
            log(f"    throttled at ch {chunk[0].number:g} - waiting {backoff}s")
            time.sleep(backoff)
            if backoff >= 300:
                rest = todo[i:]
                log(f"    giving up on this source at ch {chunk[0].number:g}"
                    f" ({len(ok)} done, {len(rest)} left wanted)")
                failed.extend(c.number for c in rest)
                break
            continue
        ok.extend(c.number for c in got)
        failed.extend(c.number for c in chunk if c.id not in have)
        backoff = 0
        if len(got) == len(chunk) and size < batch:
            size = min(batch, size * 2)
        i += len(chunk)
        log(f"    {len(ok)}/{len(todo)} done" + (f", {len(failed)} failed" if failed else ""))
        time.sleep(2)
    client.stop()
    return ok, failed


def _wait(client, polls: int = 120, every: int = 5) -> bool:
    """Block until the queue drains. True if every item errored out."""
    for _ in range(polls):
        time.sleep(every)
        q = client.queue()
        if not q:
            return False
        if all(x["state"] == "ERROR" and x["tries"] >= 3 for x in q):
            return True
    return False


def _ranges(nums):
    from .resolver import ranges
    return ranges(nums)

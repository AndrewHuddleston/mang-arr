"""One conversion in a process of its own: python -m mangarr.convert.worker.

The server starts it for every chapter (conversions.py), so a page that
makes the image decoder use too much memory, hang or crash takes this
process down and not the server. Its limits are set before Pillow is
loaded. It is told what to do in one JSON document on standard input:

    {"src": path, "out": path, "options": {...}, "rtl": bool | null, "webtoon": bool | null,
     "hints": {...}, "meta": {...}, "threads": 2, "memory_mb": 1536, "cpu_secs": 1260,
     "max_output_mb": 2048}

and answers on standard output with JSON lines: {"progress": [done, total]}
at most once a second, then {"ok": true, ...the Result} or
{"ok": false, "error": "..."}.

Exit codes: 0 done, 2 the chapter cannot be converted (trying again will
not help), 3 out of memory or another limit, 4 the output could not be
written (disk full), 1 anything else.
"""
import errno
import json
import os
import sys
import time

MAX_INPUT = 64 * 1024
EXIT_OK, EXIT_ERROR, EXIT_BAD_INPUT, EXIT_LIMIT, EXIT_WRITE = 0, 1, 2, 3, 4


def _limits(job: dict) -> None:
    try:
        import resource
    except ImportError:                 # not a Unix: no limits to set
        return
    mb = 1 << 20
    for what, value in ((resource.RLIMIT_AS, int(job.get("memory_mb") or 0) * mb),
                        (resource.RLIMIT_FSIZE, int(job.get("max_output_mb") or 0) * mb),
                        (resource.RLIMIT_CPU, int(job.get("cpu_secs") or 0))):
        if value > 0:
            try:
                soft, hard = resource.getrlimit(what)
                cap = value if hard == resource.RLIM_INFINITY else min(value, hard)
                resource.setrlimit(what, (cap, hard))
            except (ValueError, OSError):
                pass                    # a limit the system will not take is left as it is
    try:
        os.nice(10)
    except OSError:
        pass


def _say(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    try:
        raw = sys.stdin.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("the job is too large")
        job = json.loads(raw)
        if not isinstance(job, dict):
            raise ValueError("the job is not an object")
    except (ValueError, OSError) as e:
        _say({"ok": False, "error": f"the job could not be read: {e}"})
        return EXIT_ERROR
    _limits(job)
    from . import ConvertError, Options, convert_chapter

    last = [0.0]

    def progress(done: int, total: int) -> None:
        now = time.monotonic()
        if now - last[0] >= 1.0 or done == total:
            last[0] = now
            _say({"progress": [done, total]})
    try:
        options = Options.from_dict(job.get("options") or {})
        with open(job["src"], "rb") as src, open(job["out"], "r+b") as out:
            r = convert_chapter(src, out, options, rtl=job.get("rtl"), webtoon=job.get("webtoon"),
                                hints=job.get("hints"), meta=job.get("meta"), threads=int(job.get("threads") or 1),
                                progress=progress)
            out.flush()
            os.fsync(out.fileno())
        _say({"ok": True, "format": r.format, "profile": r.profile, "pages": r.pages, "bytes": r.bytes,
              "seconds": r.seconds, "rtl": r.rtl, "webtoon": r.webtoon, "decided": r.decided, "engine": r.engine})
        return EXIT_OK
    except ConvertError as e:
        _say({"ok": False, "error": str(e)[:500]})
        return EXIT_BAD_INPUT
    except MemoryError:
        _say({"ok": False, "error": "out of memory"})
        return EXIT_LIMIT
    except OSError as e:
        full = e.errno in (errno.ENOSPC, errno.EDQUOT, errno.EFBIG)
        _say({"ok": False, "error": f"{'the output could not be written' if full else 'a file could not be read'}: "
                                    f"{e.strerror or e}"[:500]})
        return EXIT_WRITE if full else EXIT_ERROR
    except (ValueError, KeyError, TypeError, RuntimeError) as e:
        _say({"ok": False, "error": f"{type(e).__name__}: {e}"[:500]})
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

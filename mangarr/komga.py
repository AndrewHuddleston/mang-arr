"""Komga: ask it to rescan after chapters land, so new files show up in
minutes instead of at its next scheduled scan; and, before library files are
renamed, whether Komga will keep their reading progress (library_settings,
series_books). Optional: needs komga_url and komga_api_key in Settings
(Komga: account menu -> API keys)."""
import json
import logging
import re
import unicodedata
import urllib.error
import urllib.parse
from collections.abc import Iterator

from . import outbound, settings

log = logging.getLogger(__name__)


def configured() -> bool:
    v = settings.all_values()
    return bool(v["komga_url"] and v["komga_api_key"])


MAX_BYTES = 8 << 20      # a library list is a few KB; never read an endless answer into memory


def _call(method: str, path: str, timeout: int = 20):
    """(status, parsed JSON). Only http(s) komga_url values are used, the
    request is capped at `timeout` seconds in total, and a redirect is not
    followed (it would carry the API key to another host, and turn the scan
    POST into a GET): it is reported as an HTTP error naming the new URL."""
    v = settings.all_values()
    url = v["komga_url"].rstrip("/") + path
    status, body = outbound.fetch(url, method=method, headers={"X-API-Key": v["komga_api_key"],
                                                               "Accept": "application/json"},
                                  timeout=timeout, max_bytes=MAX_BYTES, what="the Komga URL")
    return status, (json.loads(body) if body else None)


def libraries() -> list[dict]:
    status, data = _call("GET", "/api/v1/libraries")
    return data or []


def library_settings(library_id: str) -> dict:
    """The settings of one Komga library that a rename depends on: {"id",
    "name", "root", "hash_files", "empty_trash_after_scan"}. hash_files is
    Libraries -> Edit -> Options -> Compute hash for files: with it on, Komga
    recognises a renamed file by its hash and keeps the book (its id, reading
    progress and metadata); with it off, a renamed file is a new book.
    Raises on HTTP and connection errors, and ValueError when the answer is
    not a library."""
    _, data = _call("GET", f"/api/v1/libraries/{_quote(library_id)}")
    if not isinstance(data, dict) or not data.get("id"):
        raise ValueError(f"Komga did not return library {library_id!r}")
    return {"id": str(data["id"]), "name": str(data.get("name") or ""), "root": str(data.get("root") or ""),
            "hash_files": data.get("hashFiles") is True,
            "empty_trash_after_scan": data.get("emptyTrashAfterScan") is True}


class AmbiguousSeries(ValueError):
    """More than one Komga series is in a folder of this name: in several
    libraries, or in sub-folders of one. .library_ids: the libraries they
    are in, sorted."""

    def __init__(self, message: str, library_ids):
        super().__init__(message)
        self.library_ids = sorted(library_ids)


def series_books(folder: str, library_id: str | None = None) -> dict | None:
    """The Komga series in the library folder named `folder` (a folder
    name, not a path: Komga may see the library under another path) and
    its books: {"series_id", "library_id", "name", "books": [{"id", "name",
    "url", "file_hash"}]}, or None when Komga has no such series (not
    scanned yet). file_hash is "" until Komga has hashed the file. Series
    and books in Komga's trash are left out. library_id is the library to
    look in; without one every library is searched. When several series
    have the folder name, the one directly in its library's root folder
    (where mang-arr's series folders are) is it; AmbiguousSeries is raised
    when that is not exactly one. Raises on HTTP and connection errors."""
    want = unicodedata.normalize("NFC", folder)
    params = {"deleted": "false", **({"library_id": library_id} if library_id else {})}
    found = [s for s in _pages("/api/v1/series", params)
             if not s.get("deleted") and _folder_name(s.get("url")) == want
             and (not library_id or s.get("libraryId") == library_id)]
    if not found:
        return None
    if len(found) > 1:
        libs = {str(s.get("libraryId")) for s in found}
        roots = {lib: _path(library_settings(lib)["root"]) for lib in sorted(libs)}
        direct = [s for s in found if _parent(s.get("url")) == roots[str(s.get("libraryId"))]]
        if len(direct) != 1:
            raise AmbiguousSeries(f"Komga has {len(found)} series in folders named {folder!r} (libraries "
                                  f"{', '.join(sorted(libs))})", libs)
        found = direct
    s = found[0]
    books = [{"id": str(b.get("id") or ""), "name": str(b.get("name") or ""), "url": str(b.get("url") or ""),
              "file_hash": str(b.get("fileHash") or "")}
             for b in _pages(f"/api/v1/series/{_quote(str(s.get('id')))}/books", {"deleted": "false"})
             if not b.get("deleted")]
    return {"series_id": str(s.get("id")), "library_id": str(s.get("libraryId") or library_id or ""),
            "name": str(s.get("name") or ""), "books": books}


PAGE_SIZE = 200         # series or books per request: a page of series is a few hundred KB
MAX_PAGES = 1000        # a Komga that keeps answering "there is more" is not followed for ever


def _pages(path: str, params: dict) -> Iterator[dict]:
    """Every item of a paged Komga listing, a page at a time."""
    for page in range(MAX_PAGES):
        _, data = _call("GET", f"{path}?{urllib.parse.urlencode({**params, 'page': page, 'size': PAGE_SIZE})}")
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise ValueError(f"Komga answered {path} without a page of results")
        yield from (item for item in data["content"] if isinstance(item, dict))
        if data.get("last", True) or not data["content"]:
            return
    raise ValueError(f"Komga listed more than {MAX_PAGES} pages of {path}")


def _folder_name(url) -> str | None:
    """The last part of a path as Komga shows it (/library/Berserk, or
    D:\\Manga\\Berserk on Windows), NFC."""
    if not isinstance(url, str) or not url.strip("/\\"):
        return None
    return unicodedata.normalize("NFC", re.split(r"[\\/]", url.rstrip("/\\"))[-1])


def _path(path) -> str | None:
    """A path as Komga shows it, for comparing: NFC, '/' separators, no
    trailing separator."""
    if not isinstance(path, str):
        return None
    return unicodedata.normalize("NFC", path.replace("\\", "/").rstrip("/"))


def _parent(url) -> str | None:
    """The folder a series folder is in (/library for /library/Berserk)."""
    p = _path(url)
    return p.rpartition("/")[0] if p else None


def _quote(part: str) -> str:
    return urllib.parse.quote(part, safe="")


def scan(library_id: str | None = None) -> bool:
    """Trigger a scan of one library (or every library). True on success."""
    if not configured():
        log.debug("komga scan skipped: not configured")
        return False
    v = settings.all_values()
    ids = [library_id or v["komga_library_id"]] if (library_id or v["komga_library_id"]) else None
    try:
        if ids is None:
            ids = [lib["id"] for lib in libraries()]
        for lid in ids:
            _call("POST", f"/api/v1/libraries/{lid}/scan")
        log.info("komga: scan requested for %d librar%s", len(ids), "y" if len(ids) == 1 else "ies")
        return True
    except urllib.error.HTTPError as e:
        log.error("komga scan failed: HTTP %d %s (check the API key and URL)", e.code, e.reason)
    except Exception as e:
        log.error("komga scan failed: %s: %s", type(e).__name__, e)
    return False


def test() -> tuple[bool, str]:
    try:
        libs = libraries()
        return True, f"ok: {len(libs)} librar{'y' if len(libs) == 1 else 'ies'}: " + \
            ", ".join(f"{lib['name']} ({lib['id']})" for lib in libs)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} {e.reason}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

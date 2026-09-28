"""A fake Komga for tests: it answers the calls komga.py makes through
komga._call (libraries, and paged series and book listings), so nothing
reaches a real Komga. Use: mock.patch.object(komga, "_call", fake.call)."""
import urllib.error
import urllib.parse


class FakeKomga:
    def __init__(self):
        self.libraries = {"LIB1": {"id": "LIB1", "name": "Manga (mang-arr)", "root": "/library",
                                   "hashFiles": True, "emptyTrashAfterScan": False}}
        self.series: list[dict] = []
        self.books: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail: Exception | None = None      # raised on every call when set
        self.endless = False                    # every page says there is another one

    def add_library(self, lid: str, name: str, hash_files: bool = True, root: str = "/other") -> None:
        self.libraries[lid] = {"id": lid, "name": name, "root": root, "hashFiles": hash_files,
                               "emptyTrashAfterScan": False}

    def add_series(self, sid: str, folder: str, books, library: str = "LIB1", root: str = "/library",
                   hashed=True, deleted: bool = False) -> None:
        """books: file names; hashed: True, False, or one bool per book."""
        self.series.append({"id": sid, "libraryId": library, "name": folder, "url": f"{root}/{folder}",
                            "deleted": deleted})
        flags = hashed if isinstance(hashed, list) else [hashed] * len(books)
        self.books[sid] = [{"id": f"{sid}-{i}", "seriesId": sid, "name": b.rsplit(".", 1)[0],
                            "url": f"{root}/{folder}/{b}", "fileHash": f"hash{i}" if h else "", "deleted": False}
                           for i, (b, h) in enumerate(zip(books, flags, strict=True))]

    def call(self, method: str, path: str, timeout: int = 20):
        self.calls.append((method, path))
        if self.fail is not None:
            raise self.fail
        url = urllib.parse.urlsplit(path)
        q = urllib.parse.parse_qs(url.query)
        parts = [urllib.parse.unquote(p) for p in url.path.strip("/").split("/")]
        if method != "GET" or parts[:2] != ["api", "v1"]:
            raise AssertionError(f"unexpected Komga call {method} {path}")
        if parts[2:3] == ["libraries"] and len(parts) == 4:
            if parts[3] not in self.libraries:
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            return 200, dict(self.libraries[parts[3]])
        if parts[2:] == ["series"]:
            found = [s for s in self.series if "library_id" not in q or s["libraryId"] in q["library_id"]]
            if q.get("deleted") == ["false"]:
                found = [s for s in found if not s["deleted"]]
            return 200, self.page(found, q)
        if parts[2:3] == ["series"] and len(parts) == 5 and parts[4] == "books":
            if parts[3] not in self.books:
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            return 200, self.page(self.books[parts[3]], q)
        raise AssertionError(f"unexpected Komga call {method} {path}")

    def page(self, items: list, q: dict) -> dict:
        page, size = int(q["page"][0]), int(q["size"][0])
        if self.endless:            # the same page again and again, never the last
            return {"content": [dict(x) for x in items[:size]], "number": page, "size": size, "last": False}
        chunk = [dict(x) for x in items[page * size:(page + 1) * size]]
        last = (page + 1) * size >= len(items)
        return {"content": chunk, "number": page, "size": size, "totalElements": len(items), "last": last}

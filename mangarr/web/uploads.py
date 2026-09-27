"""Receiving a backup file for System -> Restore from file.

Starlette's form parser is not used for this route: it spools file parts to
the container's /tmp (the Docker host's system disk) with no limit on how
long a client may take. Here the multipart body is parsed as it streams in
and the one file part goes straight into a backup.UploadSpool, which lives
in the data volume and stops at the upload limit or before the disk gets
too full. A client that sends nothing for IDLE_SECS, or less on average
than MIN_RATE, is cut off, and so is one that sends more than
MAX_OTHER_BYTES besides the file's data; reading stops once the form is
complete. The route itself (app.py) lets only one upload in at a time, so
these bound how long anyone can hold it: about IDLE_SECS plus the upload
limit at MIN_RATE.
"""
import asyncio
import logging
import time

import python_multipart
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import parse_options_header
from starlette.concurrency import run_in_threadpool

from .. import backup, config

log = logging.getLogger(__name__)

# an upload that sends no bytes for IDLE_SECS is cut off, and so is one that averages fewer bytes a second
# than MIN_RATE (128 KB/s, about 1 Mbit/s); raise them for a slow link (README: Backups)
IDLE_SECS = config.env_number("MANGARR_UPLOAD_IDLE_SECS", 30.0, 5.0, 3600.0)
MIN_RATE = config.env_number("MANGARR_UPLOAD_MIN_KBPS", 128, 1, 1 << 20, integer=True) << 10
MAX_FIELDS = 8           # form fields besides the file (the form has none) ...
MAX_FIELD_BYTES = 4096   # ... and their total size, kept only to be skipped
MAX_OTHER_BYTES = 8192   # everything but the file's data: boundaries, part headers, fields, preamble
DRAIN_MAX = 8 << 20      # the rest of a refused body is read (and dropped) up to this size before answering


class UploadError(ValueError):
    """The upload was refused or cut off; the message is for the user."""


class Disconnected(Exception):
    """The client went away before the upload was complete."""


class _Form:
    """python-multipart callbacks: data of the part named `file` is queued
    for the spool, other (small) fields are skipped, anything else refused."""

    def __init__(self):
        self.filename: str | None = None
        self.pending: list[bytes] = []     # file data parsed from the last chunk, written off the event loop
        self.complete = False              # the closing boundary arrived
        self.file_bytes = 0                # of the file part's data
        self._headers: dict[bytes, bytes] = {}
        self._name = self._value = b""
        self._in_file = False
        self._fields = self._field_bytes = 0

    def callbacks(self) -> dict:
        return {"on_part_begin": self.on_part_begin, "on_header_field": self.on_header_field,
                "on_header_value": self.on_header_value, "on_header_end": self.on_header_end,
                "on_headers_finished": self.on_headers_finished, "on_part_data": self.on_part_data,
                "on_end": self.on_end}

    def on_part_begin(self) -> None:
        self._headers, self._in_file = {}, False

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def on_header_end(self) -> None:
        self._headers[self._name.lower()] = self._value
        self._name = self._value = b""

    def on_headers_finished(self) -> None:
        _, options = parse_options_header(self._headers.get(b"content-disposition", b""))
        if b"filename" in options:
            if self.filename is not None or options.get(b"name") != b"file":
                raise UploadError("send one backup file, in the form field named 'file'")
            self.filename = options[b"filename"].decode("utf-8", "replace")
            self._in_file = True
        else:
            self._fields += 1
            if self._fields > MAX_FIELDS:
                raise UploadError("the form has too many fields")

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._in_file:
            self.pending.append(data[start:end])
            self.file_bytes += end - start
            return
        self._field_bytes += end - start
        if self._field_bytes > MAX_FIELD_BYTES:
            raise UploadError("the form fields besides the file are too large")

    def on_end(self) -> None:
        self.complete = True


class Body:
    """A request body read as it arrives: `received` bytes so far, `done`
    once the last of it was read, `cut_off` once it stalled or crawled."""

    def __init__(self, request):
        self.request = request
        self.received = 0
        self.done = self.cut_off = False
        try:
            self.declared = int(request.headers.get("content-length") or -1)
        except ValueError:
            self.declared = -1

    async def chunks(self):
        """The rest of the body, chunk by chunk. UploadError when nothing
        arrives for IDLE_SECS, or when it falls behind MIN_RATE: it may take
        IDLE_SECS plus one second for every MIN_RATE bytes received (counted
        from this call), so a client that drips a byte now and then cannot
        keep the upload slot and its spool for as long as it likes.
        Disconnected when the client goes away."""
        start, got = time.monotonic(), 0
        while not self.done:
            try:
                message = await asyncio.wait_for(self.request.receive(), IDLE_SECS)
            except asyncio.TimeoutError:
                self.cut_off = True
                raise UploadError(f"the upload stalled: nothing arrived for {IDLE_SECS:g} seconds") from None
            if message["type"] == "http.disconnect":
                raise Disconnected()
            data = message.get("body", b"")
            self.received += len(data)
            got += len(data)
            self.done = not message.get("more_body", False)
            took = time.monotonic() - start
            if not self.done and took > IDLE_SECS + got / MIN_RATE:   # a complete upload is taken, however slow
                self.cut_off = True
                raise UploadError(f"the upload is too slow ({backup.size_text(got)} in {took:.0f} seconds; it has "
                                  f"to average at least {backup.size_text(MIN_RATE)} a second). Over a slow "
                                  "connection, copy the file, keeping its mangarr-<date>-<time>.db name, into the "
                                  f"backups folder ({backup.backup_dir()}) and restore it from the list instead")
            if data:
                yield data

    async def drain(self, limit: int = DRAIN_MAX) -> bool:
        """Read and drop what is left of the body when that is at most
        `limit` bytes, within the same cut-offs (not at all after one); True
        when all of it was read. Answering while a browser is still sending
        and then closing the connection makes the kernel reset it, and some
        clients (Windows) then show "connection reset" instead of the
        answer."""
        if self.done:
            return True
        if self.cut_off or (self.declared >= 0 and self.declared - self.received > limit):
            return False
        got = 0
        try:
            async for data in self.chunks():
                got += len(data)
                if got > limit:
                    return False
        except (UploadError, Disconnected):
            return False
        return self.done


async def receive(body: Body, spool: backup.UploadSpool) -> str:
    """Stream the multipart body into `spool` until the form is complete
    (whatever follows the closing boundary is not waited for); returns the
    uploaded file's name ("" when the form held no file). Raises
    UploadError, Disconnected, or what spool.write raises (RestoreError when
    the file is too big or the disk too full, OSError). The caller discards
    the spool on every failure."""
    ctype, options = parse_options_header(body.request.headers.get("content-type", ""))
    boundary = options.get(b"boundary")
    if ctype.strip().lower() != b"multipart/form-data" or not boundary:
        return ""
    form = _Form()
    parser = python_multipart.MultipartParser(boundary, form.callbacks())
    async for chunk in body.chunks():
        try:
            parser.write(chunk)
        except FormParserError as e:
            log.debug("backup upload: multipart parser: %s", e)
            raise UploadError("the upload is not a valid multipart form") from e
        if form.pending:
            data = b"".join(form.pending)
            form.pending.clear()
            await run_in_threadpool(spool.write, data)    # disk writes and free-space checks off the event loop
        if form.complete:
            break
        if body.received - form.file_bytes > MAX_OTHER_BYTES:
            raise UploadError(f"the form holds more than {backup.size_text(MAX_OTHER_BYTES)} besides the file")
    if not form.complete:
        raise UploadError("the upload ended before the file was complete")
    log.debug("backup upload received: %s, %d bytes", form.filename, spool.size)
    return form.filename or ""

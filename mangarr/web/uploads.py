"""Receiving a backup file for System -> Restore from file.

Starlette's form parser is not used for this route: it spools file parts to
the container's /tmp (the Docker host's system disk) with no limit on how
long a client may take. Here the multipart body is parsed as it streams in
and the one file part goes straight into a backup.UploadSpool, which lives
in the data volume and stops at the upload limit or before the disk gets
too full. A client that sends nothing for IDLE_SECS is cut off. The route
itself (app.py) lets only one upload in at a time.
"""
import asyncio
import logging

import python_multipart
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import parse_options_header
from starlette.concurrency import run_in_threadpool

from .. import backup

log = logging.getLogger(__name__)

IDLE_SECS = 30.0         # an upload that sends no bytes for this long is cut off
MAX_FIELDS = 8           # form fields besides the file (the form has none) ...
MAX_FIELD_BYTES = 4096   # ... and their total size, kept only to be skipped


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
            return
        self._field_bytes += end - start
        if self._field_bytes > MAX_FIELD_BYTES:
            raise UploadError("the form fields besides the file are too large")

    def on_end(self) -> None:
        self.complete = True


async def _body(request):
    """The request body as it arrives; UploadError when nothing arrives for
    IDLE_SECS, Disconnected when the client goes away."""
    while True:
        try:
            message = await asyncio.wait_for(request.receive(), IDLE_SECS)
        except asyncio.TimeoutError:
            raise UploadError(f"the upload stalled: nothing arrived for {IDLE_SECS:g} seconds") from None
        if message["type"] == "http.disconnect":
            raise Disconnected()
        if message.get("body"):
            yield message["body"]
        if not message.get("more_body", False):
            return


async def receive(request, spool: backup.UploadSpool) -> str:
    """Stream the multipart body of `request` into `spool`; returns the
    uploaded file's name ("" when the form held no file). Raises
    UploadError, Disconnected, or what spool.write raises (RestoreError when
    the file is too big or the disk too full, OSError). The caller discards
    the spool on every failure."""
    ctype, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary")
    if ctype.strip().lower() != b"multipart/form-data" or not boundary:
        return ""
    form = _Form()
    parser = python_multipart.MultipartParser(boundary, form.callbacks())
    async for chunk in _body(request):
        try:
            parser.write(chunk)
        except FormParserError as e:
            log.debug("backup upload: multipart parser: %s", e)
            raise UploadError("the upload is not a valid multipart form") from e
        if form.pending:
            data = b"".join(form.pending)
            form.pending.clear()
            await run_in_threadpool(spool.write, data)    # disk writes and free-space checks off the event loop
    if not form.complete:
        raise UploadError("the upload ended before the file was complete")
    log.debug("backup upload received: %s, %d bytes", form.filename, spool.size)
    return form.filename or ""

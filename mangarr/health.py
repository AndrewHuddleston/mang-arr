"""Health checks, Sonarr-style: a list of problems and warnings a person
can act on. Used by the System page and /api/v1/health."""
import logging
import os
import shutil
from dataclasses import dataclass

from . import config, komga, notify, settings
from .suwayomi import Client, SuwayomiError

log = logging.getLogger(__name__)


@dataclass
class Check:
    level: str        # error | warning | ok
    name: str
    detail: str


def run(client: Client) -> list[Check]:
    out: list[Check] = []
    v = settings.all_values()

    try:
        sources = client.sources()
        out.append(Check("ok", "Suwayomi", f"reachable at {config.SUWAYOMI_URL}, {len(sources)} sources"))
        enabled = [s for s in sources if not s.unusable]
        if sources and not enabled:
            out.append(Check("error", "Sources", "every source is disabled in Settings"))
        elif sources and len(enabled) < len(sources):
            out.append(Check("ok", "Sources", f"{len(enabled)} enabled, {len(sources) - len(enabled)} disabled"))
    except SuwayomiError as e:
        out.append(Check("error", "Suwayomi", f"unreachable: {e}"))

    for name, path, need_write in (("Staging", config.STAGING_ROOT, False), ("Library", config.LIBRARY_ROOT, True)):
        if not os.path.isdir(path):
            out.append(Check("error", name, f"path does not exist: {path}"))
        elif need_write and not os.access(path, os.W_OK):
            out.append(Check("error", name, f"not writable: {path}"))
        else:
            out.append(Check("ok", name, path))

    if os.path.isdir(config.STAGING_ROOT) and os.path.isdir(config.LIBRARY_ROOT):
        try:
            if os.stat(config.STAGING_ROOT).st_dev != os.stat(config.LIBRARY_ROOT).st_dev:
                out.append(Check("warning", "Hard links",
                                 "staging and library are on different filesystems: chapters are copied, "
                                 "not linked (twice the disk use)"))
        except OSError:
            pass
        try:
            usage = shutil.disk_usage(config.LIBRARY_ROOT)
            free_gb = usage.free / 1e9
            level = "error" if free_gb < 2 else "warning" if free_gb < 20 else "ok"
            out.append(Check(level, "Disk",
                             f"{free_gb:.0f} GB free of {usage.total / 1e9:.0f} GB on the library volume"))
        except OSError as e:
            out.append(Check("warning", "Disk", f"cannot read usage: {e}"))

    if not komga.configured():
        out.append(Check("warning", "Komga", "not configured: new chapters appear only at Komga's own scan interval"))
    else:
        out.append(Check("ok", "Komga", f"scan after import via {v['komga_url']}"))
    if not notify.configured():
        out.append(Check("warning", "Notifications", "none configured (Pushover or webhook in Settings)"))
    if not v["auth_user"]:
        out.append(Check("warning", "Security", "no web login set; anyone on the network can use this page"))
    return out


def problems(client: Client) -> list[str]:
    return [f"{c.name}: {c.detail}" for c in run(client) if c.level == "error"]

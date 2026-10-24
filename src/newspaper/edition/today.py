"""Today's edition as the readers see it: local date, who is generating it, recovery on open.

Kept apart from generate.py so `newspaper read` and `web` start without importing the
extraction, embedding and LLM stack.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ..config.models import NewspaperConfig
from ..storage import queries
from ..storage.database import open_db
from ..storage.models import Edition, EditionStatus, utcnow

# ponytail: a `generating` row older than this is dead even if its pid was reused;
# add a heartbeat column if a real run ever takes longer.
STALE_AFTER = timedelta(hours=6)


def local_today(config: NewspaperConfig, now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(ZoneInfo(config.general.timezone)).date()


def notice_at(config: NewspaperConfig, day: date) -> datetime | None:
    """`day`'s notice_time as an aware local datetime; None when the notice goes out with the
    edition."""
    if not config.general.notice_time:
        return None
    at = time.fromisoformat(config.general.notice_time)
    return datetime.combine(day, at, ZoneInfo(config.general.timezone))


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) would *terminate* the process on Windows; ask the kernel instead.
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def today_banner(status: str | None, error: str | None) -> str | None:
    """Notice shown by both readers about today's edition."""
    if status == EditionStatus.generating:
        return "Preparando la edición de hoy…"
    if status == EditionStatus.failed:
        return f"La edición de hoy falló: {error}. Reinténtala con «newspaper generate»."
    return None


def running(edition: Edition) -> bool:
    """Someone is generating this edition right now: recent claim, process still alive."""
    return (
        edition.status == EditionStatus.generating
        and utcnow() - edition.started_at < STALE_AFTER
        and pid_alive(edition.pid)
    )


def maybe_spawn_generate(config: NewspaperConfig, now: datetime | None = None) -> bool:
    """Recovery when a reader opens: start today's edition in the background if it is due,
    missing and nobody is working on it. A failed edition is not retried automatically."""
    local = (now or datetime.now(UTC)).astimezone(ZoneInfo(config.general.timezone))
    if local.hour < config.general.edition_hour or not config.active_feeds:
        return False
    with Session(open_db(config)) as s:
        today = queries.today_edition(s, local.date())
        if today is not None and (today.status != EditionStatus.generating or running(today)):
            return False
    cmd = [sys.executable, "-m", "newspaper"]
    if config.path:
        cmd += ["--config", str(config.path.resolve())]
    if sys.platform == "win32":
        flags = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        flags = {"start_new_session": True}
    subprocess.Popen(
        [*cmd, "generate"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **flags,
    )
    return True

"""Windows Task Scheduler integration (`newspaper schedule install|uninstall`) and keeping the
PC awake while the edition is written, and after it while someone may read."""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from .config.models import NewspaperConfig

TASK_NAME = "Newspaper"
FETCH_TASK_NAME = "Newspaper fetch"
WEB_TASK_NAME = "Newspaper web"
WEB_PORT = 8765  # `newspaper web`'s default, which the task uses
# ponytail: Cooperativa's feed holds 15 items, ~100 min of news at midday (2026-10-14);
# 30 min leaves room for bursts. Make it a setting if another feed needs a different pace.
FETCH_EVERY_MINUTES = 30


def _quote(value: str) -> str:
    """PowerShell single-quoted string literal."""
    return "'" + value.replace("'", "''") + "'"


def install_script(config: NewspaperConfig) -> str:
    # pythonw runs without flashing a console window at dawn.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    base = "-m newspaper"
    if config.path:
        base += f' --config "{config.path.resolve()}"'
    # ponytail: the trigger uses the machine's clock, not general.timezone; same thing on a
    # personal PC. Convert the hour if someone runs the PC in another timezone.
    at = f"{config.general.edition_hour:02d}:00"

    def register(
        name: str, command: str, triggers: list[str], minutes: int, about: str, wake: bool = False
    ) -> str:
        return "; ".join(
            [
                f"$action = New-ScheduledTaskAction -Execute {_quote(str(exe))} "
                f"-Argument {_quote(f'{base} {command}')}",
                "$trigger = @("
                + ", ".join(f"(New-ScheduledTaskTrigger {t})" for t in triggers)
                + ")",
                "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable "
                "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
                + ("-WakeToRun " if wake else "")
                + f"-ExecutionTimeLimit (New-TimeSpan -Minutes {minutes})",
                f"Register-ScheduledTask -TaskName {_quote(name)} -Action $action "
                f"-Trigger $trigger -Settings $settings -Description {_quote(about)} "
                "-Force | Out-Null",
            ]
        )

    every = f"-RepetitionInterval (New-TimeSpan -Minutes {FETCH_EVERY_MINUTES})"
    web = _quote(WEB_TASK_NAME)
    return "; ".join(
        [
            # Without it, powershell -Command only reports the last command's status.
            "$ErrorActionPreference = 'Stop'",
            # Reinstalling restarts the server, so it runs the updated code and config.
            f"Stop-ScheduledTask -TaskName {web} -ErrorAction SilentlyContinue",
            register(
                TASK_NAME,
                "generate",
                [f"-Daily -At '{at}'"],
                180,
                "Prepara la edición diaria de Newspaper",
                # The PC usually sleeps at night: without this the edition waits until it wakes.
                # fetch doesn't wake it; at 06:00 the short feeds still cover the night.
                wake=True,
            ),
            register(
                FETCH_TASK_NAME,
                "fetch",
                [f"-Once -At '00:00' {every}"],
                10,
                "Descarga las fuentes de Newspaper",
            ),
            register(
                WEB_TASK_NAME,
                "web --no-browser",
                # At logon, and every 30 min: with IgnoreNew (the default) a running server is left
                # alone and a dead one comes back. RestartOnFailure only covers failed launches.
                # No double quotes: the script travels as one `powershell -Command` argument.
                [
                    "-AtLogOn -User ($env:USERDOMAIN + '\\' + $env:USERNAME)",
                    f"-Once -At '00:00' {every}",
                ],
                0,  # no time limit
                "Sirve la web de Newspaper y mantiene el equipo despierto tras la edición y "
                "mientras se lee desde el móvil",
            ),
            # The stopped server's python child lets go of the port a moment later, and a start
            # before that exits with 3. ponytail: up to 10 s; a manual server keeps it longer.
            f"for ($i = 0; $i -lt 40 -and (Get-NetTCPConnection -LocalPort {WEB_PORT} -State "
            "Listen -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Milliseconds 250 }",
            f"Start-ScheduledTask -TaskName {web}",
        ]
    )


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
# Windows drops the request when the PC sleeps and doesn't restore it on wake (seen 2026-10-18),
# and after an unattended wake it sleeps again within ~2 min: renew well inside that.
RENEW_SECONDS = 60
# The web reader keeps the PC awake this long after an edition (the notice reaches the phone)
# and after each visit (someone reading); then an unattended PC sleeps again (2026-10-22).
# ponytail: constants; move them to the TOML if they need tuning.
AWAKE_AFTER_EDITION = timedelta(minutes=30)
AWAKE_AFTER_VISIT = timedelta(minutes=15)


@contextmanager
def keep_awake(needed: Callable[[], bool] = lambda: True) -> Iterator[Callable[[], None]]:
    """Ask Windows not to sleep while the edition is written, or while the web reader has a
    reason (`needed`, asked at every renewal): the task may have woken the PC, and Windows puts
    it back to sleep unless asked. Yields `recheck`, which asks `needed` again right away."""
    if sys.platform != "win32":
        yield lambda: None
        return
    import ctypes
    import threading

    kernel32 = ctypes.windll.kernel32
    held, recheck, done = threading.Event(), threading.Event(), threading.Event()

    def hold() -> None:  # the request belongs to this thread
        while not done.is_set():
            recheck.clear()  # before asking: a recheck from now on is answered
            try:
                awake = needed()
            except Exception:  # can't tell: stay awake, as before, rather than sleep on a reader
                logging.getLogger(__name__).warning("keep_awake: retengo el PC", exc_info=True)
                awake = True
            kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if awake else 0))
            held.set()
            recheck.wait(RENEW_SECONDS)
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)

    thread = threading.Thread(target=hold, name="keep-awake", daemon=True)
    thread.start()
    held.wait()
    try:
        yield recheck.set
    finally:
        done.set()
        recheck.set()
        thread.join()


def uninstall_script() -> str:
    # Stop first: unregistering a task leaves its running server alive (and reachable).
    # SilentlyContinue: older installs lack the fetch and web tasks.
    return "; ".join(
        [f"Stop-ScheduledTask -TaskName {_quote(WEB_TASK_NAME)} -ErrorAction SilentlyContinue"]
        + [
            f"Unregister-ScheduledTask -TaskName {_quote(name)} -Confirm:$false "
            "-ErrorAction SilentlyContinue"
            for name in (TASK_NAME, FETCH_TASK_NAME, WEB_TASK_NAME)
        ]
    )


def run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
    )

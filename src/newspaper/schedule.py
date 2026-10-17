"""Windows Task Scheduler integration (`newspaper schedule install|uninstall`) and keeping the
PC awake while the edition is written or the web reader serves."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config.models import NewspaperConfig

TASK_NAME = "Newspaper"
FETCH_TASK_NAME = "Newspaper fetch"
WEB_TASK_NAME = "Newspaper web"
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
                "Sirve la web de Newspaper y mantiene el equipo despierto para leer desde el móvil",
            ),
            f"Start-ScheduledTask -TaskName {web}",
        ]
    )


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextmanager
def keep_awake() -> Iterator[None]:
    """Ask Windows not to sleep while the edition is written or the web reader serves (the task
    may have woken the PC, and Windows puts it back to sleep unless asked)."""
    if sys.platform != "win32":
        yield
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        yield
    finally:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)


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

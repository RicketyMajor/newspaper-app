"""Windows Task Scheduler integration (`newspaper schedule install|uninstall`) and keeping the
PC awake while the edition is written."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config.models import NewspaperConfig

TASK_NAME = "Newspaper"
FETCH_TASK_NAME = "Newspaper fetch"
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
        name: str, command: str, trigger: str, minutes: int, about: str, wake: bool = False
    ) -> str:
        return "; ".join(
            [
                f"$action = New-ScheduledTaskAction -Execute {_quote(str(exe))} "
                f"-Argument {_quote(f'{base} {command}')}",
                f"$trigger = New-ScheduledTaskTrigger {trigger}",
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
    return "; ".join(
        [
            register(
                TASK_NAME,
                "generate",
                f"-Daily -At '{at}'",
                180,
                "Prepara la edición diaria de Newspaper",
                # The PC usually sleeps at night: without this the edition waits until it wakes.
                # fetch doesn't wake it; at 06:00 the short feeds still cover the night.
                wake=True,
            ),
            register(
                FETCH_TASK_NAME,
                "fetch",
                f"-Once -At '00:00' {every}",
                10,
                "Descarga las fuentes de Newspaper",
            ),
        ]
    )


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextmanager
def keep_awake() -> Iterator[None]:
    """Ask Windows not to sleep while the edition is written (the task may have woken the PC)."""
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
    # SilentlyContinue: installs older than the fetch task only have the daily one.
    return "; ".join(
        f"Unregister-ScheduledTask -TaskName {_quote(name)} -Confirm:$false "
        "-ErrorAction SilentlyContinue"
        for name in (TASK_NAME, FETCH_TASK_NAME)
    )


def run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
    )

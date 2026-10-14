"""Windows Task Scheduler integration: `newspaper schedule install|uninstall`."""

from __future__ import annotations

import subprocess
import sys
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

    def register(name: str, command: str, trigger: str, minutes: int, about: str) -> str:
        return "; ".join(
            [
                f"$action = New-ScheduledTaskAction -Execute {_quote(str(exe))} "
                f"-Argument {_quote(f'{base} {command}')}",
                f"$trigger = New-ScheduledTaskTrigger {trigger}",
                "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable "
                "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
                f"-ExecutionTimeLimit (New-TimeSpan -Minutes {minutes})",
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

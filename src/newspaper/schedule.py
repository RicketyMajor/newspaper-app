"""Windows Task Scheduler integration: `newspaper schedule install|uninstall`."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .config.models import NewspaperConfig

TASK_NAME = "Newspaper"


def _quote(value: str) -> str:
    """PowerShell single-quoted string literal."""
    return "'" + value.replace("'", "''") + "'"


def install_script(config: NewspaperConfig) -> str:
    # pythonw runs without flashing a console window at dawn.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    args = "-m newspaper"
    if config.path:
        args += f' --config "{config.path.resolve()}"'
    args += " generate"
    # ponytail: the trigger uses the machine's clock, not general.timezone; same thing on a
    # personal PC. Convert the hour if someone runs the PC in another timezone.
    at = f"{config.general.edition_hour:02d}:00"
    return "; ".join(
        [
            f"$action = New-ScheduledTaskAction -Execute {_quote(str(exe))} "
            f"-Argument {_quote(args)}",
            f"$trigger = New-ScheduledTaskTrigger -Daily -At '{at}'",
            "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 3)",
            f"Register-ScheduledTask -TaskName {_quote(TASK_NAME)} -Action $action "
            "-Trigger $trigger -Settings $settings "
            "-Description 'Prepara la edición diaria de Newspaper' -Force | Out-Null",
        ]
    )


def uninstall_script() -> str:
    return f"Unregister-ScheduledTask -TaskName {_quote(TASK_NAME)} -Confirm:$false"


def run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
    )

import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from sqlalchemy.orm import Session

from newspaper import schedule
from newspaper.cli import main
from newspaper.config.loader import EXAMPLE_CONFIG
from newspaper.edition import today as gen
from newspaper.storage.models import Edition, EditionStatus

AFTER_HOUR = datetime(2026, 10, 10, 12, tzinfo=UTC)  # 09:00 in Santiago, edition_hour = 6
BEFORE_HOUR = datetime(2026, 10, 10, 7, tzinfo=UTC)  # 04:00 in Santiago


def test_install_script_registers_daily_task_with_catch_up(config, tmp_path):
    config.path = tmp_path / "it's" / "newspaper.toml"
    script = schedule.install_script(config)

    assert "-StartWhenAvailable" in script
    assert "-Daily -At '06:00'" in script
    assert "pythonw.exe" in script or "python" in script
    assert f'--config "{config.path.resolve()}"'.replace("'", "''") in script
    assert "-TaskName 'Newspaper'" in script
    assert "Unregister-ScheduledTask -TaskName 'Newspaper'" in schedule.uninstall_script()
    assert "-TaskName 'Newspaper fetch'" in script
    assert "-RepetitionInterval (New-TimeSpan -Minutes 30)" in script
    assert script.count(" fetch'") == 2  # the fetch task's argument and its name
    assert "Unregister-ScheduledTask -TaskName 'Newspaper fetch'" in schedule.uninstall_script()


def test_only_the_edition_task_wakes_the_pc(config):
    edition, fetch = schedule.install_script(config).split("-TaskName 'Newspaper'")
    assert "-WakeToRun" in edition
    assert "-WakeToRun" not in fetch


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_keep_awake_holds_the_request_until_the_edition_ends(monkeypatch):
    import ctypes

    states = []
    monkeypatch.setattr(
        ctypes.windll, "kernel32", SimpleNamespace(SetThreadExecutionState=states.append)
    )
    with pytest.raises(RuntimeError), schedule.keep_awake():
        assert states == [schedule.ES_CONTINUOUS | schedule.ES_SYSTEM_REQUIRED]
        raise RuntimeError
    assert states[-1] == schedule.ES_CONTINUOUS


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_keep_awake_renews_the_request_that_sleep_drops(monkeypatch):
    # 2026-10-18: the web server held the request at 13:06 and had lost it after a sleep at
    # 13:08; the PC also slept at 06:13 right after the edition, with the server running.
    import ctypes
    import time

    states = []
    monkeypatch.setattr(
        ctypes.windll, "kernel32", SimpleNamespace(SetThreadExecutionState=states.append)
    )
    monkeypatch.setattr(schedule, "RENEW_SECONDS", 0.01)
    with schedule.keep_awake():
        time.sleep(0.1)
    held = states.count(schedule.ES_CONTINUOUS | schedule.ES_SYSTEM_REQUIRED)
    assert held >= 3 and states[-1] == schedule.ES_CONTINUOUS


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_keep_awake_lets_go_while_nothing_needs_the_pc(monkeypatch):
    # 2026-10-22: the web server kept the PC awake, unattended, from the 06:02 wake until noon.
    import ctypes
    import time

    states = []
    monkeypatch.setattr(
        ctypes.windll, "kernel32", SimpleNamespace(SetThreadExecutionState=states.append)
    )
    monkeypatch.setattr(schedule, "RENEW_SECONDS", 0.01)
    answers = iter([True, False, False, True])
    with schedule.keep_awake(lambda: next(answers, False)):
        time.sleep(0.1)
    hold, free = schedule.ES_CONTINUOUS | schedule.ES_SYSTEM_REQUIRED, schedule.ES_CONTINUOUS
    assert states[:4] == [hold, free, free, hold]
    assert states[-1] == free


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_keep_awake_rechecks_at_once_when_asked(monkeypatch):
    # A visit just after letting go: Windows may sleep long before the next renewal.
    import ctypes
    import time

    states = []
    monkeypatch.setattr(
        ctypes.windll, "kernel32", SimpleNamespace(SetThreadExecutionState=states.append)
    )
    hold, free = schedule.ES_CONTINUOUS | schedule.ES_SYSTEM_REQUIRED, schedule.ES_CONTINUOUS
    answers = iter([False, True])
    with schedule.keep_awake(lambda: next(answers, True)) as recheck:  # renewal: 60 s
        recheck()
        for _ in range(100):
            if len(states) == 2:
                break
            time.sleep(0.01)
        assert states == [free, hold]
    assert states[-1] == free


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_keep_awake_holds_when_it_cannot_tell(monkeypatch):
    import ctypes

    states = []
    monkeypatch.setattr(
        ctypes.windll, "kernel32", SimpleNamespace(SetThreadExecutionState=states.append)
    )

    def needed():
        raise OSError("database is locked")

    with schedule.keep_awake(needed):
        assert states == [schedule.ES_CONTINUOUS | schedule.ES_SYSTEM_REQUIRED]
    assert states[-1] == schedule.ES_CONTINUOUS


def test_keep_awake_does_nothing_off_windows(monkeypatch):
    monkeypatch.setattr(schedule.sys, "platform", "linux")
    with schedule.keep_awake():
        pass


def test_cli_schedule_install_runs_powershell(monkeypatch):
    calls = []

    def fake_run(script):
        calls.append(script)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(schedule, "run_powershell", fake_run)
    monkeypatch.setattr(schedule.sys, "platform", "win32")
    result = CliRunner().invoke(main, ["--config", str(EXAMPLE_CONFIG), "schedule", "install"])

    assert result.exit_code == 0, result.output
    assert "Register-ScheduledTask" in calls[0]
    assert "Newspaper web" in result.output


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(gen.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    return calls


def add_today(engine, status, pid=None):
    with Session(engine) as s, s.begin():
        s.add(Edition(edition_date=date(2026, 10, 10), status=status, pid=pid))


def test_spawns_generation_when_due_and_missing(config, engine, spawned):
    config.path = Path("C:/x/newspaper.toml")
    assert gen.maybe_spawn_generate(config, now=AFTER_HOUR)
    assert spawned[0][-3:] == ["--config", str(config.path.resolve()), "generate"]


def test_does_not_spawn_before_the_hour(config, engine, spawned):
    assert not gen.maybe_spawn_generate(config, now=BEFORE_HOUR)
    assert spawned == []


@pytest.mark.parametrize("status", [EditionStatus.ready, EditionStatus.failed])
def test_does_not_spawn_when_ready_or_failed(config, engine, spawned, status):
    add_today(engine, status)
    assert not gen.maybe_spawn_generate(config, now=AFTER_HOUR)


def test_does_not_spawn_while_running_but_recovers_dead_run(config, engine, spawned):
    add_today(engine, EditionStatus.generating, pid=os.getpid())
    assert not gen.maybe_spawn_generate(config, now=AFTER_HOUR)
    with Session(engine) as s, s.begin():
        s.query(Edition).one().pid = 999_999_999
    assert gen.maybe_spawn_generate(config, now=AFTER_HOUR)


def test_recovers_a_stale_run_even_if_its_pid_was_reused(config, engine, spawned):
    add_today(engine, EditionStatus.generating, pid=os.getpid())
    with Session(engine) as s, s.begin():
        s.query(Edition).one().started_at = datetime(2026, 10, 9)
    assert gen.maybe_spawn_generate(config, now=AFTER_HOUR)


def test_install_runs_the_web_server_task_and_fails_on_any_step(config):
    script = schedule.install_script(config)
    # A failed Register-ScheduledTask must fail install, not only the last command.
    assert script.startswith("$ErrorActionPreference = 'Stop'")
    prefix, edition, fetch, web = script.split("$action =")
    assert "Stop-ScheduledTask -TaskName 'Newspaper web' -ErrorAction SilentlyContinue" in prefix
    assert "web --no-browser" in web and "-AtLogOn" in web
    assert "-RepetitionInterval (New-TimeSpan -Minutes 30)" in web  # back within 30 min
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 0)" in web  # no limit
    assert "-WakeToRun" not in web
    assert script.endswith("Start-ScheduledTask -TaskName 'Newspaper web'")
    assert "-Daily -At '06:00'" in edition and " fetch'" in fetch


def test_uninstall_stops_the_web_server_before_removing_the_tasks():
    script = schedule.uninstall_script()
    stop = script.index("Stop-ScheduledTask -TaskName 'Newspaper web'")
    assert stop < script.index("Unregister-ScheduledTask -TaskName 'Newspaper web'")
    for name in ("Newspaper", "Newspaper fetch", "Newspaper web"):
        assert f"Unregister-ScheduledTask -TaskName '{name}'" in script


def test_install_waits_for_the_stopped_server_to_free_its_port(config):
    # The stopped server's python child lets go of 8765 a moment later; a start before that
    # exits with 3 and leaves no server until the next 30-minute trigger (seen 2026-10-17).
    script = schedule.install_script(config)
    wait = script.index("Get-NetTCPConnection -LocalPort 8765 -State Listen")
    assert script.index("Register-ScheduledTask -TaskName 'Newspaper web'") < wait
    assert wait < script.index("Start-ScheduledTask -TaskName 'Newspaper web'")

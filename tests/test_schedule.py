import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

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

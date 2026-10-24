import logging
import os
import subprocess
import sys
from datetime import UTC, date, datetime, time, timedelta
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


HOLD, FREE = schedule.ES_CONTINUOUS | schedule.ES_SYSTEM_REQUIRED, schedule.ES_CONTINUOUS


def answering(values):
    """Each call returns the next value (raising it if it's an exception); the last repeats."""
    values = list(values)

    def answer():
        value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        return value

    return answer


def hold_and_sleep(monkeypatch, needed, wakes, sleep=None, inputs=((0, 0),)):
    """keep_awake with a recording `sleep`: what `needed`, LastWakeTime and the last input
    (tick, input32) answer in turn; the requests and the sleeps, in order."""
    import ctypes
    import time

    events = []
    monkeypatch.setattr(
        ctypes.windll, "kernel32", SimpleNamespace(SetThreadExecutionState=events.append)
    )
    monkeypatch.setattr(schedule, "RENEW_SECONDS", 0.01)
    monkeypatch.setattr(schedule, "last_wake", answering(wakes))
    monkeypatch.setattr(schedule, "_input_tick", answering(inputs))

    def record(wake, fresh, used, still_needed, visited):
        events.append(("sleep", wake, fresh, still_needed()))

    with schedule.keep_awake(answering(needed), sleep=sleep or record):
        time.sleep(0.1)
    assert events[-1] == FREE
    return events


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_the_end_of_a_hold_may_sleep_the_pc_while_still_holding(monkeypatch):
    # Holding when it calls: an unattended PC would otherwise sleep the second it lets go.
    events = hold_and_sleep(monkeypatch, [False, True, False], [5])
    assert events[:4] == [FREE, HOLD, ("sleep", 5, True, False), FREE]
    assert [e for e in events if e not in (HOLD, FREE)] == [("sleep", 5, True, False)]  # once


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_a_visit_while_deciding_is_seen_right_before_sleeping(monkeypatch):
    import ctypes
    import threading
    import time as clock

    monkeypatch.setattr(
        ctypes.windll, "kernel32", SimpleNamespace(SetThreadExecutionState=lambda state: None)
    )
    monkeypatch.setattr(schedule, "last_wake", lambda: 5)
    monkeypatch.setattr(schedule, "_input_tick", lambda: (0, 0))
    seen, decided = [], threading.Event()
    answers = answering([False, True, False])

    def sleep(wake, fresh, used, still_needed, visited):
        seen.append(visited())
        recheck()  # the visit middleware, mid-decision
        seen.append(visited())
        decided.set()

    with schedule.keep_awake(answers, sleep=sleep) as recheck:  # renewal: 60 s
        recheck()  # a visit: hold
        clock.sleep(0.05)
        monkeypatch.setattr(schedule, "RENEW_SECONDS", 0.01)
        recheck()  # its window is over: decide
        assert decided.wait(2)
    assert seen == [False, True]


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_a_hold_already_there_at_the_wake_is_not_fresh(monkeypatch):
    # 06:00: generate may claim before the first renewal (it's ours, that's fine). Any other wake:
    # a visit window still open from before a manual sleep isn't a reason of this wake.
    events = hold_and_sleep(monkeypatch, [True, True, False], [1, 2])
    assert [e for e in events if e not in (HOLD, FREE)] == [("sleep", 2, False, False)]


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_a_hold_from_before_a_sleep_never_sleeps_the_next_wake(monkeypatch):
    # A visit at night, the PC slept by hand: at 06:00 the PC must not sleep before generate runs.
    events = hold_and_sleep(monkeypatch, [True, False], [1, 2])
    assert not [e for e in events if e not in (HOLD, FREE)]


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_no_hold_no_sleep(monkeypatch):
    assert set(hold_and_sleep(monkeypatch, [False], [5])) == {FREE}


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_holding_because_it_cannot_tell_never_sleeps_the_pc(monkeypatch):
    events = hold_and_sleep(monkeypatch, [OSError("database is locked"), False], [5])
    assert events[:2] == [HOLD, FREE] and set(events) == {HOLD, FREE}


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_a_reason_back_while_deciding_keeps_the_hold(monkeypatch):
    # A visit lands while it decides: the stale "no" is never applied.
    events = hold_and_sleep(monkeypatch, [True, False, True], [5])
    assert events[:3] == [HOLD, ("sleep", 5, False, True), HOLD]


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_an_unknown_wake_only_turns_off_the_sleeping(monkeypatch):
    events = hold_and_sleep(monkeypatch, [True, False, True], [OSError("CallNtPowerInformation")])
    assert events[:3] == [HOLD, FREE, HOLD] and set(events) == {HOLD, FREE}


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_a_failed_sleep_keeps_renewing(monkeypatch):
    def sleep(wake, fresh, used, still_needed, visited):
        raise OSError("SetSuspendState")

    events = hold_and_sleep(monkeypatch, [True, False, True], [5], sleep=sleep)
    assert events[:3] == [HOLD, HOLD, HOLD]  # asked again after the failure: a reason is back


WOKE = 1_000_000 * 10_000  # 1000 s after boot, in LastWakeTime's 100 ns units
LATER = 1_500_000 * 10_000  # the next wake, 500 s on


def used_at_sleeps(
    monkeypatch, inputs, wakes=(WOKE,), needed=(True, True, True, False), at_start=False
):
    """What keep_awake tells each `sleep` about use, for these last inputs (tick, input32): in
    wakes after the one it started in (the web up since the night before), unless `at_start`."""
    told = []

    def sleep(wake, fresh, used, still_needed, visited):
        told.append((wake, used))

    if not at_start:  # the night before: a wake 500 s after boot, its input unknown, no hold
        wakes, inputs, needed = (500_000 * 10_000, *wakes), [(0, 0), *inputs], (False, *needed)
    hold_and_sleep(monkeypatch, list(needed), list(wakes), sleep=sleep, inputs=inputs)
    return told


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_inputs_minutes_apart_after_the_first_minutes_are_someone_using_the_pc(monkeypatch):
    # +200 s, then +290 s: a person comes back to the keyboard; a phantom is one burst.
    inputs = [(1_300_000, 1_200_000), (1_400_000, 1_290_000)]
    assert used_at_sleeps(monkeypatch, inputs) == [(WOKE, True)]


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_one_burst_is_no_use(monkeypatch):
    inputs = [(1_300_000, 1_200_000), (1_400_000, 1_230_000)]  # +200 s, +230 s
    assert used_at_sleeps(monkeypatch, inputs) == [(WOKE, False)]


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_inputs_of_the_first_minutes_are_no_use(monkeypatch):
    inputs = [(1_300_000, 1_020_000), (1_400_000, 1_170_000)]  # +20 s, +170 s: the phantoms
    assert used_at_sleeps(monkeypatch, inputs) == [(WOKE, False)]


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_a_wake_it_cannot_read_keeps_the_use_seen(monkeypatch):
    inputs = [(1_300_000, 1_200_000), (1_400_000, 1_290_000)]
    wakes = (WOKE, WOKE, OSError("CallNtPowerInformation"), WOKE)
    told = used_at_sleeps(monkeypatch, inputs, wakes=wakes, needed=(True,) * 4 + (False,))
    assert told == [(WOKE, True)]


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_a_new_wake_starts_with_no_use(monkeypatch):
    # Used in the first wake; slept by hand; the next wake has only its +50 s phantom.
    inputs = [(1_300_000, 1_200_000), (1_400_000, 1_290_000), (1_600_000, 1_550_000)]
    told = used_at_sleeps(monkeypatch, inputs, wakes=(WOKE, WOKE, LATER))
    assert told == [(LATER, False)]
    # One phantom in each wake (+200 s, then +400 s): two bursts of two wakes are no use either.
    inputs = [(1_300_000, 1_200_000), (1_300_000, 1_200_000), (1_950_000, 1_900_000)]
    told = used_at_sleeps(monkeypatch, inputs, wakes=(WOKE, WOKE, LATER))
    assert told == [(LATER, False)]


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_an_input_it_cannot_read_counts_as_use(monkeypatch):
    told = used_at_sleeps(monkeypatch, [OSError("GetLastInputInfo")], needed=(True, False))
    assert told == [(WOKE, True)]  # any doubt keeps the PC awake: only #22's rule is left


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows-only")
def test_a_wake_under_way_at_start_counts_an_unseen_late_input_as_use(monkeypatch):
    # The server restarted mid-wake (schedule install): an input past the grace before it began
    # may have been someone working, unseen. One within the grace is still the phantom, and a
    # phantom seen afterwards is one burst again.
    late = [(1_300_000, 1_200_000)]
    assert used_at_sleeps(monkeypatch, late, needed=(True, False), at_start=True) == [(WOKE, True)]
    early = [(1_300_000, 1_001_000), (1_400_000, 1_200_000)]  # +1 s at start, then +200 s
    told = used_at_sleeps(monkeypatch, early, needed=(True, True, False), at_start=True)
    assert told == [(WOKE, False)]


def test_input_after_wake_counts_from_the_last_wake():
    assert schedule.input_after_wake(1_100_000, 1_001_000, WOKE) == timedelta(seconds=1)
    assert schedule.input_after_wake(1_100_000, 900_000, WOKE) == timedelta(seconds=-100)


def test_input_after_wake_survives_the_32_bit_wrap():
    # GetLastInputInfo keeps 32 bits: 49.7 days of uptime wrap it, the wake can be before that.
    tick, woke = 2**32 + 5_000, (2**32 - 10_000) * 10_000
    assert schedule.input_after_wake(tick, 2_000, woke) == timedelta(seconds=12)


def test_an_input_stamped_after_the_tick_counts_as_now():
    # SendInput stamps its own time: it must not read as 49.7 days ago, i.e. before the wake.
    assert schedule.input_after_wake(1_100_000, 1_100_500, WOKE) == timedelta(seconds=100)


def test_a_doubtful_wake_is_unknown():
    assert schedule.input_after_wake(1_100_000, 1_000_000, 0) is None  # no sleep since boot
    assert schedule.input_after_wake(1_100_000, 1_000_000, 1_200_000 * 10_000) is None  # future


SIX = [time(6)]


def test_our_wake_forgives_the_phantom_input_of_its_first_minutes():
    # 2026-10-23: woke 06:02:32, a key report at +1 s and the session unlocked itself at +67 s.
    woke = datetime(2026, 10, 23, 6, 2, 32)
    grace = schedule.wake_grace(woke, woke + timedelta(minutes=53), SIX)
    assert grace == schedule.SCHEDULED_WAKE_GRACE >= timedelta(seconds=67)
    # an edition at 23:00 whose hold ends past midnight: still that wake
    woke = datetime(2026, 10, 23, 23, 2)
    grace = schedule.wake_grace(woke, woke + timedelta(hours=1), [time(23)])
    assert grace == schedule.SCHEDULED_WAKE_GRACE


def test_any_other_wake_forgives_only_the_key_that_woke_it():
    late, early = datetime(2026, 10, 23, 6, 12), datetime(2026, 10, 23, 5, 59)
    for woke in (datetime(2026, 10, 23, 23, 17, 31), late, early):
        assert schedule.wake_grace(woke, woke + timedelta(hours=1), SIX) == schedule.WAKE_GRACE
    # yesterday's 06:00, never slept since: today's job may be starting, it's no longer ours
    woke = datetime(2026, 10, 23, 6, 2, 32)
    assert schedule.wake_grace(woke, woke + timedelta(days=1), SIX) == schedule.WAKE_GRACE


AFTER_WAKE = datetime(2026, 10, 23, 23, 20)  # not one of our wakes: the 15 s grace


def sleep_if_unattended(
    monkeypatch, caplog, snapshot, *, needed=False, console=True, visited=False, **kw
):
    """`sleep_if_unattended` at AFTER_WAKE (unless `now`), fresh unless told; True if it slept."""
    slept, order = [], []
    monkeypatch.setattr(schedule, "suspend", lambda: slept.append(1))
    monkeypatch.setattr(schedule, "_console_session", lambda: console)
    monkeypatch.setattr(schedule, "_snapshot", lambda: order.append("snapshot") or snapshot)
    monkeypatch.setattr(schedule, "_power_info", {14: WOKE, 15: WOKE // 2, 16: 0x80000001}.get)
    monkeypatch.setattr(schedule, "_tick", lambda: 1_100_000)
    caplog.set_level(logging.INFO, logger="newspaper.schedule")

    def still_needed():
        order.append("needed")
        return needed

    def was_visited():
        order.append("visited")
        return visited

    now = kw.get("now", AFTER_WAKE)
    fresh, used = kw.get("fresh", True), kw.get("used", False)
    schedule.sleep_if_unattended(SIX, WOKE, fresh, used, still_needed, was_visited, lambda: now)
    assert order[0] == "needed"  # the slow question first: the snapshot is the last thing read
    assert not slept or order[-1] == "visited"  # and a visit, right before sleeping
    return bool(slept)


def test_sleeps_the_pc_nobody_touched_since_it_woke(monkeypatch, caplog):
    assert sleep_if_unattended(monkeypatch, caplog, (1_100_000, 1_001_000, WOKE))
    assert (
        "despertó 23:18:20, última entrada +1 s (margen 15 s), estado 0x80000001: lo duermo"
        in caplog.text
    )
    assert "Volvió: durmió de 23:10:00 a 23:18:20" in caplog.text  # proof it slept, and how long


def test_someone_at_the_pc_keeps_it_awake(monkeypatch, caplog):
    assert not sleep_if_unattended(monkeypatch, caplog, (1_100_000, 1_060_000, WOKE))
    assert "última entrada +60 s (margen 15 s), estado 0x80000001: hay alguien" in caplog.text


def test_our_wake_sleeps_even_with_a_hold_already_there(monkeypatch, caplog):
    # 06:00: generate may claim the edition before the web's first renewal after the wake.
    now, tick = datetime(2026, 10, 23, 6, 55), 1_000_000 + 3_148_000  # woke 06:02:32
    snapshot = (tick, 1_067_000, WOKE)  # the session unlocked itself at +67 s
    assert sleep_if_unattended(monkeypatch, caplog, snapshot, fresh=False, now=now)
    assert "despertó 06:02:32 (programado), última entrada +67 s (margen 180 s)" in caplog.text


DECIDED = datetime(2026, 10, 24, 6, 48, 25)  # 2026-10-24: woke 06:01:26, decided 06:48:25
SINCE = 2_819_000  # ms from that wake to the decision


def on_the_24th(monkeypatch, caplog, input_after_ms, **kw):
    """That morning's decision with the last input at `input_after_ms` after the wake."""
    snapshot = (1_000_000 + SINCE, 1_000_000 + input_after_ms, WOKE)
    return sleep_if_unattended(monkeypatch, caplog, snapshot, now=DECIDED, **kw)


def test_our_wake_sleeps_when_nobody_touched_the_pc_for_a_while(monkeypatch, caplog):
    # The phantom at 06:08:28 (+422 s), 40 min before the decision, and no use since the wake.
    assert on_the_24th(monkeypatch, caplog, 422_000)
    assert (
        "despertó 06:01:26 (programado), última entrada +422 s (margen 180 s), "
        "hace 2397 s (umbral 1200 s), estado 0x80000001: lo duermo" in caplog.text
    )


def test_someone_who_used_the_pc_keeps_it_awake_however_long_ago(monkeypatch, caplog):
    assert not on_the_24th(monkeypatch, caplog, 422_000, used=True)
    assert "hace 2397 s (umbral 1200 s, con uso), estado 0x80000001: hay alguien" in caplog.text


def test_away_starts_at_twenty_minutes(monkeypatch, caplog):
    assert not on_the_24th(monkeypatch, caplog, SINCE - 1_199_000)  # 19:59 ago: maybe reading
    assert on_the_24th(monkeypatch, caplog, SINCE - 1_200_000)


def test_an_input_stamped_after_the_tick_is_someone_now(monkeypatch, caplog):
    # SendInput brings its own time: it reads as now, never as 49.7 days of idling.
    assert not on_the_24th(monkeypatch, caplog, SINCE + 1_000)
    assert "hace 0 s (umbral 1200 s)" in caplog.text


@pytest.mark.parametrize("used", [False, True])
def test_our_wake_still_forgives_its_first_minutes(monkeypatch, caplog, used):
    # #22's rule stands alone: a hold that ends 2 min after a 06:09 wake, outside the job's window.
    snapshot = (1_000_000 + 120_000, 1_001_000, WOKE)
    now = datetime(2026, 10, 24, 6, 11)
    assert sleep_if_unattended(monkeypatch, caplog, snapshot, now=now, used=used)


def test_twenty_minutes_away_is_only_for_our_wakes(monkeypatch, caplog):
    # The user's wake: input at +60 s, an hour ago, is still someone (Sleep: Never is theirs).
    snapshot = (1_000_000 + 3_660_000, 1_060_000, WOKE)
    assert not sleep_if_unattended(monkeypatch, caplog, snapshot)
    assert "última entrada +60 s (margen 15 s), estado 0x80000001: hay alguien" in caplog.text


@pytest.mark.parametrize(
    ("snapshot", "kw"),
    [
        ((1_100_000, 1_001_000, WOKE), {"needed": True}),  # a visit or an edition meanwhile
        ((1_100_000, 1_001_000, WOKE + 10_000), {}),  # it slept and woke again
        ((1_100_000, 1_001_000, 0), {}),  # can't tell when it woke
        ((1_100_000, 1_001_000, WOKE), {"console": False}),  # another session: input not seen
        ((1_100_000, 1_001_000, WOKE), {"fresh": False}),  # the hold came from before the wake
        ((1_100_000, 1_001_000, WOKE), {"visited": True}),  # the phone, right now
        ((1_100_000, 1_001_000, WOKE), {"now": datetime(2026, 10, 24, 6, 0, 5)}),  # job starting
    ],
)
def test_any_doubt_keeps_the_pc_awake(monkeypatch, caplog, snapshot, kw):
    assert not sleep_if_unattended(monkeypatch, caplog, snapshot, **kw)
    assert "no lo duermo" in caplog.text


def test_a_failed_sleep_is_logged(monkeypatch, caplog):
    def suspend():
        raise OSError(1300, "SetSuspendState")

    sleep_if_unattended(monkeypatch, caplog, (1_100_000, 1_001_000, WOKE))  # the fakes
    monkeypatch.setattr(schedule, "suspend", suspend)
    schedule.sleep_if_unattended([], WOKE, True, False, bool, bool, lambda: AFTER_WAKE)
    assert "SetSuspendState falló" in caplog.text


@pytest.mark.skipif(sys.platform != "win32" or "CI" in os.environ, reason="this PC's session")
def test_the_native_readers_answer():
    # Real calls, read-only (nothing here can sleep the PC): the ctypes signatures hold.
    tick, input32, wake = schedule._snapshot()
    assert 0 <= input32 < 2**32 and tick > 0
    assert wake == 0 or 0 < wake // 10_000 <= tick
    assert wake == schedule.last_wake() and schedule._power_info(15) <= wake
    assert 0 <= schedule._power_info(16) < 2**32 and abs(schedule._tick() - tick) < 1_000
    assert schedule._console_session() is True


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

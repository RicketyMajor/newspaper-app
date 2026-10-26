"""Windows Task Scheduler integration (`newspaper schedule install|uninstall`) and keeping the
PC awake while the edition is written, and after it while someone may read."""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path

from .config.models import NewspaperConfig

TASK_NAME = "Newspaper"
FETCH_TASK_NAME = "Newspaper fetch"
WEB_TASK_NAME = "Newspaper web"
NOTICE_TASK_NAME = "Newspaper aviso"
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
    notice = config.general.notice_time
    if notice:
        notice_step = register(
            NOTICE_TASK_NAME,
            "notify --pendiente",
            [f"-Daily -At '{notice}'"],
            10,
            "Despierta el equipo y manda el aviso de Newspaper a su hora",
            wake=True,
        )
    else:  # no longer configured: a task left from before would still wake the PC
        notice_step = (
            f"Unregister-ScheduledTask -TaskName {_quote(NOTICE_TASK_NAME)} -Confirm:$false "
            "-ErrorAction SilentlyContinue"
        )
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
            notice_step,
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
# The web reader keeps the PC awake this long after the notice (the phone saves its copy and
# reads), and after each visit; then an unattended PC sleeps again (2026-10-22, #20a). After an
# edition only AWAY (below): the decision comes that long after the generation's phantom inputs.
# ponytail: constants; move them to the TOML if they need tuning.
AWAKE_AFTER_NOTICE = timedelta(minutes=30)
AWAKE_AFTER_VISIT = timedelta(minutes=15)
# Letting go isn't always enough: after the 06:00 wake Windows may count the PC as attended (a key
# report 1 s after waking, the session unlocking itself a minute later; 2026-10-20, 22 and 23)
# and the user's plan never sleeps it. So the web sleeps it when nobody touched it since it woke:
# input this soon after one of our wakes is no one, after any other wake only the key that woke
# it. ponytail: knobs; web.log shows each decision's numbers to calibrate them. Never past 5 min:
# beyond that a grace swallows real use, so a later phantom means rethinking, not widening.
SCHEDULED_WAKE_GRACE = timedelta(minutes=3)
WAKE_GRACE = timedelta(seconds=15)
# Phantoms come later too (2026-10-24: a second one at +7 min). A phantom is one burst; a person
# touches the PC again and again. So after one of our wakes the PC also sleeps once nobody touched
# it in AWAY (the display goes off at 15 min: 5 more for whoever reads without touching), unless
# the web saw it used: two inputs more than USE_GAP apart past the grace.
AWAY = timedelta(minutes=20)
USE_GAP = timedelta(minutes=1)
# Our wakes came at 06:00:23 … 06:02:35; and in this window after a scheduled time our own job may
# be starting (generate claims its edition ~10 s in), so nothing is slept then.
SCHEDULED_WAKE_WINDOW = timedelta(minutes=10)
# The wake can also come just before its time (07:29:57 for 07:30, 2026-10-26): still ours.
WAKE_LEAD = timedelta(seconds=30)


def input_after_wake(tick_ms: int, input_ms32: int, wake_100ns: int) -> timedelta | None:
    """How long after the last wake the last input came (negative: before it), from
    GetTickCount64, GetLastInputInfo's 32-bit tick and LastWakeTime (100 ns, same clock). None
    when the wake can't be trusted: none since boot, or after now."""
    wake_ms = wake_100ns // 10_000
    if wake_ms <= 0 or wake_ms > tick_ms:
        return None
    idle = (tick_ms - input_ms32) % 2**32
    if idle >= 2**31:  # stamped after the tick (SendInput brings its own time): it's now
        idle = 0
    return timedelta(milliseconds=tick_ms - idle - wake_ms)


def _at_scheduled(
    moment: datetime, scheduled: list[time], lead: timedelta = timedelta(0)
) -> bool:
    """Within the window after a scheduled time, or `lead` before it (local). ponytail: the
    window doesn't cross midnight; today the times are the edition's and the notice's (06:00,
    07:30)."""
    return any(
        -lead <= moment - datetime.combine(moment.date(), t) < SCHEDULED_WAKE_WINDOW
        for t in scheduled
    )


def wake_grace(woke_at: datetime, now: datetime, scheduled: list[time]) -> timedelta:
    """How long after waking an input is still no one: longer after one of our own wakes, if it
    was this one's (past midnight too, for a 23:00 edition), not one a day old."""
    ours = now - woke_at < timedelta(hours=12) and _at_scheduled(woke_at, scheduled, WAKE_LEAD)
    return SCHEDULED_WAKE_GRACE if ours else WAKE_GRACE


def _power_info(level: int) -> int:
    """CallNtPowerInformation: 14 LastWakeTime and 15 LastSleepTime (100 ns of interrupt time,
    GetTickCount64's clock; 0 before the first sleep), 16 SystemExecutionState."""
    import ctypes

    call = ctypes.WinDLL("powrprof").CallNtPowerInformation
    call.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong]
    value = ctypes.c_ulong() if level == 16 else ctypes.c_uint64()
    status = call(level, None, 0, ctypes.byref(value), ctypes.sizeof(value))
    if status:
        raise OSError(f"CallNtPowerInformation({level}): NTSTATUS {status & 0xFFFFFFFF:#x}")
    return value.value


def last_wake() -> int:
    """When the PC last woke (see _power_info)."""
    return _power_info(14)


def _tick() -> int:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetTickCount64.restype = ctypes.c_uint64
    return kernel32.GetTickCount64()


def _input_tick() -> tuple[int, int]:
    """(GetTickCount64 ms, GetLastInputInfo's 32-bit tick). The input before the tick: a key
    pressed between the two reads is never newer than the tick."""
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO))
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _tick(), info.dwTime


def _snapshot() -> tuple[int, int, int]:
    """(GetTickCount64 ms, GetLastInputInfo's 32-bit tick, last_wake())."""
    return (*_input_tick(), last_wake())


def _console_session() -> bool:
    """This process runs in the session at the PC's screen and keyboard: its input is the one
    GetLastInputInfo sees (not session 0, a remote desktop or another user's)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
    session = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session)):
        raise ctypes.WinError(ctypes.get_last_error())
    return session.value == kernel32.WTSGetActiveConsoleSessionId()


def suspend() -> None:
    """Sleep (not hibernate) with wake timers on: the 06:00 edition must still wake the PC.
    Overrides every app's request; returns once the PC is back."""
    import ctypes

    powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
    powrprof.SetSuspendState.argtypes = [ctypes.c_ubyte] * 3
    powrprof.SetSuspendState.restype = ctypes.c_ubyte  # BOOLEAN
    if not powrprof.SetSuspendState(False, False, False):
        raise ctypes.WinError(ctypes.get_last_error())


def _clock(interrupt_100ns: int, tick_ms: int, now: datetime) -> datetime:
    """An interrupt-time instant (LastWakeTime, LastSleepTime) in local time."""
    return now - timedelta(milliseconds=tick_ms - interrupt_100ns // 10_000)


def sleep_if_unattended(
    scheduled: list[time],
    wake: int,
    fresh: bool,
    used: bool,
    needed: Callable[[], bool],
    visited: Callable[[], bool],
    now: Callable[[], datetime] = datetime.now,
) -> None:
    """Sleep the PC if nobody touched it since `wake` (after one of ours, also if nobody touched
    it in AWAY and it wasn't `used`) and nothing needs it any more: called by keep_awake, still
    holding, when a hold of this wake ends (`fresh`: it began after a "no" in this wake, not left
    over from before a sleep; `visited`: a visit since it asked). Any doubt keeps it awake;
    web.log keeps the numbers of each decision."""
    log = logging.getLogger(__name__)
    if needed():  # the slow question first: the snapshot is the last thing read before sleeping
        log.info("Suelto el PC: la razón volvió, no lo duermo")
        return
    moment = now()
    if _at_scheduled(moment, scheduled):
        log.info("Suelto el PC: es la hora de una tarea, no lo duermo")
        return
    tick, input32, now_wake = _snapshot()
    if now_wake != wake:
        log.info("Suelto el PC: despertó otra vez, no lo duermo")
        return
    after = input_after_wake(tick, input32, wake)
    if after is None:
        log.info("Suelto el PC: datos dudosos (despertar %s), no lo duermo", wake)
        return
    woke_at = _clock(wake, tick, moment)
    grace = wake_grace(woke_at, moment, scheduled)
    ours = grace == SCHEDULED_WAKE_GRACE
    idle = timedelta(milliseconds=tick - wake // 10_000) - after  # clamped like `after`
    away = ours and not used and idle >= AWAY
    said = (
        f"Suelto el PC: despertó {woke_at:%H:%M:%S}{' (programado)' if ours else ''}, última "
        f"entrada {after.total_seconds():+.0f} s (margen {grace.total_seconds():.0f} s), "
        + (
            f"hace {idle.total_seconds():.0f} s (umbral {AWAY.total_seconds():.0f} s"
            f"{', con uso' if used else ''}), "
            if ours
            else ""
        )
        + f"estado {_power_info(16):#x}"  # ponytail: data only, for a video/audio veto if needed
    )
    if after > grace and not away:
        log.info("%s: hay alguien", said)
    elif not fresh and not ours:
        log.info("%s: la retención venía de antes del despertar, no lo duermo", said)
    elif not _console_session():
        log.info("%s: otra sesión, no lo duermo", said)
    elif visited():  # the phone, while it decided: its middleware asked for a recheck
        log.info("%s: una visita, no lo duermo", said)
    else:
        log.info("%s: lo duermo", said)
        try:
            suspend()
        except OSError as e:
            log.warning("SetSuspendState falló: %s", e)
            return
        tick, back, again = _tick(), now(), _power_info(14)  # it returns once the PC is back
        if again == wake:  # back at once, never slept: uTorrent's Away Mode did it (2026-10-25)
            log.warning(
                "No durmió: SetSuspendState volvió sin suspender "
                "(¿modo ausente? powercfg /requests, sección AWAYMODE)"
            )
            return
        slept, woke = _clock(_power_info(15), tick, back), _clock(again, tick, back)
        log.info("Volvió: durmió de %s a %s", f"{slept:%H:%M:%S}", f"{woke:%H:%M:%S}")


@contextmanager
def keep_awake(
    needed: Callable[[], bool] = lambda: True,
    sleep: Callable[[int, bool, bool, Callable[[], bool], Callable[[], bool]], None] | None = None,
) -> Iterator[Callable[[], None]]:
    """Ask Windows not to sleep while the edition is written, or while the web reader has a
    reason (`needed`, asked at every renewal): the task may have woken the PC, and Windows puts
    it back to sleep unless asked. Yields `recheck`, which asks `needed` again right away.

    With `sleep` (the web's), the end of a hold seen in this same wake calls
    `sleep(wake, fresh, used, needed, visited)` before letting go: Windows may count an
    unattended PC as attended. `used`: each renewal reads the last input, and two of them more
    than USE_GAP apart past the grace are someone using the PC in this wake."""
    if sys.platform != "win32":
        yield lambda: None
        return
    import ctypes
    import threading

    kernel32 = ctypes.windll.kernel32
    log = logging.getLogger(__name__)
    held, recheck, done = threading.Event(), threading.Event(), threading.Event()

    def ask() -> tuple[bool, bool]:  # (awake, sure)
        try:
            return needed(), True
        except Exception:  # can't tell: stay awake, as before, rather than sleep on a reader
            log.warning("keep_awake: retengo el PC", exc_info=True)
            return True, False

    def woke() -> int | None:
        try:
            return last_wake()
        except Exception:  # only the sleeping is lost: the hold goes on
            log.warning("keep_awake: no sé cuándo despertó el PC", exc_info=True)
            return None

    def hold() -> None:  # the request belongs to this thread
        held_in = None  # the wake in which the web surely needed the PC: only its end may sleep it
        free_in = None  # the wake in which it last didn't
        fresh = False  # the hold began after a "no" in its wake: not left over from before a sleep
        seen_in = None  # the wake that `first` and `used` describe
        first = None  # its first input past the grace
        used = False
        unseen = False  # its inputs so far went unseen: the server started (or restarted) in it
        while not done.is_set():
            recheck.clear()  # before asking: a recheck from now on is answered
            awake, sure = ask()
            if sleep is not None:
                wake = woke()
                if wake != seen_in:  # a wake it couldn't read is unseen too: its next is a doubt
                    seen_in, first, used, unseen = wake, None, False, seen_in is None
                if wake is not None and not used:
                    try:
                        after = input_after_wake(*_input_tick(), wake)
                    except Exception:  # any doubt keeps it awake: only "nobody since it woke" left
                        log.warning("keep_awake: no sé si usaron el PC", exc_info=True)
                        used = True
                    else:
                        if after is not None and after > SCHEDULED_WAKE_GRACE:
                            first = after if first is None else first
                            used = unseen or abs(after - first) > USE_GAP  # unseen: maybe use
                        unseen = False
                if awake and sure:
                    if held_in != wake:
                        fresh = free_in == wake
                    held_in = wake
                elif not awake:
                    if wake is not None and wake == held_in:
                        held_in = None  # once per hold
                        try:
                            sleep(wake, fresh, used, lambda: ask()[0], recheck.is_set)
                        except Exception:
                            log.warning("keep_awake: no pude dormir el PC", exc_info=True)
                        awake, _ = ask()  # never let go on a stale "no": a visit may have come
                    free_in = wake
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
            for name in (TASK_NAME, FETCH_TASK_NAME, WEB_TASK_NAME, NOTICE_TASK_NAME)
        ]
    )


def run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
    )

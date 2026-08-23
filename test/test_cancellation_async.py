"""
Cancellation-safety tests for `bounded_subprocess_async.run`.

`run` promises that the child and its descendants are bounded: the child gets
its own session and the whole process group is killed before `run` returns.
Cancellation is the interesting case, because it is how callers normally
abandon a run (an enclosing `asyncio.wait_for`, a shutting-down server, a
supervisor cancelling a worker). If the cleanup only happens on the
straight-line path, a cancelled `run` walks away from a live process group
that nothing else will ever kill.

These tests only ever launch sleeping children, and each one kills whatever it
finds by marker in a `finally`, so a failure cannot leave the host loaded.
"""

import asyncio
import os
import signal
import uuid
from pathlib import Path
from typing import List, Optional, Set, Tuple

import pytest

from bounded_subprocess.bounded_subprocess_async import run

ROOT = Path(__file__).resolve().parent / "evil_programs"


def _live_pids_matching(marker: str) -> List[int]:
    """
    Pids whose command line contains `marker`.

    We scan `/proc` instead of shelling out to `pgrep`: launching another
    process would both pollute the match and trigger `subprocess._cleanup`,
    which reaps abandoned children and would hide the zombies these tests look
    for. Zombies have an empty command line, so they never show up here.
    """
    needle = marker.encode()
    pids = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            with open(f"/proc/{entry.name}/cmdline", "rb") as f:
                cmdline = f.read()
        except OSError:
            continue
        if needle in cmdline:
            pids.append(int(entry.name))
    return pids


def _read_state_and_parent(pid: int) -> Optional[Tuple[str, int]]:
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
            stat = f.read()
    except OSError:
        return None
    right_paren = stat.rfind(")")
    if right_paren == -1:
        return None
    # After "comm)", the fields are: state, ppid, ...
    rest = stat[right_paren + 1 :].split()
    if len(rest) < 2:
        return None
    try:
        return rest[0], int(rest[1])
    except ValueError:
        return None


def _zombie_children() -> Set[int]:
    """Pids of this process's unreaped children."""
    me = os.getpid()
    zombies = set()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        state_and_parent = _read_state_and_parent(int(entry.name))
        if state_and_parent is None:
            continue
        state, ppid = state_and_parent
        if state == "Z" and ppid == me:
            zombies.add(int(entry.name))
    return zombies


async def _wait_until_running(marker: str, *, timeout_seconds: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if _live_pids_matching(marker):
            return True
        await asyncio.sleep(0.05)
    return False


async def _assert_marker_is_gone(marker: str) -> None:
    # killpg does not block until the group is dead, and /proc can lag, so
    # allow a moment before declaring a leak.
    for _ in range(40):
        survivors = _live_pids_matching(marker)
        if not survivors:
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"processes survived a cancelled run: {_live_pids_matching(marker)}")


def _kill_marker(marker: str) -> None:
    for pid in _live_pids_matching(marker):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_cancelled_run_kills_the_process_group():
    """
    A cancelled `run` must kill the child's whole process group, including
    descendants that outlive the direct child.
    """
    marker = f"bounded-cancel-group-{uuid.uuid4()}"
    task = asyncio.create_task(
        run(
            ["python3", str(ROOT / "fork_child_marker.py"), marker],
            timeout_seconds=60,
            max_output_size=1024,
        )
    )
    try:
        assert await _wait_until_running(marker), "the child never started"
        # The forked grandchild takes a moment to appear.
        await asyncio.sleep(0.5)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await _assert_marker_is_gone(marker)
    finally:
        _kill_marker(marker)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_cancelled_run_during_stdin_write_kills_the_child():
    """
    Cancellation can also land in the stdin write phase, when the child is not
    reading and the pipe is full. That child must not survive either.
    """
    marker = f"bounded-cancel-stdin-{uuid.uuid4()}"
    task = asyncio.create_task(
        run(
            ["python3", str(ROOT / "does_not_read.py"), marker],
            timeout_seconds=60,
            max_output_size=1024,
            # Far more than a pipe buffer holds, so the write phase blocks.
            stdin_data="x" * (4 * 1024 * 1024),
            stdin_write_timeout=60,
        )
    )
    try:
        assert await _wait_until_running(marker), "the child never started"
        await asyncio.sleep(0.25)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await _assert_marker_is_gone(marker)
    finally:
        _kill_marker(marker)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_cancelled_run_reaps_the_child():
    """
    Killing the group is not enough: `run` also has to wait for the direct
    child, which it launched itself. Otherwise every cancelled run leaves a
    zombie behind for the lifetime of the caller's process.
    """
    marker = f"bounded-cancel-reap-{uuid.uuid4()}"
    zombies_before = _zombie_children()
    task = asyncio.create_task(
        run(
            ["python3", str(ROOT / "sleep_forever.py"), marker],
            timeout_seconds=60,
            max_output_size=1024,
        )
    )
    try:
        assert await _wait_until_running(marker), "the child never started"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await _assert_marker_is_gone(marker)
        assert _zombie_children() <= zombies_before, (
            "a cancelled run left its child unreaped"
        )
    finally:
        _kill_marker(marker)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_repeatedly_cancelled_run_still_kills_and_reaps():
    """
    Cleanup that awaits must be shielded from the cancellation that triggered
    it. A caller that keeps cancelling -- an outer `wait_for` whose own
    deadline has passed, or a shutdown loop cancelling until the task is done
    -- otherwise interrupts cleanup partway through, leaving the group alive or
    the child unreaped.
    """
    marker = f"bounded-cancel-twice-{uuid.uuid4()}"
    zombies_before = _zombie_children()
    task = asyncio.create_task(
        run(
            ["python3", str(ROOT / "fork_child_marker.py"), marker],
            timeout_seconds=60,
            max_output_size=1024,
        )
    )
    try:
        assert await _wait_until_running(marker), "the child never started"
        await asyncio.sleep(0.5)

        # Cancel on every scheduling pass, the way a shutdown loop would.
        for _ in range(500):
            if task.done():
                break
            task.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await task

        await _assert_marker_is_gone(marker)
        assert _zombie_children() <= zombies_before, (
            "repeated cancellation cut cleanup short and left the child unreaped"
        )
    finally:
        _kill_marker(marker)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_cancelled_run_does_not_leak_the_memory_watchdog():
    """
    The memory watchdog is a task `run` owns. On cancellation it has to go
    away with the run: a watchdog that outlives its process group keeps
    scanning `/proc` and can `killpg` a recycled process group id.
    """
    marker = f"bounded-cancel-watchdog-{uuid.uuid4()}"
    tasks_before = {t for t in asyncio.all_tasks() if not t.done()}
    task = asyncio.create_task(
        run(
            ["python3", str(ROOT / "sleep_forever.py"), marker],
            timeout_seconds=60,
            max_output_size=1024,
            memory_limit_mb=512,
            memory_watchdog_interval_seconds=0.05,
        )
    )
    try:
        assert await _wait_until_running(marker), "the child never started"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await _assert_marker_is_gone(marker)
        leaked = (
            {t for t in asyncio.all_tasks() if not t.done()} - tasks_before - {task}
        )
        assert not leaked, f"a cancelled run left tasks running: {leaked}"
    finally:
        _kill_marker(marker)


def _open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_cancelled_run_closes_its_pipes():
    """
    Releasing the child closes our ends of its pipes.

    Without that, the file objects live until the `Popen` is collected -- and a
    caller that retains the `CancelledError` retains its traceback, which
    retains `run`'s frame, which retains the `Popen`. Logging the exception for
    later is enough to hold three descriptors per cancelled run.
    """
    marker = f"bounded-cancel-fds-{uuid.uuid4()}"
    held = []
    baseline = _open_fd_count()
    try:
        for _ in range(5):
            task = asyncio.create_task(
                run(
                    ["python3", str(ROOT / "sleep_forever.py"), marker],
                    timeout_seconds=60,
                    max_output_size=1024,
                    stdin_data="x" * (4 * 1024 * 1024),
                    stdin_write_timeout=60,
                )
            )
            assert await _wait_until_running(marker), "the child never started"
            task.cancel()
            try:
                await task
            except asyncio.CancelledError as cancelled:
                held.append(cancelled)  # keeps the traceback, and the Popen
            await _assert_marker_is_gone(marker)

        assert _open_fd_count() <= baseline, (
            "cancelled runs leaked descriptors while the exception was held"
        )
    finally:
        held.clear()
        _kill_marker(marker)

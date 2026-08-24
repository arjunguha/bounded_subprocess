"""
Tests for the child-process resource, independent of who is using it.

`Child` is shared by the sync and async `run`, both `Interactive`s, and
anything else that needs a killable process tree, so its guarantees are worth
pinning down on their own: the session invariant, and a release that kills,
reaps, and closes in either flavor.
"""

import os
import uuid

import pytest

from bounded_subprocess.child import Child

from test.procinfo import live_pids_matching, zombie_children

SLEEPER = "import sys, time; time.sleep(60)"


def _spawn_sleeper(marker: str, **kwargs) -> Child:
    return Child.spawn(["python3", "-c", SLEEPER, marker], **kwargs)


def _wait_until_running(marker: str, *, tries: int = 100) -> bool:
    import time

    for _ in range(tries):
        if live_pids_matching(marker):
            return True
        time.sleep(0.05)
    return False


def test_spawn_puts_the_child_in_its_own_group():
    """
    The pgid is the kill handle, so it has to be the child's own -- killing our
    group instead would take the test runner with it.
    """
    marker = f"bounded-child-group-{uuid.uuid4()}"
    child = _spawn_sleeper(marker)
    try:
        assert child.process_group_id == child.popen.pid
        assert child.process_group_id != os.getpgid(os.getpid())
    finally:
        child.release_sync()


def test_spawn_without_stderr_leaves_it_inherited():
    marker = f"bounded-child-stderr-{uuid.uuid4()}"
    child = _spawn_sleeper(marker, stderr=False)
    try:
        assert child.stderr is None
        assert child.stdout is not None
    finally:
        child.release_sync()


@pytest.mark.timeout(30)
def test_release_sync_kills_descendants_reaps_and_closes():
    marker = f"bounded-child-release-{uuid.uuid4()}"
    zombies_before = zombie_children()
    child = Child.spawn(
        [
            "python3",
            "-c",
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', "
            "sys.argv[1]]); time.sleep(60)",
            marker,
        ]
    )
    try:
        assert _wait_until_running(marker), "the child never started"
        child.release_sync()

        assert live_pids_matching(marker) == [], "a descendant outlived the release"
        assert zombie_children() <= zombies_before, "the child was left unreaped"
        assert child.stdout.closed and child.stderr.closed
    finally:
        for pid in live_pids_matching(marker):
            os.kill(pid, 9)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_release_async_kills_reaps_and_closes():
    marker = f"bounded-child-release-async-{uuid.uuid4()}"
    zombies_before = zombie_children()
    child = _spawn_sleeper(marker, stdin=True)
    try:
        assert _wait_until_running(marker), "the child never started"
        await child.release_async()

        assert live_pids_matching(marker) == []
        assert zombie_children() <= zombies_before
        assert child.stdin.closed and child.stdout.closed
    finally:
        for pid in live_pids_matching(marker):
            os.kill(pid, 9)


@pytest.mark.timeout(30)
def test_release_is_idempotent():
    """Release runs from cleanup paths that can be reached more than once."""
    marker = f"bounded-child-twice-{uuid.uuid4()}"
    child = _spawn_sleeper(marker)
    assert _wait_until_running(marker), "the child never started"
    child.release_sync()
    child.release_sync()
    assert live_pids_matching(marker) == []


@pytest.mark.timeout(30)
def test_release_of_an_exited_child_is_quiet():
    """
    The common case for a well-behaved child: it is already gone, and possibly
    already reaped, by the time we let go of it.
    """
    child = Child.spawn(["true"])
    assert child.reap_sync(10.0), "`true` did not exit"
    child.release_sync()
    assert child.poll() == 0

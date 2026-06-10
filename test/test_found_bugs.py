"""
Regression tests for bugs found by reading the code against its documentation.

Each test documents one bug; the docstrings describe the defect (in the past
tense of the code that has since been fixed) and where it lived. None of
these tests risks destabilizing the host: the largest child allocation is
160 MiB and every child is killed via Interactive.close or the library's own
cleanup.
"""

import fcntl
import time
from pathlib import Path

import pytest
import asyncio

from bounded_subprocess.interactive import Interactive as SyncInteractive
from bounded_subprocess.interactive_async import Interactive as AsyncInteractive
from bounded_subprocess.bounded_subprocess_async import run as async_run

ROOT = Path(__file__).resolve().parent / "evil_programs"


def _stdin_pipe_capacity(p) -> int:
    """
    Size of the child's stdin pipe. Writing exactly this many bytes from a
    clean start goes through the BufferedWriter's write-through path, so it
    fills the pipe completely while leaving the user-space buffer empty.
    """
    return fcntl.fcntl(p._state.popen.stdin.fileno(), fcntl.F_GETPIPE_SZ)


def test_version_matches_package_metadata():
    """
    BUG: src/bounded_subprocess/__init__.py hardcodes __version__ = "1.0.0",
    but pyproject.toml (and the published package) says 2.9.0.
    """
    import bounded_subprocess
    from importlib.metadata import version

    assert bounded_subprocess.__version__ == version("bounded_subprocess")


@pytest.mark.timeout(10)
def test_sync_read_line_returns_output_of_exited_child():
    """
    BUG: Interactive.read_line (interactive.py) checks poll() and returns None
    *before* ever reading the stdout pipe. A child that prints a line and
    exits promptly therefore loses all of its output, even though a complete
    line is sitting unread in the OS pipe buffer. The docstring only allows
    None on "timeout / EOF", and this is neither.
    """
    p = SyncInteractive(["python3", "-c", "print('hello')"], read_buffer_size=1024)
    try:
        time.sleep(1.5)  # ensure the child has exited
        assert p.read_line(timeout_seconds=2) == b"hello"
    finally:
        p.close(1)


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_async_read_line_returns_output_of_exited_child():
    """
    BUG: same early-poll() data loss as the synchronous version, in
    interactive_async.Interactive.read_line.
    """
    p = AsyncInteractive(["python3", "-c", "print('hello')"], read_buffer_size=1024)
    try:
        await asyncio.sleep(1.5)  # ensure the child has exited
        assert await p.read_line(timeout_seconds=2) == b"hello"
    finally:
        await p.close(1)


@pytest.mark.timeout(30)
def test_sync_write_does_not_duplicate_data_when_pipe_is_full():
    """
    BUG: _InteractiveState.write_chunk (interactive.py) miscounts bytes when
    the stdin pipe is full. popen.stdin is a BufferedWriter, so for a payload
    smaller than the buffer, .write() succeeds (buffering the bytes) and the
    subsequent .flush() raises BlockingIOError with characters_written=0.
    write_chunk reports (0, True), so write_loop_sync re-writes the same
    payload on every retry, appending another copy to the buffer each time.
    When the pipe later drains, the child receives several copies of a payload
    that Interactive.write claimed (by returning False) was never delivered.

    The child sleeps 4 s, then reads stdin until it sees b"END" and reports
    how many copies of b"MARKER" it actually received.
    """
    p = SyncInteractive(
        ["python3", ROOT / "counts_marker_after_sleep.py"],
        read_buffer_size=1024,
    )
    try:
        # Fill the pipe exactly to capacity while the child sleeps. This
        # write-through succeeds and leaves the BufferedWriter's buffer empty.
        assert p.write(b"x" * _stdin_pipe_capacity(p), timeout_seconds=2)

        # The pipe is full, so this small write cannot be delivered yet. The
        # buggy retry loop buffers a fresh copy roughly every 0.5 s.
        ok = p.write(b"MARKER", timeout_seconds=2)

        # Wait for the child to wake up and drain the pipe, then send the
        # terminator (its flush also delivers whatever was buffered above).
        time.sleep(3)
        assert p.write(b"END\n", timeout_seconds=2)

        line = p.read_line(timeout_seconds=10)
        assert line is not None
        delivered = int(line)

        # A 6-byte pipe write is atomic: it is either delivered once (write
        # should return True) or not at all (write should return False).
        assert delivered <= 1, (
            f"child received {delivered} copies of a payload written once"
        )
        if not ok:
            assert delivered == 0, (
                "write returned False, but the payload was delivered anyway"
            )
    finally:
        p.close(1)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_async_write_false_means_not_delivered():
    """
    BUG: interactive_async.Interactive.write returns False while having
    accepted the entire payload. For a small payload, write_nonblocking_async
    succeeds immediately (the bytes land in the BufferedWriter), then the
    explicit flush() raises BlockingIOError because the pipe is full, and
    write returns False. The buffered bytes are still delivered by the next
    successful flush. A caller that trusts False ("there was either a timeout
    or a broken pipe") and re-sends the payload will deliver it twice.
    """
    p = AsyncInteractive(
        ["python3", ROOT / "counts_marker_after_sleep.py"],
        read_buffer_size=1024,
    )
    try:
        # Fill the pipe exactly to capacity while the child sleeps. This
        # write-through succeeds and leaves the BufferedWriter's buffer empty.
        assert await p.write(b"x" * _stdin_pipe_capacity(p), timeout_seconds=2)

        ok = await p.write(b"MARKER", timeout_seconds=2)

        # Wait for the child (asleep for its first 4 s) to wake up and drain
        # the pipe, then send the terminator (its flush also delivers
        # whatever was buffered above).
        await asyncio.sleep(4.5)
        assert await p.write(b"END\n", timeout_seconds=2)

        line = await p.read_line(timeout_seconds=10)
        assert line is not None
        delivered = int(line)

        assert delivered <= 1
        if not ok:
            assert delivered == 0, (
                "write returned False, but the payload was delivered anyway"
            )
    finally:
        await p.close(1)


@pytest.mark.asyncio
@pytest.mark.timeout(25)
async def test_memory_watchdog_runs_during_stdin_write_phase():
    """
    BUG: bounded_subprocess_async.run starts the memory watchdog only *after*
    the stdin write phase finishes. A child that allocates past the limit
    immediately but never reads stdin stalls the write phase for the full
    stdin_write_timeout (default 15 s), and during that whole window nothing
    enforces memory_limit_mb. The docs promise the watchdog "polls ... every
    memory_watchdog_interval_seconds and kills the whole group when usage
    exceeds the limit" with no carve-out for the stdin phase.

    The child allocates 160 MiB against a 64 MiB limit and never reads stdin.
    With the watchdog running, the group is killed within ~a second and the
    blocked stdin write fails fast. With the bug, the call cannot return
    before stdin_write_timeout (10 s here).
    """
    start = time.monotonic()
    result = await async_run(
        [
            "python3",
            "-c",
            "import time; x = bytearray(160 * 1024 * 1024); time.sleep(30)",
        ],
        timeout_seconds=15,
        max_output_size=1024,
        stdin_data="y" * (1 << 20),  # exceeds pipe capacity; child never reads
        stdin_write_timeout=10,
        memory_limit_mb=64,
        memory_watchdog_interval_seconds=0.05,
    )
    elapsed = time.monotonic() - start
    assert result.exit_code == -1
    assert elapsed < 8, (
        f"run took {elapsed:.1f}s: the memory watchdog did not run while the "
        "stdin write phase was blocked"
    )

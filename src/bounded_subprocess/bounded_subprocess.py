"""
Synchronous subprocess execution with bounds on runtime and output size.
"""

import subprocess
from typing import List, Optional
import time

from .child import Child
from .util import (
    Result,
    write_nonblocking_sync,
    read_to_eof_sync,
)


def run(
    args: List[str],
    timeout_seconds: int = 15,
    max_output_size: int = 2048,
    tail: bool = False,
    env=None,
    stdin_data: Optional[str] = None,
    stdin_write_timeout: Optional[int] = None,
    cwd: Optional[str] = None,
) -> Result:
    """
    Run a subprocess with a timeout and bounded output capture.

    The child runs in its own session, so on timeout we kill the entire
    process group, not just the child itself. We read stdout and stderr
    nonblockingly and keep at most `max_output_size` bytes from each — the
    prefix by default, or the suffix when `tail=True`.

    On timeout, `Result.timeout` is `True` and `Result.exit_code` is `-1`.
    `timeout_seconds` bounds output collection and the final wait for the
    launched child process. If you pass `stdin_data`, the stdin write phase is
    governed by `stdin_write_timeout` seconds (default 15), not preempted by
    `timeout_seconds`; if the write cannot finish in that time, we force
    `exit_code` to `-1` even when the child exits cleanly.

    If a descendant process inherits stdout or stderr, that pipe can remain
    open after the direct child exits. In that case this function may wait
    until `timeout_seconds` before killing the process group.

    ```python
    from bounded_subprocess import run

    result = run(
        ["bash", "-lc", "echo ok; echo err 1>&2"],
        timeout_seconds=5,
        max_output_size=1024,
    )
    print(result.exit_code, result.stdout.strip(), result.stderr.strip())
    ```
    """
    deadline = time.time() + timeout_seconds

    child = Child.spawn(args, env=env, cwd=cwd, stdin=stdin_data is not None)

    if stdin_data is not None:
        write_ok = write_nonblocking_sync(
            fd=child.stdin,
            data=stdin_data.encode(),
            timeout_seconds=stdin_write_timeout
            if stdin_write_timeout is not None
            else 15,
        )
        # From what I recall, closing stdin is not necessary, but is customary.
        child.close_stdin()

    bufs = read_to_eof_sync(
        [child.stdout, child.stderr],
        timeout_seconds=timeout_seconds,
        max_len=max_output_size,
        tail=tail,
    )

    # Without this, even the trivial test fails on Linux but not on macOS. It
    # seems possible for (1) both stdout and stderr to close (2) before the child
    # process exits, and we can observe the instant between (1) and (2). So, we
    # need to p.wait and not p.poll.
    #
    # Reading the above, we should be able to write a test case that just closes
    # both stdout and stderr explicitly, and then sleeps for an instant before
    # terminating normally. That program should not timeout.
    try:
        exit_code = child.popen.wait(timeout=max(0, deadline - time.time()))
        is_timeout = False
    except subprocess.TimeoutExpired:
        exit_code = None
        is_timeout = True

    # Kills the process group -- without this, test_fork_once fails -- and
    # reaps a timed-out child rather than leaving a zombie behind.
    child.release_sync()

    # Even if the process terminates normally, if we failed to write everything to
    # stdin, we return -1 as the exit code.
    exit_code = (
        -1 if is_timeout or (stdin_data is not None and not write_ok) else exit_code
    )

    return Result(
        timeout=is_timeout,
        exit_code=exit_code,
        stdout=bufs[0].decode(errors="ignore"),  # ty:ignore[not-subscriptable]
        stderr=bufs[1].decode(errors="ignore"),  # ty:ignore[not-subscriptable]
    )

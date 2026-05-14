"""
Synchronous subprocess execution with bounds on runtime and output size.
"""

import subprocess
import os
import signal
from typing import List, Optional
import time

from .util import (
    Result,
    set_nonblocking,
    MAX_BYTES_PER_READ,
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
    Run a subprocess with a wall-clock timeout and bounded output capture.

    The child runs in its own session, so on timeout we kill the entire
    process group, not just the child itself. We read stdout and stderr
    nonblockingly and keep at most `max_output_size` bytes from each — the
    prefix by default, or the suffix when `tail=True`.

    On timeout, `Result.timeout` is `True` and `Result.exit_code` is `-1`.
    If you pass `stdin_data` and we cannot finish writing it within
    `stdin_write_timeout` seconds (default 15), we force `exit_code` to `-1`
    even when the child exits cleanly.

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

    p = subprocess.Popen(
        args,
        env=env,
        cwd=cwd,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        bufsize=MAX_BYTES_PER_READ,
    )
    process_group_id = os.getpgid(p.pid)

    set_nonblocking(p.stdout)
    set_nonblocking(p.stderr)

    if stdin_data is not None:
        set_nonblocking(p.stdin)
        write_ok = write_nonblocking_sync(
            fd=p.stdin,
            data=stdin_data.encode(),
            timeout_seconds=stdin_write_timeout
            if stdin_write_timeout is not None
            else 15,
        )
        # From what I recall, closing stdin is not necessary, but is customary.
        try:
            p.stdin.close()
        except (BrokenPipeError, BlockingIOError):
            pass

    bufs = read_to_eof_sync(
        [p.stdout, p.stderr],
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
        exit_code = p.wait(timeout=max(0, deadline - time.time()))
        is_timeout = False
    except subprocess.TimeoutExpired:
        exit_code = None
        is_timeout = True

    try:
        # Kills the process group. Without this line, test_fork_once fails.
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass

    # Even if the process terminates normally, if we failed to write everything to
    # stdin, we return -1 as the exit code.
    exit_code = (
        -1 if is_timeout or (stdin_data is not None and not write_ok) else exit_code
    )

    return Result(
        timeout=is_timeout,
        exit_code=exit_code,
        stdout=bufs[0].decode(errors="ignore"),
        stderr=bufs[1].decode(errors="ignore"),
    )

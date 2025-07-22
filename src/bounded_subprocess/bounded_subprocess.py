import subprocess
import os
import signal
from typing import List, Optional

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
    env=None,
    stdin_data: Optional[str] = None,
    stdin_write_timeout: Optional[int] = None,
) -> Result:
    """
    Runs the given program with arguments. After the timeout elapses, kills the process
    and all other processes in the process group. Captures at most max_output_size bytes
    of stdout and stderr each, and discards any output beyond that.
    """
    p = subprocess.Popen(
        args,
        env=env,
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
    )

    exit_code = p.poll()
    try:
        # Kills the process group. Without this line, test_fork_once fails.
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass

    timeout = exit_code is None
    exit_code = (
        -1 if timeout or (stdin_data is not None and not write_ok) else exit_code
    )
    stdout = bufs[0].decode(errors="ignore")
    stderr = bufs[1].decode(errors="ignore")
    return Result(
        timeout=timeout,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
    )

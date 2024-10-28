import os
import signal
import fcntl
import asyncio
import subprocess
from typing import List
from bounded_subprocess import (
    MAX_BYTES_PER_READ,
    SLEEP_BETWEEN_READS,
    Result,
    set_nonblocking,
)


async def run(
    args: List[str],
    timeout_seconds: int = 15,
    max_output_size: int = 2048,
    env=None,
) -> Result:
    """
    Runs the given program with arguments. After the timeout elapses, kills the process
    and all other processes in the process group. Captures at most max_output_size bytes
    of stdout and stderr each, and discards any output beyond that.
    """
    # You probably thought we were going to use asyncio.create_subprocess_exec.
    # But, we're not. We're using subprocess.Popen because it supports non-blocking
    # reads. What's async here? It's just the sleep between reads.
    p = subprocess.Popen(
        args,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        bufsize=MAX_BYTES_PER_READ,
    )
    set_nonblocking(p.stdout)
    set_nonblocking(p.stderr)

    process_group_id = os.getpgid(p.pid)

    # We sleep for 0.1 seconds in each iteration.
    max_iterations = timeout_seconds * 10
    stdout_saved_bytes = []
    stderr_saved_bytes = []
    stdout_bytes_read = 0
    stderr_bytes_read = 0

    for _ in range(max_iterations):
        this_stdout_read = p.stdout.read(MAX_BYTES_PER_READ)
        this_stderr_read = p.stderr.read(MAX_BYTES_PER_READ)
        # this_stdout_read and this_stderr_read may be None if stdout or stderr
        # are closed. Without these checks, test_close_output fails.
        if this_stdout_read is not None and stdout_bytes_read < max_output_size:
            stdout_saved_bytes.append(this_stdout_read)
            stdout_bytes_read += len(this_stdout_read)
        if this_stderr_read is not None and stderr_bytes_read < max_output_size:
            stderr_saved_bytes.append(this_stderr_read)
            stderr_bytes_read += len(this_stderr_read)

        exit_code = p.poll()
        if exit_code is not None:
            # finish reading output
            this_stdout_read = p.stdout.read(max_output_size - stdout_bytes_read)
            this_stderr_read = p.stderr.read(max_output_size - stderr_bytes_read)
            if this_stdout_read is not None:
                stdout_saved_bytes.append(this_stdout_read)
            if this_stderr_read is not None:
                stderr_saved_bytes.append(this_stderr_read)
            break

        await asyncio.sleep(SLEEP_BETWEEN_READS)

    try:
        # Kills the process group. Without this line, test_fork_once fails.
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass

    timeout = exit_code is None
    exit_code = exit_code if exit_code is not None else -1
    stdout = b"".join(stdout_saved_bytes).decode("utf-8", errors="ignore")
    stderr = b"".join(stderr_saved_bytes).decode("utf-8", errors="ignore")
    return Result(timeout=timeout, exit_code=exit_code, stdout=stdout, stderr=stderr)

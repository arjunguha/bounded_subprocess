import asyncio
import os
import signal
import time
import subprocess
from typing import List, Optional

from .util import (
    Result,
    set_nonblocking,
    MAX_BYTES_PER_READ,
    write_nonblocking_async,
    read_to_eof_async,
)


async def run(
    args: List[str],
    timeout_seconds: int = 15,
    max_output_size: int = 2048,
    env=None,
    stdin_data: Optional[str] = None,
    stdin_write_timeout: Optional[int] = None,
) -> Result:
    """Run a subprocess asynchronously with bounded I/O."""

    deadline = time.time() + timeout_seconds

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

    write_ok = True
    if stdin_data is not None:
        set_nonblocking(p.stdin)
        write_ok = await write_nonblocking_async(
            fd=p.stdin,
            data=stdin_data.encode(),
            timeout_seconds=stdin_write_timeout if stdin_write_timeout is not None else 15,
        )
        try:
            p.stdin.close()
        except (BrokenPipeError, BlockingIOError):
            pass

    bufs = await read_to_eof_async(
        [p.stdout, p.stderr],
        timeout_seconds=timeout_seconds,
        max_len=max_output_size,
    )

    exit_code = None
    is_timeout = False
    while True:
        rc = p.poll()
        if rc is not None:
            exit_code = rc
            break
        remaining = deadline - time.time()
        if remaining <= 0:
            is_timeout = True
            break
        await asyncio.sleep(min(0.05, remaining))

    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass

    exit_code = -1 if is_timeout or (stdin_data is not None and not write_ok) else exit_code

    return Result(
        timeout=is_timeout,
        exit_code=exit_code if exit_code is not None else -1,
        stdout=bufs[0].decode(errors="ignore"),
        stderr=bufs[1].decode(errors="ignore"),
    )

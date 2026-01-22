"""
Asynchronous subprocess execution with bounds on runtime and output size.
"""

import asyncio
import os
import signal
import time
import subprocess
import tempfile
from typing import List, Optional
import logging

from .util import (
    Result,
    set_nonblocking,
    MAX_BYTES_PER_READ,
    write_nonblocking_async,
    read_to_eof_async,
)

logger = logging.getLogger(__name__)


async def run(
    args: List[str],
    timeout_seconds: int = 15,
    max_output_size: int = 2048,
    env=None,
    stdin_data: Optional[str] = None,
    stdin_write_timeout: Optional[int] = None,
) -> Result:
    """
    Run a subprocess asynchronously with bounded stdout/stderr capture.

    The child process is started in a new session and polled until it exits or
    the timeout elapses. Stdout and stderr are read in nonblocking mode and
    truncated to `max_output_size` bytes each. If the timeout elapses,
    `Result.timeout` is True and `Result.exit_code` is -1. If `stdin_data`
    cannot be fully written before `stdin_write_timeout`, `Result.exit_code`
    is set to -1 even if the process exits normally.

    Example:

    ```python
    import asyncio
    from bounded_subprocess.bounded_subprocess_async import run

    async def main():
        result = await run(
            ["bash", "-lc", "echo ok; echo err 1>&2"],
            timeout_seconds=5,
            max_output_size=1024,
        )
        print(result.exit_code)
        print(result.stdout.strip())
        print(result.stderr.strip())

    asyncio.run(main())
    ```
    """

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
            timeout_seconds=stdin_write_timeout
            if stdin_write_timeout is not None
            else 15,
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

    exit_code = (
        -1 if is_timeout or (stdin_data is not None and not write_ok) else exit_code
    )

    return Result(
        timeout=is_timeout,
        exit_code=exit_code if exit_code is not None else -1,
        stdout=bufs[0].decode(errors="ignore"),
        stderr=bufs[1].decode(errors="ignore"),
    )


# https://docs.podman.io/en/stable/markdown/podman-rm.1.html
async def _podman_rm(cidfile_path: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            "podman",
            "rm",
            "-f",
            "--time",
            "0",
            "--cidfile",
            cidfile_path,
            "--ignore",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # podman rm can take time. I think this will eventually complete even
        # if we timeout below.
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except Exception as e:
        logger.error(f"Error removing container: {e}")
    finally:
        try:
            os.unlink(cidfile_path)
        except OSError:
            pass


async def podman_run(
    args: List[str],
    *,
    image: str,
    timeout_seconds: int,
    max_output_size: int,
    env=None,
    stdin_data: Optional[str] = None,
    stdin_write_timeout: Optional[int] = None,
    volumes: List[str] = [],
    cwd: Optional[str] = None,
) -> Result:
    """
    Run a subprocess in a podman container asynchronously with bounded stdout/stderr capture.

    This function wraps `run` but executes the command inside a podman container.
    The container is automatically removed after execution. The interface is otherwise
    the same as `run`, except for an additional `image` parameter to specify the
    container image to use.

    Args:
        args: Command arguments to run in the container.
        image: Container image to use.
        timeout_seconds: Maximum time to wait for the process to complete.
        max_output_size: Maximum size in bytes for stdout/stderr capture.
        env: Optional dictionary of environment variables.
        stdin_data: Optional string data to write to stdin.
        stdin_write_timeout: Optional timeout for writing stdin data.
        volumes: Optional list of volume mount specifications (e.g., ["/host/path:/container/path"]).
        cwd: Optional working directory path inside the container.

    Example:

    ```python
    import asyncio
    from bounded_subprocess.bounded_subprocess_async import podman_run

    async def main():
        result = await podman_run(
            ["cat"],
            image="alpine:latest",
            timeout_seconds=5,
            max_output_size=1024,
            stdin_data="hello\n",
            volumes=["/host/data:/container/data"],
            cwd="/container/data",
        )
        print(result.exit_code)
        print(result.stdout.strip())

    asyncio.run(main())
    ```
    """
    deadline = time.time() + timeout_seconds

    # Use --cidfile to get the container ID
    with tempfile.NamedTemporaryFile(
        mode="w", delete=False, prefix="bounded_subprocess_cid_"
    ) as cidfile:
        cidfile_path = cidfile.name

    # Build podman command
    podman_args = ["podman", "run", "--rm", "-i", "--cidfile", cidfile_path]

    # Handle environment variables
    if env is not None:
        # Convert env dict to -e flags for podman
        for key, value in env.items():
            podman_args.extend(["-e", f"{key}={value}"])

    # Handle volume mounts
    for volume in volumes:
        podman_args.extend(["-v", volume])

    # Handle working directory
    if cwd is not None:
        podman_args.extend(["-w", cwd])

    podman_args.append(image)
    podman_args.extend(args)

    p = subprocess.Popen(
        podman_args,
        env=None,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=MAX_BYTES_PER_READ,
    )

    set_nonblocking(p.stdout)
    set_nonblocking(p.stderr)

    write_ok = True
    if stdin_data is not None:
        set_nonblocking(p.stdin)
        write_ok = await write_nonblocking_async(
            fd=p.stdin,
            data=stdin_data.encode(),
            timeout_seconds=stdin_write_timeout
            if stdin_write_timeout is not None
            else 15,
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

    # Busy-wait for the process to exit or the deadline. Why do we need this
    # when read_to_eof_async seems to do this? read_to_eof_async will return
    # when the process closes stdout and stderr, but the process can continue
    # running even after that. So, we really need to wait for an exit code.
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

    await _podman_rm(cidfile_path)
    exit_code = (
        -1 if is_timeout or (stdin_data is not None and not write_ok) else exit_code
    )
    return Result(
        timeout=is_timeout,
        exit_code=exit_code if exit_code is not None else -1,
        stdout=bufs[0].decode(errors="ignore"),
        stderr=bufs[1].decode(errors="ignore"),
    )

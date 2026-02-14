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


def _read_process_group_id(pid: int) -> Optional[int]:
    """
    Read a process's PGID from `/proc/<pid>/stat`.

    We use PGID to approximate "all related processes" without requiring
    cgroups. Parsing `/proc/<pid>/stat` is Linux-specific and somewhat brittle
    if format assumptions ever change, but it avoids external dependencies and
    works on typical cluster nodes where cgroups may not be configured for us.
    """
    stat_path = f"/proc/{pid}/stat"
    try:
        with open(stat_path, "r", encoding="utf-8") as f:
            stat = f.read().strip()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None

    right_paren = stat.rfind(")")
    if right_paren == -1:
        return None
    rest = stat[right_paren + 1 :].strip().split()
    # After "comm)", fields are: state, ppid, pgrp, ...
    if len(rest) < 3:
        return None
    try:
        return int(rest[2])
    except ValueError:
        return None


def _read_proc_status_kb(pid: int, metric_name: str) -> Optional[int]:
    """
    Read a memory metric (in kB) from `/proc/<pid>/status`.

    Returns None when the process is gone or the metric is unavailable.
    This is intentionally best-effort because `/proc` is racy for short-lived
    processes and the watchdog tolerates partial visibility.
    """
    status_path = f"/proc/{pid}/status"
    try:
        with open(status_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith(f"{metric_name}:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1])
                    return None
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
        return None
    return None


def _read_peak_rss_kb(pid: int) -> Optional[int]:
    """
    Read per-process peak resident memory in kB.

    Preferred metric is `VmHWM` (peak RSS); if absent, falls back to `VmRSS`
    (current RSS). The fallback makes behavior degrade gracefully on kernels
    that do not expose `VmHWM` consistently, at the cost of weaker "true peak"
    tracking for those processes.
    """
    # VmHWM is the process's peak RSS in kB. Fall back to VmRSS if unavailable.
    peak = _read_proc_status_kb(pid, "VmHWM")
    if peak is not None:
        return peak
    return _read_proc_status_kb(pid, "VmRSS")


def _sum_process_group_peak_rss_kb(process_group_id: int) -> Optional[int]:
    """
    Sum per-process peak RSS across all processes in a process group.

    This is a deliberate non-cgroup approximation for aggregate memory. It
    overcounts shared pages (RSS semantics), can miss processes that start/exit
    between scans, and is O(number of /proc entries) per poll. Those tradeoffs
    are accepted here to keep enforcement portable on cluster environments
    where we cannot assume cgroup control.
    """
    total_kb = 0
    found_any = False
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        pgid = _read_process_group_id(pid)
        if pgid != process_group_id:
            continue
        peak_kb = _read_peak_rss_kb(pid)
        if peak_kb is not None:
            total_kb += peak_kb
            found_any = True
    return total_kb if found_any else None


async def _memory_watchdog(
    p: subprocess.Popen,
    process_group_id: int,
    deadline: float,
    memory_limit_mb: int,
    memory_watchdog_interval_seconds: float,
) -> bool:
    """
    Enforce `memory_limit_mb` using periodic process-group peak-RSS checks.

    The watchdog polls aggregate process-group peak RSS and kills the whole
    process group when the limit is exceeded, returning True in that case.
    Polling is interval-based (default 1s, configurable) and intentionally
    approximate: it favors broad compatibility without cgroups over perfect
    accounting precision.
    """
    limit_kb = memory_limit_mb * 1024
    check_interval = max(0.01, memory_watchdog_interval_seconds)
    while True:
        if p.poll() is not None:
            return False
        aggregate_peak_rss_kb = _sum_process_group_peak_rss_kb(process_group_id)
        if aggregate_peak_rss_kb is not None and aggregate_peak_rss_kb > limit_kb:
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return True
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(check_interval, remaining))


async def run(
    args: List[str],
    timeout_seconds: int = 15,
    max_output_size: int = 2048,
    env=None,
    stdin_data: Optional[str] = None,
    stdin_write_timeout: Optional[int] = None,
    memory_limit_mb: Optional[int] = None,
    memory_watchdog_interval_seconds: float = 1.0,
) -> Result:
    """
    Run a subprocess asynchronously with bounded stdout/stderr capture.

    The child process is started in a new session and polled until it exits or
    the timeout elapses. Stdout and stderr are read in nonblocking mode and
    truncated to `max_output_size` bytes each. If the timeout elapses,
    `Result.timeout` is True and `Result.exit_code` is -1. If `stdin_data`
    cannot be fully written before `stdin_write_timeout`, `Result.exit_code`
    is set to -1 even if the process exits normally. If `memory_limit_mb` is
    set, a watchdog checks aggregate peak RSS (`VmHWM`, summed across the
    process group) at a fixed interval and kills the process group when the
    limit is exceeded.

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

    memory_watchdog_task = None
    if memory_limit_mb is not None:
        memory_watchdog_task = asyncio.create_task(
            _memory_watchdog(
                p=p,
                process_group_id=process_group_id,
                deadline=deadline,
                memory_limit_mb=memory_limit_mb,
                memory_watchdog_interval_seconds=memory_watchdog_interval_seconds,
            )
        )

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

    memory_limit_exceeded = False
    if memory_watchdog_task is not None:
        if memory_watchdog_task.done():
            memory_limit_exceeded = memory_watchdog_task.result()
        else:
            memory_watchdog_task.cancel()
            try:
                await memory_watchdog_task
            except asyncio.CancelledError:
                pass

    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass

    exit_code = (
        -1
        if is_timeout
        or (stdin_data is not None and not write_ok)
        or memory_limit_exceeded
        else exit_code
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
    memory_limit_mb: Optional[int] = None,
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
        memory_limit_mb: Optional memory limit in megabytes for the container.

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

    # Handle memory limit
    if memory_limit_mb is not None:
        podman_args.extend(["--memory", f"{memory_limit_mb}m", "--memory-swap", f"{memory_limit_mb}m"])

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

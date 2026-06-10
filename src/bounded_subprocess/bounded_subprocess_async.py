"""
Asynchronous subprocess execution with bounds on runtime and output size.
"""

import asyncio
import os
import signal
import time
import subprocess
import tempfile
from typing import AsyncIterator, List, Optional
import logging

from .util import (
    Result,
    set_nonblocking,
    MAX_BYTES_PER_READ,
    can_read,
    write_nonblocking_async,
    read_to_eof_async,
)

logger = logging.getLogger(__name__)


def _build_podman_run_args(
    args: List[str],
    *,
    image: str,
    cidfile_path: str,
    env=None,
    volumes: List[str] = [],
    cwd: Optional[str] = None,
    memory_limit_mb: Optional[int] = None,
    entrypoint: Optional[str] = None,
) -> List[str]:
    """
    Build the `podman run` command line shared by the capture and stream APIs.
    """
    podman_args = ["podman", "run", "--rm", "-i", "--cidfile", cidfile_path]

    if env is not None:
        for key, value in env.items():
            podman_args.extend(["-e", f"{key}={value}"])

    for volume in volumes:
        podman_args.extend(["-v", volume])

    if memory_limit_mb is not None:
        podman_args.extend(
            ["--memory", f"{memory_limit_mb}m", "--memory-swap", f"{memory_limit_mb}m"]
        )

    if cwd is not None:
        podman_args.extend(["-w", cwd])

    if entrypoint is not None:
        podman_args.extend(["--entrypoint", entrypoint])

    podman_args.append(image)
    podman_args.extend(args)
    return podman_args


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
        aggregate_peak_rss_kb = _sum_process_group_peak_rss_kb(process_group_id)
        if aggregate_peak_rss_kb is not None and aggregate_peak_rss_kb > limit_kb:
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return True
        if aggregate_peak_rss_kb is None and p.poll() is not None:
            return False
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(check_interval, remaining))


async def run(
    args: List[str],
    timeout_seconds: int = 15,
    max_output_size: int = 2048,
    tail: bool = False,
    env=None,
    stdin_data: Optional[str] = None,
    stdin_write_timeout: Optional[int] = None,
    memory_limit_mb: Optional[int] = None,
    memory_watchdog_interval_seconds: float = 1.0,
    cwd: Optional[str] = None,
) -> Result:
    """
    Async counterpart of `bounded_subprocess.run`, with an optional memory limit.

    The child runs in its own session. We read stdout and stderr nonblockingly
    and keep at most `max_output_size` bytes from each (prefix by default;
    suffix when `tail=True`). On timeout, `Result.timeout` is `True` and
    `Result.exit_code` is `-1`.

    `timeout_seconds` bounds output collection and the final wait for the
    launched child process. If you pass `stdin_data`, the stdin write phase is
    governed by `stdin_write_timeout` seconds (default 15), not preempted by
    `timeout_seconds`; if the write cannot finish in that time, we force
    `exit_code` to `-1` even when the child exits cleanly.

    If a descendant process inherits stdout or stderr, that pipe can remain
    open after the direct child exits. In that case this function may wait
    until `timeout_seconds` before killing the process group.

    When you set `memory_limit_mb`, a watchdog polls aggregate peak RSS
    (`VmHWM` from `/proc`, summed across the process group) every
    `memory_watchdog_interval_seconds` and kills the whole group when usage
    exceeds the limit. We accept this non-cgroup approximation deliberately:
    it overcounts shared pages and can miss very short-lived children, but
    it works without elevated privileges on a typical cluster node.

    ```python
    import asyncio
    from bounded_subprocess.bounded_subprocess_async import run

    async def main():
        result = await run(
            ["bash", "-lc", "echo ok; echo err 1>&2"],
            timeout_seconds=5,
            max_output_size=1024,
        )
        print(result.exit_code, result.stdout.strip(), result.stderr.strip())

    asyncio.run(main())
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

    # Start the watchdog before the stdin write phase: a child that balloons
    # past the limit while stalling our stdin write must still be killed.
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
        tail=tail,
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
    tail: bool = False,
    env=None,
    stdin_data: Optional[str] = None,
    stdin_write_timeout: Optional[int] = None,
    volumes: List[str] = [],
    cwd: Optional[str] = None,
    memory_limit_mb: Optional[int] = None,
    entrypoint: Optional[str] = None,
) -> Result:
    """
    Run a command inside a podman container, with the same bounds as `run`.

    We launch the container with `--rm -i`, track its id through a
    `--cidfile`, and force-remove it when the call returns (whether the
    command succeeded, timed out, or errored). `volumes` flow through as
    `-v` flags, `env` as `-e` flags, and `cwd` as `-w`.

    `entrypoint=""` is meaningful: it clears the image's ENTRYPOINT, so
    `args` becomes the full command. Passing `None` (the default) omits
    the flag entirely and leaves the image's ENTRYPOINT in place.

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
        print(result.exit_code, result.stdout)

    asyncio.run(main())
    ```
    """
    deadline = time.time() + timeout_seconds

    # Use --cidfile to get the container ID
    with tempfile.NamedTemporaryFile(
        mode="w", delete=False, prefix="bounded_subprocess_cid_"
    ) as cidfile:
        cidfile_path = cidfile.name

    podman_args = _build_podman_run_args(
        args,
        image=image,
        cidfile_path=cidfile_path,
        env=env,
        volumes=volumes,
        cwd=cwd,
        memory_limit_mb=memory_limit_mb,
        entrypoint=entrypoint,
    )

    p = subprocess.Popen(
        podman_args,
        env=None,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=MAX_BYTES_PER_READ,
    )

    # The finally block ensures the container is stopped and removed even if
    # the caller cancels us (e.g., an enclosing asyncio.wait_for) or an await
    # raises while output is being collected.
    try:
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
            tail=tail,
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
    finally:
        # Kill the podman client before removing the container. The client may
        # not have created the container yet (e.g., a slow image pull): if it
        # outlived us, it could create and start the container *after* the
        # `podman rm` below ran, and nothing would ever stop that container.
        # Waiting also reaps the client so it does not linger as a zombie.
        if p.poll() is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(asyncio.to_thread(p.wait), timeout=5.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
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


async def _read_line_from_nonblocking_pipe(
    fd,
    saved_bytes: bytearray,
    *,
    deadline: float,
    max_line_size: int,
) -> Optional[str]:
    """
    Read one newline-terminated line from a nonblocking byte pipe.

    The returned line excludes the trailing newline. If EOF arrives with a
    partial line buffered, that partial line is returned once. While waiting
    for a newline, retained bytes are capped to `max_line_size` by dropping the
    oldest bytes, matching the lossy-but-bounded interactive reader behavior.
    """
    line = _pop_decoded_line(saved_bytes, 0)
    if line is not None:
        return line

    while time.time() < deadline:
        wait_timeout = deadline - time.time()
        if wait_timeout <= 0:
            return None
        try:
            await asyncio.wait_for(can_read(fd), timeout=wait_timeout)
        except asyncio.TimeoutError:
            return None

        try:
            new_bytes = fd.read(MAX_BYTES_PER_READ)
        except (BlockingIOError, InterruptedError):
            continue

        if new_bytes is None:
            continue
        if len(new_bytes) == 0:
            if len(saved_bytes) == 0:
                return None
            line = bytes(saved_bytes).decode(errors="ignore")
            saved_bytes.clear()
            return line

        prev_len = len(saved_bytes)
        saved_bytes.extend(new_bytes)
        newline_index = saved_bytes.find(b"\n", prev_len)
        if newline_index != -1:
            if max_line_size <= 0:
                del saved_bytes[:newline_index]
                prev_len = 0
            elif newline_index > max_line_size:
                del saved_bytes[: newline_index - max_line_size]
                prev_len = 0
        line = _pop_decoded_line(saved_bytes, prev_len)
        if line is not None:
            return line
        if max_line_size <= 0:
            saved_bytes.clear()
        elif len(saved_bytes) > max_line_size:
            del saved_bytes[: len(saved_bytes) - max_line_size]
    return None


def _pop_decoded_line(saved_bytes: bytearray, start_idx: int) -> Optional[str]:
    newline_index = saved_bytes.find(b"\n", start_idx)
    if newline_index == -1:
        return None
    line = memoryview(saved_bytes)[:newline_index].tobytes().decode(errors="ignore")
    del saved_bytes[: newline_index + 1]
    return line


async def podman_run_stream_lines(
    args: List[str],
    *,
    image: str,
    timeout_seconds: int,
    max_line_size: int,
    env=None,
    stdin_data: Optional[str] = None,
    stdin_write_timeout: Optional[int] = None,
    volumes: List[str] = [],
    cwd: Optional[str] = None,
    memory_limit_mb: Optional[int] = None,
    entrypoint: Optional[str] = None,
    stderr_max_output_size: int = 2048,
) -> AsyncIterator[str]:
    """
    Stream stdout lines from a command inside a podman container.

    This is the streaming counterpart to `podman_run` for the case where stdin
    is provided all at once as a `str`, but stdout is consumed incrementally.
    The async iterator yields decoded `str` lines without trailing newlines.
    Pending line data is capped at `max_line_size` bytes; if a line grows past
    the cap before a newline arrives, older bytes are discarded.

    The container is force-removed in a `finally` block, so breaking out early
    works as long as the async generator is closed by the caller. For
    deterministic early termination when storing the generator in a variable,
    use `contextlib.aclosing` or call `aclose()`.

    ```python
    from contextlib import aclosing
    from bounded_subprocess.bounded_subprocess_async import podman_run_stream_lines

    async with aclosing(podman_run_stream_lines(
        ["sh", "-c", "printf '%s\\n' one two three"],
        image="alpine:latest",
        timeout_seconds=5,
        max_line_size=1024,
    )) as lines:
        async for line in lines:
            print(line)
            break
    ```
    """
    deadline = time.time() + timeout_seconds
    with tempfile.NamedTemporaryFile(
        mode="w", delete=False, prefix="bounded_subprocess_cid_"
    ) as cidfile:
        cidfile_path = cidfile.name

    podman_args = _build_podman_run_args(
        args,
        image=image,
        cidfile_path=cidfile_path,
        env=env,
        volumes=volumes,
        cwd=cwd,
        memory_limit_mb=memory_limit_mb,
        entrypoint=entrypoint,
    )

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

    stderr_task = asyncio.create_task(
        read_to_eof_async(
            [p.stderr],
            timeout_seconds=timeout_seconds,
            max_len=stderr_max_output_size,
            tail=True,
        )
    )

    try:
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
            if not write_ok:
                return

        saved_stdout = bytearray()
        while True:
            if time.time() >= deadline:
                return
            line = await _read_line_from_nonblocking_pipe(
                p.stdout,
                saved_stdout,
                deadline=deadline,
                max_line_size=max_line_size,
            )
            if line is None:
                return
            yield line
    finally:
        if not stderr_task.done():
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass
        try:
            p.stdout.close()
        except OSError:
            pass
        try:
            if p.stdin is not None:
                p.stdin.close()
        except OSError:
            pass
        try:
            p.stderr.close()
        except OSError:
            pass
        if p.poll() is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(asyncio.to_thread(p.wait), timeout=1.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        await _podman_rm(cidfile_path)

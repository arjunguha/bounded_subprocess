"""
Utilities for bounded subprocess I/O and nonblocking pipe helpers.
"""

import subprocess
import os
import fcntl
import signal
from typing import Callable, List, Optional
import errno
import time
import asyncio
import dataclasses
import select

MAX_BYTES_PER_READ = 1024
SLEEP_BETWEEN_READS = 0.1
SLEEP_BETWEEN_WRITES = 0.01


@dataclasses.dataclass
class Result:
    """
    Result of a bounded subprocess run.

    The `stdout` and `stderr` fields contain at most the requested number of
    bytes, decoded with errors ignored. `timeout` is True only when the overall
    timeout elapses. When a timeout or stdin write failure occurs, `exit_code`
    is -1.
    """

    timeout: int
    exit_code: int
    stdout: str
    stderr: str

    def __init__(self, timeout, exit_code, stdout, stderr):
        self.timeout = timeout
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def set_nonblocking(reader):
    """
    Mark a file descriptor as nonblocking.

    This is required before using the read/write helpers that rely on
    nonblocking behavior.
    """
    fd = reader.fileno()
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)


def write_loop_sync(
    write_chunk: Callable[[memoryview], tuple[int, bool]],
    data: bytes,
    timeout_seconds: float,
    *,
    sleep_interval: float,
) -> bool:
    """
    Repeatedly write data using `write_chunk` until complete or timeout.

    The `write_chunk` callback returns `(bytes_written, keep_going)`. If
    `keep_going` is False, this function returns False immediately.
    """
    mv = memoryview(data)
    start = 0
    start_time = time.time()
    while start < len(mv):
        written, keep_going = write_chunk(mv[start:])
        start += written
        if not keep_going:
            return False
        if start < len(mv):
            if time.time() - start_time > timeout_seconds:
                return False
            time.sleep(sleep_interval)
    return True


async def can_write(fd):
    """
    Wait until the file descriptor is writable.
    """
    future = asyncio.Future()
    loop = asyncio.get_running_loop()
    loop.add_writer(fd, future.set_result, None)
    future.add_done_callback(lambda f: loop.remove_writer(fd))
    await future


async def can_read(fd):
    """
    Wait until the file descriptor is readable.
    """
    future = asyncio.Future()
    loop = asyncio.get_running_loop()
    loop.add_reader(fd, future.set_result, None)
    future.add_done_callback(lambda f: loop.remove_reader(fd))
    await future


async def write_nonblocking_async(*, fd, data: bytes, timeout_seconds: int) -> bool:
    """
    Writes to a nonblocking file descriptor with the timeout.

    Returns True if all the data was written. False indicates that there was
    either a timeout or a broken pipe.

    This function does not close the file descriptor.
    """
    start_time_seconds = time.time()

    # A slice, data[..], would create a copy. A memoryview does not.
    mv = memoryview(data)
    start = 0
    while start < len(mv):
        try:
            # Write as much as possible without blocking.
            written = fd.write(mv[start:])
            if written is None:
                written = 0
            start = start + written
        except BrokenPipeError:
            return False
        except BlockingIOError as exn:
            if exn.errno != errno.EAGAIN:
                # NOTE(arjun): I am not certain why this would happen. However,
                # you are only supposed to retry on EAGAIN.
                return False
            # Some, but not all the bytes were written.
            start = start + exn.characters_written

            # Compute how much more time we have left.
            wait_timeout = timeout_seconds - (time.time() - start_time_seconds)
            # We are already past the deadline, so abort.
            if wait_timeout <= 0:
                return False
            try:
                await asyncio.wait_for(can_write(fd), wait_timeout)
            except asyncio.TimeoutError:
                # Deadline elapsed, so abort.
                return False

    return True


def read_to_eof_sync(
    files: list,
    *,
    timeout_seconds: int,
    max_len: int,
) -> Optional[List[bytes]]:
    """
    Read from nonblocking file descriptors until EOF, with limits on how long
    to wait and the maximum number of bytes to read.

    Returns the data read, or None if the timeout elapsed.
    """
    bufs = {fd: bytearray() for fd in files}
    avail = set(files)
    end_at = time.time() + timeout_seconds

    while avail and time.time() < end_at:
        # Wait only as long as we still have time left
        remaining = max(0, end_at - time.time())
        ready, _, _ = select.select(avail, [], [], remaining)
        if not ready:
            break
        for fd in ready:
            try:
                chunk = fd.read(MAX_BYTES_PER_READ)
                if not chunk:
                    # Reached EOF, so we can stop reading from this file.
                    avail.discard(fd)
                    continue
                the_buf = bufs[fd]
                # Keep at most max_len bytes, silently dropping any extra bytes.
                if len(the_buf) < max_len:
                    keep = max_len - len(the_buf)
                    the_buf.extend(chunk[:keep])
            except (BlockingIOError, InterruptedError):
                # Would-block, so we can't read from this file.
                pass
            except OSError:
                # Broken pipe, bad fd, etc.
                avail.discard(fd)

    # Preserve the caller-supplied order
    return [bytes(bufs[fd]) for fd in files]


async def _wait_for_any_read(fds, timeout: float):
    """Wait until any of the fds is readable or the timeout elapses."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def make_cb(fd):
        return lambda: (not fut.done()) and fut.set_result(fd)

    for fd in fds:
        loop.add_reader(fd.fileno(), make_cb(fd))
    try:
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        for fd in fds:
            loop.remove_reader(fd.fileno())


async def read_to_eof_async(
    files: list,
    *,
    timeout_seconds: int,
    max_len: int,
) -> List[bytes]:
    """
    Asynchronously read from nonblocking FDs until EOF or timeout.

    The returned list preserves the order of the `files` argument.
    """
    bufs = {fd: bytearray() for fd in files}
    avail = list(files)
    end_at = time.time() + timeout_seconds

    while avail and time.time() < end_at:
        remaining = max(0, end_at - time.time())
        fd = await _wait_for_any_read(avail, remaining)
        if fd is None:
            break
        try:
            chunk = fd.read(MAX_BYTES_PER_READ)
            if not chunk:
                avail.remove(fd)
                continue
            buf = bufs[fd]
            if len(buf) < max_len:
                keep = max_len - len(buf)
                buf.extend(chunk[:keep])
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            avail.remove(fd)

    return [bytes(bufs[fd]) for fd in files]


# This function is very similar to write_nonblocking_async. But, in my
# opinion, trying to build an abstraction that works for both sync and async
# code is painful and a deficiency of Python.
def write_nonblocking_sync(*, fd, data: bytes, timeout_seconds: int) -> bool:
    """
    Writes to a nonblocking file descriptor with the timeout.

    Returns True if all the data was written. False indicates that there was
    either a timeout or a broken pipe.

    This function does not close the file descriptor.
    """
    start_time_seconds = time.time()

    # A slice, data[..], would create a copy. A memoryview does not.
    mv = memoryview(data)
    start = 0
    while start < len(mv):
        try:
            # Write as much as possible without blocking.
            written = fd.write(mv[start:])
            if written is None:
                written = 0
            start = start + written
        except BrokenPipeError:
            return False
        except BlockingIOError as exn:
            if exn.errno != errno.EAGAIN:
                # NOTE(arjun): I am not certain why this would happen. However,
                # you are only supposed to retry on EAGAIN.
                return False
            # Some, but not all the bytes were written.
            start = start + exn.characters_written

            # Compute how much more time we have left.
            wait_timeout = timeout_seconds - (time.time() - start_time_seconds)
            # We are already past the deadline, so abort.
            if wait_timeout <= 0:
                return False
            select_result = select.select([], [fd], [], wait_timeout)
            if len(select_result[1]) == 0:
                # Deadline elapsed, so abort.
                return False

    return True


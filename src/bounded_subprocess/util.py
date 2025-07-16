import subprocess
import os
import fcntl
import signal
from typing import Callable, Optional
import errno
import time
import asyncio

MAX_BYTES_PER_READ = 1024
SLEEP_BETWEEN_READS = 0.1
_STDIN_WRITE_TIMEOUT = 15
SLEEP_BETWEEN_WRITES = 0.01


class Result:
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
    fd = reader.fileno()
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)


def popen_write_chunk(p: subprocess.Popen, data: memoryview) -> tuple[int, bool]:
    """Try writing a chunk of bytes to a non-blocking Popen stdin."""
    try:
        written = p.stdin.write(data)
        p.stdin.flush()
        if written is None:
            written = 0
        return written, True
    except BlockingIOError as exn:
        if exn.errno != errno.EAGAIN:
            return exn.characters_written, False
        return exn.characters_written, True
    except BrokenPipeError:
        return 0, False


def write_loop_sync(
    write_chunk: Callable[[memoryview], tuple[int, bool]],
    data: bytes,
    timeout_seconds: float,
    *,
    sleep_interval: float,
) -> bool:
    """Repeatedly write data using write_chunk until complete or timeout."""
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


async def write_loop_async(
    write_chunk: Callable[[memoryview], tuple[int, bool]],
    data: bytes,
    timeout_seconds: float,
    *,
    sleep_interval: float,
) -> bool:
    """Asynchronously write data until complete or timeout."""
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
            await asyncio.sleep(sleep_interval)
    return True


class BoundedSubprocessState:
    """State shared between synchronous and asynchronous subprocess helpers."""

    def __init__(self, args, env, max_output_size, use_stdin_pipe: bool = False):
        """
        Start the process in a new session.
        """
        p = subprocess.Popen(
            args,
            env=env,
            stdin=subprocess.PIPE if use_stdin_pipe else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=MAX_BYTES_PER_READ,
        )
        set_nonblocking(p.stdout)
        set_nonblocking(p.stderr)
        if use_stdin_pipe:
            set_nonblocking(p.stdin)

        self.process_group_id = os.getpgid(p.pid)
        self.p = p
        self.exit_code = None
        self.stdout_saved_bytes = []
        self.stderr_saved_bytes = []
        self.stdout_bytes_read = 0
        self.stderr_bytes_read = 0
        self.max_output_size = max_output_size

    def write_chunk(self, data: memoryview) -> tuple[int, bool]:
        if self.p.stdin is None:
            return 0, False
        try:
            written = self.p.stdin.write(data)
            self.p.stdin.flush()
            if written is None:
                written = 0
            return written, True
        except BlockingIOError as exn:
            if exn.errno != errno.EAGAIN:
                return exn.characters_written, False
            return exn.characters_written, True
        except BrokenPipeError:
            return 0, False

    def close_stdin(self) -> None:
        if self.p.stdin is None:
            return
        try:
            self.p.stdin.close()
        except BrokenPipeError:
            pass

    async def close_stdin_async(self, timeout: int) -> None:
        if self.p.stdin is None:
            return
        for _ in range(timeout):
            try:
                self.p.stdin.close()
                return
            except BlockingIOError:
                await asyncio.sleep(1)
            except BrokenPipeError:
                return



    def try_read(self) -> bool:
        """
        Reads from the process. Returning False indicates that we should stop
        reading.
        """
        this_stdout_read = self.p.stdout.read(MAX_BYTES_PER_READ)
        this_stderr_read = self.p.stderr.read(MAX_BYTES_PER_READ)
        # this_stdout_read and this_stderr_read may be None if stdout or stderr
        # are closed. Without these checks, test_close_output fails.
        if (
            this_stdout_read is not None
            and self.stdout_bytes_read < self.max_output_size
        ):
            self.stdout_saved_bytes.append(this_stdout_read)
            self.stdout_bytes_read += len(this_stdout_read)
        if (
            this_stderr_read is not None
            and self.stderr_bytes_read < self.max_output_size
        ):
            self.stderr_saved_bytes.append(this_stderr_read)
            self.stderr_bytes_read += len(this_stderr_read)

        self.exit_code = self.p.poll()
        if self.exit_code is not None:
            # Finish reading output if needed.
            left_to_read = self.max_output_size - self.stdout_bytes_read
            if left_to_read <= 0:
                return False
            this_stdout_read = self.p.stdout.read(left_to_read)
            this_stderr_read = self.p.stderr.read(left_to_read)
            if this_stdout_read is not None:
                self.stdout_saved_bytes.append(this_stdout_read)
            if this_stderr_read is not None:
                self.stderr_saved_bytes.append(this_stderr_read)
            return False
        return True

    def terminate(self) -> Result:
        try:
            # Kills the process group. Without this line, test_fork_once fails.
            os.killpg(self.process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass

        timeout = self.exit_code is None
        exit_code = self.exit_code if self.exit_code is not None else -1
        stdout = b"".join(self.stdout_saved_bytes).decode("utf-8", errors="ignore")
        stderr = b"".join(self.stderr_saved_bytes).decode("utf-8", errors="ignore")
        return Result(
            timeout=timeout, exit_code=exit_code, stdout=stdout, stderr=stderr
        )

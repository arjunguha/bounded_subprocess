"""
Interactive subprocess wrapper with nonblocking stdin/stdout.
"""

from typing import List, Optional
import os
import signal
import time
import errno
import subprocess
from .util import set_nonblocking, MAX_BYTES_PER_READ, write_loop_sync

_SLEEP_AFTER_WOUND_BLOCK = 0.5


class _InteractiveState:
    """Shared implementation for synchronous and asynchronous interaction."""

    def __init__(
        self,
        args: List[str],
        read_buffer_size: int,
        cwd: Optional[str] = None,
    ) -> None:
        popen = subprocess.Popen(
            args,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            start_new_session=True,
            bufsize=MAX_BYTES_PER_READ,
        )
        process_group_id = os.getpgid(popen.pid)
        set_nonblocking(popen.stdin)
        set_nonblocking(popen.stdout)
        self.popen = popen
        self.process_group_id = process_group_id
        self.read_buffer_size = read_buffer_size
        self.stdout_saved_bytes = bytearray()

    # --- low level helpers -------------------------------------------------
    def poll(self) -> Optional[int]:
        return self.popen.poll()

    def close_pipes(self) -> None:
        try:
            self.popen.stdin.close()
        except (BlockingIOError, BrokenPipeError, ValueError):
            pass
        try:
            self.popen.stdout.close()
        except ValueError:
            pass

    def kill(self) -> None:
        try:
            os.killpg(self.process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            self.popen.wait()
        except ChildProcessError:
            pass

    def return_code(self) -> int:
        rc = self.popen.returncode
        return rc if rc is not None else -9

    def write_chunk(self, data: memoryview) -> tuple[int, bool]:
        # Write to the raw file, bypassing the BufferedWriter. The buffered
        # write() can accept bytes into its user-space buffer while the
        # subsequent flush() fails, so there is no way to report how many
        # bytes actually reached the pipe; retrying then delivers duplicates.
        # The raw write returns exactly the count delivered, or None when the
        # pipe is full.
        try:
            written = self.popen.stdin.raw.write(data)
            return (written if written is not None else 0), True
        except BlockingIOError as exn:
            if exn.errno != errno.EAGAIN:
                return exn.characters_written, False
            return exn.characters_written, True
        except BrokenPipeError:
            return 0, False

    def read_chunk(self) -> Optional[bytes]:
        return self.popen.stdout.read(MAX_BYTES_PER_READ)

    def pop_line(self, start_idx: int) -> Optional[bytes]:
        newline_index = self.stdout_saved_bytes.find(b"\n", start_idx)
        if newline_index == -1:
            return None
        line = memoryview(self.stdout_saved_bytes)[:newline_index].tobytes()
        del self.stdout_saved_bytes[: newline_index + 1]
        return line

    def append_stdout(self, data: bytes) -> None:
        self.stdout_saved_bytes.extend(data)

    def trim_stdout(self) -> None:
        if len(self.stdout_saved_bytes) > self.read_buffer_size:
            del self.stdout_saved_bytes[
                : len(self.stdout_saved_bytes) - self.read_buffer_size
            ]


class Interactive:
    """
    A long-lived subprocess you can write to and read lines from.

    The child runs with nonblocking stdin/stdout pipes. `write` honors a
    timeout; `read_line` returns one complete line at a time (without the
    trailing newline) or `None` on timeout / EOF.

    `read_buffer_size` caps how many bytes of recent stdout we retain while
    waiting for a newline. Lines longer than this lose bytes from the front
    — useful when a child spews structured output without ever emitting
    `\\n`, but lossy if you actually need to read very long lines.

    ```python
    from bounded_subprocess.interactive import Interactive

    proc = Interactive(["python3", "-u", "-c", "print(input())"], read_buffer_size=4096)
    proc.write(b"hello\\n", timeout_seconds=1)
    line = proc.read_line(timeout_seconds=1)   # b'hello'
    rc = proc.close(nice_timeout_seconds=1)
    ```
    """

    def __init__(
        self,
        args: List[str],
        read_buffer_size: int,
        cwd: Optional[str] = None,
    ) -> None:
        """Spawn the child process. See the class docstring for parameter semantics."""
        self._state = _InteractiveState(args, read_buffer_size, cwd=cwd)

    def close(self, nice_timeout_seconds: int) -> int:
        """
        Close the pipes, wait up to `nice_timeout_seconds` for a clean exit,
        then `SIGKILL` the child if it is still running. Returns the child's
        exit code, or `-9` if we had to kill it.
        """
        self._state.close_pipes()
        for _ in range(nice_timeout_seconds):
            if self._state.poll() is not None:
                break
            time.sleep(1)
        self._state.kill()
        return self._state.return_code()

    def write(self, stdin_data: bytes, timeout_seconds: int) -> bool:
        """
        Write `stdin_data` to the child within the timeout. Returns `False`
        if the child already exited or the write failed (e.g. broken pipe).
        """
        if self._state.poll() is not None:
            return False
        return write_loop_sync(
            self._state.write_chunk,
            stdin_data,
            timeout_seconds,
            sleep_interval=_SLEEP_AFTER_WOUND_BLOCK,
        )

    def read_line(self, timeout_seconds: int) -> Optional[bytes]:
        """
        Read the next line of stdout (without the trailing newline), or
        return `None` on timeout / EOF.
        """
        line = self._state.pop_line(0)
        if line is not None:
            return line
        # Note that we must not return early just because the child exited:
        # its final output may still be sitting unread in the pipe. The read
        # loop below observes EOF (an empty read) promptly in that case.
        if self._state.popen.stdout.closed:
            return None
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            new_bytes = self._state.read_chunk()
            if new_bytes is None:
                time.sleep(_SLEEP_AFTER_WOUND_BLOCK)
                continue
            if len(new_bytes) == 0:
                return None
            prev_len = len(self._state.stdout_saved_bytes)
            self._state.append_stdout(new_bytes)
            line = self._state.pop_line(prev_len)
            if line is not None:
                return line
            self._state.trim_stdout()
            time.sleep(_SLEEP_AFTER_WOUND_BLOCK)
        return None

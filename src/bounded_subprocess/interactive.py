"""
Interactive subprocess wrapper with nonblocking stdin/stdout.
"""

from typeguard import typechecked
from typing import List, Optional
import time
import errno
import subprocess
from .util import set_nonblocking, MAX_BYTES_PER_READ, write_loop_sync

_SLEEP_AFTER_WOUND_BLOCK = 0.5


class _InteractiveState:
    """Shared implementation for synchronous and asynchronous interaction."""

    def __init__(self, args: List[str], read_buffer_size: int) -> None:
        popen = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=MAX_BYTES_PER_READ,
        )
        set_nonblocking(popen.stdin)
        set_nonblocking(popen.stdout)
        self.popen = popen
        self.read_buffer_size = read_buffer_size
        self.stdout_saved_bytes = bytearray()

    # --- low level helpers -------------------------------------------------
    def poll(self) -> Optional[int]:
        return self.popen.poll()

    def close_pipes(self) -> None:
        try:
            self.popen.stdin.close()
        except BlockingIOError:
            pass
        self.popen.stdout.close()

    def kill(self) -> None:
        self.popen.kill()

    def return_code(self) -> int:
        rc = self.popen.returncode
        return rc if rc is not None else -9

    def write_chunk(self, data: memoryview) -> tuple[int, bool]:
        try:
            written = self.popen.stdin.write(data)
            self.popen.stdin.flush()
            return written, True
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


@typechecked
class Interactive:
    """
    Interact with a subprocess using nonblocking I/O.

    The subprocess is started with pipes for stdin and stdout. Writes are
    bounded by a timeout, and reads return complete lines (without the trailing
    newline). The internal buffer is capped at `read_buffer_size`; older bytes
    are dropped if output grows without line breaks.

    Example:

    ```python
    from bounded_subprocess.interactive import Interactive

    proc = Interactive(["python3", "-u", "-c", "print(input())"], read_buffer_size=4096)
    ok = proc.write(b"hello\n", timeout_seconds=1)
    line = proc.read_line(timeout_seconds=1)
    rc = proc.close(nice_timeout_seconds=1)
    ```
    """

    def __init__(self, args: List[str], read_buffer_size: int) -> None:
        """
        Start a subprocess with a bounded stdout buffer.

        The child process is created with nonblocking stdin/stdout pipes. The
        internal read buffer keeps at most `read_buffer_size` bytes of recent
        output.
        """
        self._state = _InteractiveState(args, read_buffer_size)

    def close(self, nice_timeout_seconds: int) -> int:
        """
        Close pipes, wait briefly, then kill the subprocess.

        Returns the subprocess return code, or -9 if the process is still
        running when it is killed.
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
        Write `stdin_data` to the subprocess within `timeout_seconds`.

        Returns False if the subprocess has already exited or if writing fails.
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
        Read the next line from stdout, or return None on timeout/EOF.

        The returned line does not include the trailing newline byte.
        """
        line = self._state.pop_line(0)
        if line is not None:
            return line
        if self._state.poll() is not None:
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

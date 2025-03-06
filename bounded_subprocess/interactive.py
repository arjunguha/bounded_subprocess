from typeguard import typechecked
from typing import List, Optional
import time
import errno
import subprocess
from . import set_nonblocking


_MAX_BYTES_PER_READ = 1024
_SLEEP_AFTER_WOUND_BLOCK = 0.5


@typechecked
class Interactive:
    """
    A class for interacting with a subprocess that is careful to use non-blocking
    I/O so that we can timeout reads and writes.
    """

    def __init__(self, args: List[str], read_buffer_size: int):
        """
        read_buffer_size is the maximum number of bytes to read from stdout
        and stdout each. If the process writes more than this, the extra bytes
        will be discarded.
        """
        popen = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # stderr=subprocess.PIPE,
            bufsize=_MAX_BYTES_PER_READ,
        )
        set_nonblocking(popen.stdin)
        set_nonblocking(popen.stdout)
        self._read_buffer_size = read_buffer_size
        self._stdout_saved_bytes = bytearray()
        self._stderr_saved_bytes = bytearray()
        self._popen = popen

    def close_blocking(self, nice_timeout_seconds: int) -> int:
        """
        Close the process and wait for it to exit.
        """
        self._popen.stdin.close()
        self._popen.stdout.close()
        try:
            self._popen.wait(timeout=nice_timeout_seconds)
        except subprocess.TimeoutExpired:
            self._popen.kill()
            self._popen.wait()
        return self._popen.returncode

    def write(self, stdin_data: bytes, timeout_seconds: int):
        """
        Write data to the process's stdin.
        """
        if self._popen.poll() is not None:
            return False

        write_start_index = 0
        start_time = time.time()
        while write_start_index < len(stdin_data):
            try:
                bytes_written = self._popen.stdin.write(stdin_data[write_start_index:])
                self._popen.stdin.flush()
            except BlockingIOError as exn:
                if exn.errno != errno.EAGAIN:
                    return False
                bytes_written = exn.characters_written
                time.sleep(_SLEEP_AFTER_WOUND_BLOCK)
            except BrokenPipeError:
                # The child has closed stdin. It is likely dead.
                return False
            write_start_index += bytes_written
            if time.time() - start_time > timeout_seconds:
                return False
        return True

    def _read_line_from_saved_bytes(self, newline_search_index: int) -> Optional[bytes]:
        """
        Try to read a line of output from the buffer of stdout that this object
        manages itself. The newline_search_index to indicate where to  start
        looking for b"\n". Typically will be 0, but there are cases where we
        are certain that the the newline is not in a prefix.
        """
        newline_index = self._stdout_saved_bytes.find(b"\n", newline_search_index)
        if newline_index == -1:
            return None
        # memoryview helps avoid a pointless copy
        line = memoryview(self._stdout_saved_bytes)[: newline_index].tobytes()
        del self._stdout_saved_bytes[: newline_index + 1]
        return line

    def read_line(self, timeout_seconds: int) -> Optional[bytes]:
        """
        Read a line from the process's stdout.
        """
        from_saved_bytes = self._read_line_from_saved_bytes(0)
        if from_saved_bytes is not None:
            return from_saved_bytes
        if self._popen.poll() is not None:
            return None
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            new_bytes = self._popen.stdout.read(_MAX_BYTES_PER_READ)
            # If the read would block, we get None and not zero bytes on MacOS.
            if new_bytes is None:
                time.sleep(_SLEEP_AFTER_WOUND_BLOCK)
                continue
            # If we read 0 bytes, the child has closed stdout and is likely dead.
            if len(new_bytes) == 0:
                return None
            prev_saved_bytes_len = len(self._stdout_saved_bytes)
            self._stdout_saved_bytes.extend(new_bytes)
            from_saved_bytes = self._read_line_from_saved_bytes(prev_saved_bytes_len)
            if from_saved_bytes is not None:
                return from_saved_bytes
            if len(self._stdout_saved_bytes) > self._read_buffer_size:
                del self._stdout_saved_bytes[:len(self._stdout_saved_bytes) - self._read_buffer_size]
            time.sleep(_SLEEP_AFTER_WOUND_BLOCK)

        return None

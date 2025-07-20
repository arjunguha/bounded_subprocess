from typeguard import typechecked
from typing import List, Optional
import asyncio
import time
from .interactive import _InteractiveState
from .util import write_nonblocking_async, can_read, MAX_BYTES_PER_READ


@typechecked
class Interactive:
    """Asynchronous interface for interacting with a subprocess."""

    def __init__(self, args: List[str], read_buffer_size: int) -> None:
        self._state = _InteractiveState(args, read_buffer_size)

    async def close(self, nice_timeout_seconds: int) -> int:
        self._state.close_pipes()
        for _ in range(nice_timeout_seconds):
            if self._state.poll() is not None:
                break
            await asyncio.sleep(1)
        self._state.kill()
        return self._state.return_code()

    async def write(self, stdin_data: bytes, timeout_seconds: int) -> bool:
        if self._state.poll() is not None:
            return False
        return await write_nonblocking_async(
            fd=self._state.popen.stdin,
            data=stdin_data,
            timeout_seconds=timeout_seconds,
        )

    # I think I have reinvented buffered line reading. I dimly recall studying
    # this in excruciating detail in CS153. The difference here is that there
    # is a bunch of extra work to avoid blocking, a timeout, and a limit on
    # how long a received line can be.
    async def read_line(self, timeout_seconds: int) -> Optional[bytes]:
        # First, try to read a line from the internal buffer. The zero argument
        # indicates where to *start looking for a newline*. The returned line
        # always begins from the start of the buffer. This is an optimization
        # for the loop below.
        line = self._state.pop_line(0)
        if line is not None:
            return line

        if self._state.poll() is not None:
            return None

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            # Non-blocking wait until bytes are available, or we reach the
            # deadline.
            try:
                can_read_timeout = deadline - time.time()
                if can_read_timeout <= 0:
                    return None
                await asyncio.wait_for(
                    can_read(self._state.popen.stdout),
                    timeout=can_read_timeout,
                )
            except asyncio.TimeoutError:
                return None

            new_bytes = self._state.popen.stdout.read(MAX_BYTES_PER_READ)

            # We append the received bytes to the buffer, and look for a newline.
            # As an optimization, we only look for a newline in the received
            # bytes.
            prev_len = len(self._state.stdout_saved_bytes)
            self._state.append_stdout(new_bytes)
            line = self._state.pop_line(prev_len)
            if line is not None:
                return line

            if len(new_bytes) == 0:
                # Closed pipe before newline. We do *not* return the final
                # bit of text that we received. But, we do clear our internal
                # buffer.

                # Alternative design: we could return the following
                # last_incomplete_line = memoryview(self._state.stdout_saved_bytes).tobytes()
                self._state.stdout_saved_bytes.clear()
                return None

            # We cap the size of the received line. This will make things go
            # wrong if we are getting structured output (e.g., JSON) from the
            # subprocess. However, it prevents the subprocess from making us
            # run out of memory.
            self._state.trim_stdout()
        return None

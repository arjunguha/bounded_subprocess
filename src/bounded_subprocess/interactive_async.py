from typeguard import typechecked
from typing import List, Optional
import asyncio
import time

from .interactive import _InteractiveState, _SLEEP_AFTER_WOUND_BLOCK


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
        mv = memoryview(stdin_data)
        start = 0
        start_time = time.time()
        while start < len(stdin_data):
            written, keep_going = self._state.write_chunk(mv[start:])
            start += written
            if not keep_going:
                return False
            if start < len(stdin_data):
                if time.time() - start_time > timeout_seconds:
                    return False
                await asyncio.sleep(_SLEEP_AFTER_WOUND_BLOCK)
        return True

    async def read_line(self, timeout_seconds: int) -> Optional[bytes]:
        line = self._state.pop_line(0)
        if line is not None:
            return line
        if self._state.poll() is not None:
            return None
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            new_bytes = self._state.read_chunk()
            if new_bytes is None:
                await asyncio.sleep(_SLEEP_AFTER_WOUND_BLOCK)
                continue
            if len(new_bytes) == 0:
                return None
            prev_len = len(self._state.stdout_saved_bytes)
            self._state.append_stdout(new_bytes)
            line = self._state.pop_line(prev_len)
            if line is not None:
                return line
            self._state.trim_stdout()
            await asyncio.sleep(_SLEEP_AFTER_WOUND_BLOCK)
        return None


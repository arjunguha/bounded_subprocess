import time
from typing import List, Optional

from .util import (
    Result,
    BoundedSubprocessState,
    SLEEP_BETWEEN_READS,
    write_loop_sync,
    _STDIN_WRITE_TIMEOUT,
    SLEEP_BETWEEN_WRITES,
)


def run(
    args: List[str],
    timeout_seconds: int = 15,
    max_output_size: int = 2048,
    env=None,
    stdin_data: Optional[str] = None,
    stdin_write_timeout: Optional[int] = None,
) -> Result:
    """
    Runs the given program with arguments. After the timeout elapses, kills the process
    and all other processes in the process group. Captures at most max_output_size bytes
    of stdout and stderr each, and discards any output beyond that.
    """
    state = BoundedSubprocessState(args, env, max_output_size, stdin_data is not None)
    if stdin_data is not None:
        ok = write_loop_sync(
            state.write_chunk,
            stdin_data.encode(),
            stdin_write_timeout if stdin_write_timeout is not None else 15,
            sleep_interval=SLEEP_BETWEEN_WRITES,
        )
        if not ok:
            state.terminate()
            return Result(True, -1, "", "failed to write to stdin")

        state.close_stdin()

    # We sleep for 0.1 seconds in each iteration.
    max_iterations = timeout_seconds * 10

    for _ in range(max_iterations):
        keep_reading = state.try_read()
        if keep_reading:
            time.sleep(SLEEP_BETWEEN_READS)
        else:
            break

    return state.terminate()

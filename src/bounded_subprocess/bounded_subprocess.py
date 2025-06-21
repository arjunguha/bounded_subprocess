import time
from typing import List, Optional

from .util import (
    Result,
    BoundedSubprocessState,
    SLEEP_BETWEEN_READS,
    write_loop_sync,
    _STDIN_WRITE_TIMEOUT,
)


def run(
    args: List[str],
    timeout_seconds: int = 15,
    max_output_size: int = 2048,
    env=None,
    stdin_data: Optional[str] = None,
) -> Result:
    """
    Runs the given program with arguments. After the timeout elapses, kills the process
    and all other processes in the process group. Captures at most max_output_size bytes
    of stdout and stderr each, and discards any output beyond that.
    """
    state = BoundedSubprocessState(args, env, max_output_size, stdin_data is not None)
    if stdin_data is not None:
        write_loop_sync(
            state.write_chunk,
            stdin_data.encode(),
            _STDIN_WRITE_TIMEOUT,
            sleep_interval=SLEEP_BETWEEN_READS,
        )
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

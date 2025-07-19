import time
from typing import List, Optional

from .util import (
    Result,
    BoundedSubprocessState,
    SLEEP_BETWEEN_READS,
    write_nonblocking_sync,
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
        write_ok = write_nonblocking_sync(
            fd=state.p.stdin,
            data=stdin_data.encode(),
            timeout_seconds=stdin_write_timeout if stdin_write_timeout is not None else 15,
        )
        state.close_stdin(stdin_write_timeout if stdin_write_timeout is not None else 15)

    # We sleep for 0.1 seconds in each iteration.
    max_iterations = timeout_seconds * 10

    for _ in range(max_iterations):
        keep_reading = state.try_read()
        if keep_reading:
            time.sleep(SLEEP_BETWEEN_READS)
        else:
            break

    result = state.terminate()
    if stdin_data is not None and not write_ok:
        result.exit_code = -1
        result.stderr = result.stderr + "\nFailed to write all data to subprocess."
    return result


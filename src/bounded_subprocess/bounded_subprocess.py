import time
from typing import List

from .util import Result, BoundedSubprocessState, SLEEP_BETWEEN_READS


def run(
    args: List[str],
    timeout_seconds: int = 15,
    max_output_size: int = 2048,
    env=None,
) -> Result:
    """
    Runs the given program with arguments. After the timeout elapses, kills the process
    and all other processes in the process group. Captures at most max_output_size bytes
    of stdout and stderr each, and discards any output beyond that.
    """
    state = BoundedSubprocessState(args, env, max_output_size)

    # We sleep for 0.1 seconds in each iteration.
    max_iterations = timeout_seconds * 10

    for _ in range(max_iterations):
        keep_reading = state.try_read()
        if keep_reading:
            time.sleep(SLEEP_BETWEEN_READS)
        else:
            break

    return state.terminate()
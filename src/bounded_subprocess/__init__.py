"""
Bounded subprocess execution with timeout and output limits.

This package provides convenient functions for running subprocesses with bounded
execution time and output size, with support for both synchronous and asynchronous
execution patterns.
"""

__version__ = "1.0.0"

# Lazy imports for better startup performance
def __getattr__(name):
    if name == "run":
        from .bounded_subprocess import run
        return run
    elif name == "Result":
        from .util import Result
        return Result
    elif name == "BoundedSubprocessState": 
        from .util import BoundedSubprocessState
        return BoundedSubprocessState
    elif name == "SLEEP_BETWEEN_READS":
        from .util import SLEEP_BETWEEN_READS
        return SLEEP_BETWEEN_READS
    else:
        raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

# Expose key classes and constants for convenience
__all__ = [
    "run",
    "Result", 
    "BoundedSubprocessState",
    "SLEEP_BETWEEN_READS"
]

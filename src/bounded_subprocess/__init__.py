"""
Bounded subprocess execution with timeout and output limits.

This package provides convenient functions for running subprocesses with bounded
execution time and output size, with support for both synchronous and asynchronous
execution patterns.
"""

# Lazy imports for better startup performance
def __getattr__(name):
    if name == "run":
        from .bounded_subprocess import run

        return run
    elif name == "__version__":
        # Resolve from the installed package metadata so the version cannot
        # drift from pyproject.toml.
        from importlib.metadata import version

        return version("bounded_subprocess")
    elif name == "Result":
        from .util import Result

        return Result
    elif name == "SLEEP_BETWEEN_READS":
        from .util import SLEEP_BETWEEN_READS

        return SLEEP_BETWEEN_READS
    else:
        raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


# Expose key classes and constants for convenience
__all__ = ["run", "Result", "SLEEP_BETWEEN_READS"]

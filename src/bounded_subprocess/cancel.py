"""
Cancellation discipline for cleanup that must not be abandoned.
"""

import asyncio
from typing import Awaitable


async def uncancellable(work: Awaitable[None]) -> bool:
    """
    Run `work` to completion even while the caller is being cancelled.

    Cleanup that awaits is cleanup that can be interrupted. The
    `CancelledError` that triggered the cleanup is delivered again at the next
    `await`, and a caller that cancels repeatedly -- a shutdown loop, an outer
    `wait_for` whose deadline has passed -- can cut the cleanup off partway.
    Shielding moves `work` into its own task, so cancelling the awaiter does
    not touch it, and we keep waiting until it finishes.

    Returns True if a cancellation arrived while we were waiting. Those
    cancellations are absorbed here, so a caller that has nothing else to raise
    owes the event loop a `raise asyncio.CancelledError` of its own; a caller
    already unwinding under an exception just re-raises that one.

    `work` must be bounded, or this waits forever. Exceptions from `work`
    propagate: cleanup that can fail should handle its own failures rather than
    replace whatever exception the caller was already unwinding under.
    """
    task = asyncio.ensure_future(work)
    absorbed_cancellation = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            absorbed_cancellation = True
    return absorbed_cancellation

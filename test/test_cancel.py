"""
Tests for the cancellation primitive, with no subprocess in sight.

`uncancellable` is the whole of the shielded-cleanup pattern, so it can be
tested against a plain sleep instead of through a process's cleanup path.
"""

import asyncio

import pytest

from bounded_subprocess.cancel import uncancellable


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_work_finishes_when_the_caller_is_cancelled_once():
    finished = False

    async def work():
        nonlocal finished
        await asyncio.sleep(0.2)
        finished = True

    async def caller():
        return await uncancellable(work())

    task = asyncio.create_task(caller())
    await asyncio.sleep(0.05)
    task.cancel()

    assert await task is True, "the absorbed cancellation was not reported"
    assert finished, "cleanup did not run to completion"


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_work_finishes_when_the_caller_is_cancelled_repeatedly():
    """The case a bare `await` in a cleanup block cannot survive."""
    finished = False

    async def work():
        nonlocal finished
        for _ in range(10):
            await asyncio.sleep(0.02)
        finished = True

    async def caller():
        return await uncancellable(work())

    task = asyncio.create_task(caller())
    await asyncio.sleep(0.05)
    for _ in range(500):
        if task.done():
            break
        task.cancel()
        await asyncio.sleep(0)

    assert await task is True
    assert finished, "repeated cancellation cut the cleanup short"


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_reports_no_cancellation_when_none_arrives():
    async def work():
        await asyncio.sleep(0)

    assert await uncancellable(work()) is False


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_exceptions_from_work_propagate():
    """
    Cleanup that can fail has to handle that itself. Hiding the failure here
    would silently drop it; letting it out replaces the caller's exception.
    Neither is this primitive's call to make.
    """

    async def work():
        raise RuntimeError("cleanup failed")

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await uncancellable(work())

import pytest
from bounded_subprocess.bounded_subprocess_async import run
from pathlib import Path
import asyncio
import time
import os

ROOT = Path(__file__).resolve().parent / "evil_programs"


async def assert_no_running_evil():
    result = await run(["pgrep", "-f", ROOT], timeout_seconds=1, max_output_size=1024)
    assert (
        result.exit_code == 1
    ), f"There are still evil processes running: {result.stdout}"
    assert len(result.stderr) == 0
    assert len(result.stdout) == 0


@pytest.mark.asyncio
async def test_fork_once():
    # The program exits cleanly and immediately. But, it forks a child that runs
    # forever.
    result = await run(
        ["python3", ROOT / "fork_once.py"],
        timeout_seconds=2,
        max_output_size=1024,
    )
    assert result.exit_code == 0
    assert result.timeout == False
    assert len(result.stderr) == 0
    assert len(result.stdout) == 0
    await assert_no_running_evil()


@pytest.mark.asyncio
async def test_close_outputs():
    # The program prints to stdout, closes its output, and then runs forever.
    result = await run(
        ["python3", ROOT / "close_outputs.py"],
        timeout_seconds=2,
        max_output_size=1024,
    )
    assert result.exit_code == -1
    assert result.timeout == True
    assert len(result.stderr) == 0
    assert result.stdout == "This is the end\n"
    await assert_no_running_evil()


@pytest.mark.asyncio
async def test_unbounded_output():
    result = await run(
        ["python3", ROOT / "unbounded_output.py"],
        timeout_seconds=3,
        max_output_size=1024,
    )
    assert result.exit_code == -1
    assert result.timeout == True
    assert len(result.stderr) == 0
    assert len(result.stdout) == 1024
    await assert_no_running_evil()


@pytest.mark.asyncio
async def test_concurrent_sleep():
    proc = lambda: run(["sleep", "1"], timeout_seconds=2)
    results = await asyncio.gather(proc(), proc(), proc())
    start_time = time.time()
    assert all(r.exit_code == 0 for r in results)
    assert all(r.timeout == False for r in results)
    assert all(len(r.stderr) == 0 for r in results)
    assert time.time() - start_time < 1.1
    await assert_no_running_evil()

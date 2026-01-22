import pytest
from bounded_subprocess.bounded_subprocess_async import run, podman_run
from pathlib import Path
import asyncio
import time
import os

ROOT = Path(__file__).resolve().parent / "evil_programs"


async def assert_no_running_evil():
    result = await run(["pgrep", "-f", ROOT], timeout_seconds=1, max_output_size=1024)
    assert result.exit_code == 1, (
        f"There are still evil processes running: {result.stdout}"
    )
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


@pytest.mark.asyncio
async def test_stdin_data_async_does_not_read():
    data = "hello async\n"
    result = await run(
        ["python3", ROOT / "does_not_read.py"],
        timeout_seconds=2,
        max_output_size=1024,
        stdin_data=data,
    )
    assert result.exit_code == -1
    assert result.timeout is True
    assert len(result.stdout) == 0
    assert len(result.stderr) == 0
    await assert_no_running_evil()


@pytest.mark.asyncio
async def test_stdin_data_async_echo():
    data = "hello async\n"
    result = await run(
        ["python3", ROOT / "echo_stdin.py"],
        timeout_seconds=2,
        max_output_size=1024,
        stdin_data=data,
    )
    assert result.exit_code == 0
    assert result.timeout is False
    assert result.stdout == data
    await assert_no_running_evil()


@pytest.mark.asyncio
async def test_read_one_line():
    """
    The test program reads just one line of input, but we are trying to send
    two. The program still runs and prints. It runs for longer than the
    stdin_write_timeout, but shorter than timeout_seconds. However, we still
    get -1 as the exit_code because it did not receive the entire input.
    """
    result = await run(
        ["python3", ROOT / "read_one_line.py"],
        timeout_seconds=30,
        max_output_size=1024,
        stdin_data="Line 1\n" + ("x" * 128 * 1024),
        stdin_write_timeout=3,
    )
    assert result.exit_code == -1
    assert result.timeout is False
    assert result.stdout == "I read one line\n"
    await assert_no_running_evil()


@pytest.mark.asyncio
async def test_podman_run_stdin():
    """Test podman_run with stdin_data input."""
    data = "hello container\n"
    result = await podman_run(
        ["cat"],
        image="alpine:latest",
        timeout_seconds=5,
        max_output_size=1024,
        stdin_data=data,
    )
    assert result.exit_code == 0
    assert result.timeout is False
    assert result.stdout == data
    # stderr may contain podman pull messages, which is fine


@pytest.mark.asyncio
async def test_podman_run_sleep_forever():
    """Test podman_run with a container that runs forever (timeout scenario)."""
    result = await podman_run(
        ["python3", "-c", "import time; time.sleep(60)"],
        image="python:3",
        timeout_seconds=2,
        max_output_size=1024,
    )
    assert result.exit_code == -1
    assert result.timeout is True
    # stderr may contain podman pull messages, which is fine
    assert len(result.stdout) == 0


@pytest.mark.asyncio
async def test_podman_run_unbounded_output():
    """Test podman_run with a container that produces output forever."""
    result = await podman_run(
        ["sh", "-c", "while true; do echo 'x' | tr -d '\\n' | head -c 100; echo; done"],
        image="alpine:latest",
        timeout_seconds=3,
        max_output_size=1024,
    )
    assert result.exit_code == -1
    assert result.timeout is True
    # stderr may contain podman pull messages, which is fine
    assert len(result.stdout) == 1024

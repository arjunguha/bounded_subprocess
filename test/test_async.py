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
async def test_run_memory_limit_ok():
    result = await run(
        ["python3", "-c", "import time; x = bytearray(8 * 1024 * 1024); time.sleep(0.5)"],
        timeout_seconds=5,
        max_output_size=1024,
        memory_limit_mb=256,
        memory_watchdog_interval_seconds=0.05,
    )
    assert result.exit_code == 0
    assert result.timeout is False
    assert len(result.stderr) == 0


@pytest.mark.asyncio
async def test_run_memory_limit_exceeded():
    result = await run(
        ["python3", "-c", "import time; x = bytearray(128 * 1024 * 1024); time.sleep(5)"],
        timeout_seconds=10,
        max_output_size=1024,
        memory_limit_mb=64,
        memory_watchdog_interval_seconds=0.05,
    )
    assert result.exit_code == -1
    assert result.timeout is False


@pytest.mark.asyncio
async def test_run_memory_limit_exceeded_in_child_process():
    result = await run(
        [
            "python3",
            "-c",
            (
                "import os, time\n"
                "pid = os.fork()\n"
                "_ = bytearray(4 * 1024 * 1024)\n"
                "if pid == 0:\n"
                "    _ = bytearray(128 * 1024 * 1024)\n"
                "    time.sleep(5)\n"
                "else:\n"
                "    time.sleep(5)\n"
            ),
        ],
        timeout_seconds=10,
        max_output_size=1024,
        memory_limit_mb=64,
        memory_watchdog_interval_seconds=0.05,
    )
    assert result.exit_code == -1
    assert result.timeout is False


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


@pytest.mark.asyncio
async def test_podman_run_volumes():
    """Test podman_run with volume mounts."""
    import tempfile
    
    # Create a temporary file with test content
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as tmp_file:
        tmp_file.write("test content\n")
        tmp_path = tmp_file.name
    
    try:
        # Mount the file and read it from the container
        result = await podman_run(
            ["cat", "/mnt/test.txt"],
            image="alpine:latest",
            timeout_seconds=5,
            max_output_size=1024,
            volumes=[f"{tmp_path}:/mnt/test.txt:ro"],
        )
        assert result.exit_code == 0
        assert result.timeout is False
        assert result.stdout == "test content\n"
    finally:
        # Clean up
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_podman_run_cwd():
    """Test podman_run with working directory."""
    result = await podman_run(
        ["pwd"],
        image="alpine:latest",
        timeout_seconds=5,
        max_output_size=1024,
        cwd="/tmp",
    )
    assert result.exit_code == 0
    assert result.timeout is False
    assert result.stdout.strip() == "/tmp"


@pytest.mark.asyncio
async def test_podman_run_memory_limit_ok():
    """Test podman_run with memory limit that is sufficient."""
    result = await podman_run(
        ["echo", "hello"],
        image="alpine:latest",
        timeout_seconds=10,
        max_output_size=1024,
        memory_limit_mb=64,
    )
    assert result.exit_code == 0
    assert result.timeout is False
    assert result.stdout.strip() == "hello"


@pytest.mark.asyncio
async def test_podman_run_memory_limit_oom():
    """Test podman_run where the process exceeds the memory limit."""
    # Allocate ~128MB in a container limited to 32MB
    result = await podman_run(
        ["python3", "-c", "x = bytearray(128 * 1024 * 1024)"],
        image="python:3.12-alpine",
        timeout_seconds=30,
        max_output_size=4096,
        memory_limit_mb=32,
    )
    assert result.exit_code != 0

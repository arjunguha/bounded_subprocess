from bounded_subprocess import run
import time
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent / "evil_programs"

def assert_no_running_evil():
    result = run(
        ["pgrep", "-f", ROOT], timeout_seconds=1, max_output_size=1024
    )
    assert (
        result.exit_code == 1
    ), f"There are still evil processes running: {result.stdout}"
    assert len(result.stderr) == 0
    assert len(result.stdout) == 0


def test_trivial():
    result = run(
        ["echo", "hello"],
        timeout_seconds=1,
        max_output_size=1024,
    )
    assert result.exit_code == 0
    assert result.timeout == False
    assert result.stdout == "hello\n"
    assert result.stderr == ""


def test_fork_once():
    # The program exits cleanly and immediately. But, it forks a child that runs
    # forever.
    result = run(
        ["python3", ROOT / "fork_once.py"],
        timeout_seconds=2,
        max_output_size=1024,
    )
    assert result.exit_code == 0
    assert result.timeout == False
    assert len(result.stderr) == 0
    assert len(result.stdout) == 0
    assert_no_running_evil()


def test_close_outputs():
    # The program prints to stdout, closes its output, and then runs forever.
    result = run(
        ["python3", ROOT / "close_outputs.py"],
        timeout_seconds=2,
        max_output_size=1024,
    )
    assert result.exit_code == -1
    assert result.timeout == True
    assert len(result.stderr) == 0
    assert result.stdout == "This is the end\n"
    assert_no_running_evil()


def test_unbounded_output():
    result = run(
        ["python3", ROOT / "unbounded_output.py"],
        timeout_seconds=3,
        max_output_size=1024,
    )
    assert result.exit_code == -1
    assert result.timeout == True
    assert len(result.stderr) == 0
    assert len(result.stdout) == 1024
    assert_no_running_evil()


def test_sleep_forever():
    result = run(
        ["python3", ROOT / "sleep_forever.py"],
        timeout_seconds=2,
        max_output_size=1024,
    )
    assert result.exit_code == -1
    assert result.timeout == True
    assert len(result.stderr) == 0
    assert len(result.stdout) == 0
    assert_no_running_evil()


@pytest.mark.unsafe
def test_fork_bomb():
    result = run(
        ["python3", ROOT / "fork_bomb.py"],
        timeout_seconds=2,
        max_output_size=1024,
    )
    assert result.exit_code == -1
    assert result.timeout == True
    # We used to assert that len(result.stderr) == 0. I don't understand how
    # this could have ever been correct.
    assert len(result.stdout) == 0
    # Unfortunately, this sleep seems to be necessary. My theories:
    # 1. os.killpg doesn't block until the whole process group is dead.
    # 2. pgrep can produce stale output
    time.sleep(2)
    assert_no_running_evil()


def test_block_on_inputs():
    # We run the subprocess with /dev/null as input. So, any program that tries
    # to read input will error.
    result = run(
        ["python3", ROOT / "block_on_inputs.py"],
        timeout_seconds=2,
        max_output_size=1024,
    )
    assert result.exit_code == 1
    assert result.timeout == False
    assert len(result.stdout) == 0
    assert "EOF when reading a line" in result.stderr
    assert_no_running_evil()

def test_long_stdout():
    # We run the subprocess with a very long print message. Check that it
    # is not truncated in stdout.
    result = run(
        ["python3", ROOT / "long_stdout.py"],
        timeout_seconds=10,
        max_output_size=10000,
    )
    assert result.exit_code == 0
    assert result.timeout == False
    # leave some leeway space for encoding
    assert 9500 <= len(result.stdout) <= 10500
    assert_no_running_evil()


def test_stdin_data_does_not_read():
    data = "hello world\n"
    result = run(
        ["python3", ROOT / "does_not_read.py"],
        timeout_seconds=2,
        max_output_size=1024,
        stdin_data=data,
    )
    assert result.exit_code == -1
    assert result.timeout is True
    assert len(result.stdout) == 0
    assert len(result.stderr) == 0
    assert_no_running_evil()


def test_stdin_data_echo():
    data = "hello world\n"
    result = run(
        ["python3", ROOT / "echo_stdin.py"],
        timeout_seconds=2,
        max_output_size=1024,
        stdin_data=data,
    )
    assert result.exit_code == 0
    assert result.timeout is False
    assert result.stdout == data
    assert_no_running_evil()

def test_read_one_line():
    """
    The test program reads just one line of input, but we are trying to send 
    two. The program still runs and prints. It runs for longer than the
    stdin_write_timeout, but shorter than timeout_seconds. However, we still
    get -1 as the exit_code because it did not receive the entire input.
    """
    result = run(
        ["python3", ROOT / "read_one_line.py"],
        timeout_seconds=30,
        max_output_size=1024,
        stdin_data="Line 1\n" + ("x" * 128 * 1024),
        stdin_write_timeout=3,
    )
    assert result.exit_code == -1
    assert result.timeout is False
    assert result.stdout == "I read one line\n"
    assert_no_running_evil()
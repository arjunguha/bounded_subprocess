import time
import os
import subprocess
import uuid
from pathlib import Path
from bounded_subprocess.interactive import Interactive

ROOT = Path(__file__).resolve().parent / "evil_programs"
import pytest


def _marker_pids(marker):
    result = subprocess.run(
        ["pgrep", "-f", marker],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode == 1:
        return []
    assert result.returncode == 0, result.stderr
    return [int(line) for line in result.stdout.splitlines()]


def _kill_marker_processes(marker):
    for pid in _marker_pids(marker):
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not _marker_pids(marker):
            return
        time.sleep(0.05)
    assert _marker_pids(marker) == []


def test_does_not_read():
    p = Interactive(
        ["python3", ROOT / "does_not_read.py"],
        read_buffer_size=1,
    )
    assert p.write(b"x", timeout_seconds=1)
    # The Linux buffer size is 64KB. This should make us block, unless we
    # set timeouts appropriately.
    assert not p.write(b"x" * 128 * 1024, timeout_seconds=5)
    assert p.close(1) == -9


def test_dies_shortly_after_launch():
    p = Interactive(
        ["python3", ROOT / "dies_shortly_after_launch.py"],
        read_buffer_size=1,
    )
    # We write a large amount of data that would block. But, the child dies
    # before it reads everything we write.
    assert not p.write(b"x" * 128 * 1024, timeout_seconds=5)


@pytest.mark.timeout(5)
def test_never_writes():
    # The child program happens to read all input, but it never writes anything
    # so, the read_line() call will block indefinitely unless we set a timeout.
    p = Interactive(
        ["python3", ROOT / "block_on_inputs.py"],
        read_buffer_size=1,
    )
    assert p.read_line(timeout_seconds=3) is None


@pytest.mark.timeout(5)
def test_write_forever_but_no_newline():
    p = Interactive(
        ["python3", ROOT / "write_forever_but_no_newline.py"],
        read_buffer_size=1,
    )
    assert p.read_line(timeout_seconds=3) is None


@pytest.mark.timeout(5)
def test_dies_while_writing():
    p = Interactive(
        ["python3", ROOT / "dies_while_writing.py"],
        read_buffer_size=100,
    )
    assert p.read_line(timeout_seconds=1) == b"Will die before next newline"
    assert p.read_line(timeout_seconds=3) is None


@pytest.mark.timeout(5)
def test_dies_shortly_after_launch():
    # The child dies one second after launch. The test is potentially flaky.
    p = Interactive(
        ["python3", ROOT / "dies_shortly_after_launch.py"],
        read_buffer_size=100,
    )
    time.sleep(2)
    assert p.read_line(timeout_seconds=1) is None


@pytest.mark.timeout(5)
def test_dies_shortly_after_launch_2():
    # The child dies one second after launch. The test is potentially flaky.
    p = Interactive(
        ["python3", ROOT / "dies_shortly_after_launch.py"],
        read_buffer_size=100,
    )
    time.sleep(2)
    assert p.read_line(timeout_seconds=1) is None
    assert p.read_line(timeout_seconds=1) is None


@pytest.mark.timeout(5)
def test_close_trivial():
    p = Interactive(
        ["python3", ROOT / "sleep_forever.py"],
        read_buffer_size=1,
    )
    # -9 indicates that the child was killed with SIGKILL.
    assert p.close(1) == -9


@pytest.mark.timeout(5)
def test_close_when_child_writes_forever():
    p = Interactive(
        ["python3", ROOT / "write_forever_but_no_newline.py"],
        read_buffer_size=1,
    )
    # The child will do a non-normal exit because it fails to write. But,
    # it will not be killed by a signal.
    assert p.close(1) > 0


@pytest.mark.timeout(5)
def test_double_close():
    p = Interactive(
        ["python3", ROOT / "sleep_forever.py"],
        read_buffer_size=1,
    )
    assert p.close(1) == -9
    # The child is already dead, so this should be a no-op.
    assert p.close(1) == -9


@pytest.mark.timeout(5)
def test_cwd(tmp_path):
    p = Interactive(
        ["python3", "-u", "-c", "import os; print(os.getcwd())"],
        read_buffer_size=4096,
        cwd=str(tmp_path),
    )
    line = p.read_line(timeout_seconds=3)
    assert line is not None
    assert Path(line.decode()).resolve() == tmp_path.resolve()
    assert p.close(1) == 0


@pytest.mark.timeout(5)
def test_close_after_normal_exit():
    p = Interactive(
        ["python3", ROOT / "dies_shortly_after_launch.py"],
        read_buffer_size=1,
    )
    time.sleep(2)
    assert p.close(1) == 1


@pytest.mark.timeout(5)
def test_close_kills_forked_child_processes():
    marker = f"bounded-interactive-leak-{uuid.uuid4()}"
    p = Interactive(
        ["python3", ROOT / "fork_child_marker.py", marker],
        read_buffer_size=1024,
    )
    leaked_pids = []
    try:
        assert p.read_line(timeout_seconds=2) == b"ready"
        p.close(1)
        leaked_pids = _marker_pids(marker)
    finally:
        _kill_marker_processes(marker)

    assert leaked_pids == []

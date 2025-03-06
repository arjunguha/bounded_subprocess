import time
from pathlib import Path
from bounded_subprocess.interactive import Interactive
ROOT = Path(__file__).resolve().parent / "evil_programs"
import pytest


def test_does_not_read():
    p = Interactive(
        ["python3", ROOT / "does_not_read.py"],
        read_buffer_size=1,
    )
    assert p.write(b"x", timeout_seconds=1)
    # The Linux buffer size is 64KB. This should make us block, unless we
    # set timeouts appropriately.
    assert not p.write(b"x" * 128 * 1024, timeout_seconds=5)


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
def test_close_blocking_trivial():
    p = Interactive(
        ["python3", ROOT / "sleep_forever.py"],
        read_buffer_size=1,
    )
    # -9 indicates that the child was killed with SIGKILL.
    assert p.close_blocking(1) == -9

@pytest.mark.timeout(5)
def test_close_blocking_when_child_writes_forever():
    p = Interactive(
        ["python3", ROOT / "write_forever_but_no_newline.py"],
        read_buffer_size=1,
    )
    # The child will do a non-normal exit because it fails to write. But,
    # it will not be killed by a signal.
    assert p.close_blocking(1) > 0

@pytest.mark.timeout(5)
def test_double_close():
    p = Interactive(
        ["python3", ROOT / "sleep_forever.py"],
        read_buffer_size=1,
    )
    assert p.close_blocking(1) == -9
    # The child is already dead, so this should be a no-op.
    assert p.close_blocking(1) == -9

@pytest.mark.timeout(5)
def test_close_after_normal_exit():
    p = Interactive(
        ["python3", ROOT / "dies_shortly_after_launch.py"],
        read_buffer_size=1,
    )
    time.sleep(2)
    assert p.close_blocking(1) == 1

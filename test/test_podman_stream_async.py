from contextlib import aclosing
import time
import uuid

import pytest

from bounded_subprocess.bounded_subprocess_async import podman_run_stream_lines, run


async def assert_no_running_podman_command(marker: str):
    """
    Assert that no currently running podman container command contains marker.

    The streaming cleanup tests put a UUID marker into the container's shell
    command. Checking `podman ps --no-trunc` for that marker verifies that the
    specific container launched by the test is gone, without making assumptions
    about other containers that may be running on the developer's machine.
    """
    result = await run(
        ["podman", "ps", "--no-trunc", "--format", "{{.Command}}"],
        timeout_seconds=5,
        max_output_size=8192,
    )
    assert result.exit_code == 0
    assert marker not in result.stdout


@pytest.mark.asyncio
async def test_podman_stream_lines_yields_strings_from_string_stdin():
    """
    The streaming Podman API should use text at its public boundary.

    This test sends ordinary Python `str` input to a real Alpine container that
    uppercases stdin with `tr`. It then consumes the async generator to EOF and
    verifies that each yielded line is a `str`, not bytes, and that line breaks
    are handled as streaming record separators rather than being included in the
    returned values.
    """
    lines = [
        line
        async for line in podman_run_stream_lines(
            ["sh", "-c", "tr '[:lower:]' '[:upper:]'"],
            image="alpine:latest",
            timeout_seconds=5,
            max_line_size=1024,
            stdin_data="one\ntwo\n",
        )
    ]

    assert lines == ["ONE", "TWO"]
    assert all(isinstance(line, str) for line in lines)


@pytest.mark.asyncio
async def test_podman_stream_lines_can_be_closed_early_and_cleans_unbounded_stream():
    """
    Closing the async generator should stop an unbounded output stream.

    The container runs `while true` and emits one marked line every 50ms. The
    reader intentionally consumes only three lines and then exits the async
    generator through `contextlib.aclosing`. If generator finalization does not
    kill and remove the container, the command would keep streaming forever in
    the background. The final `podman ps` check verifies that this test's marked
    container is no longer running.
    """
    marker = f"bounded-stream-test-{uuid.uuid4()}"
    lines = []

    async with aclosing(
        podman_run_stream_lines(
            [
                "sh",
                "-c",
                (
                    f"i=0; "
                    f"while true; do "
                    f"echo {marker}-$i; "
                    f"i=$((i + 1)); "
                    f"sleep 0.05; "
                    f"done"
                ),
            ],
            image="alpine:latest",
            timeout_seconds=30,
            max_line_size=1024,
        )
    ) as stream:
        async for line in stream:
            lines.append(line)
            if len(lines) == 3:
                break

    assert lines == [f"{marker}-0", f"{marker}-1", f"{marker}-2"]

    await assert_no_running_podman_command(marker)


@pytest.mark.asyncio
@pytest.mark.timeout(8)
async def test_podman_stream_lines_no_output_times_out_and_cleans_container():
    """
    Reading from a silent container must have a hard upper bound.

    The container starts successfully but never writes a stdout line; it only
    sleeps forever. Awaiting `anext(stream)` is the dangerous operation here,
    because a broken implementation could wait indefinitely for the first line.
    The stream should instead hit `timeout_seconds`, raise `StopAsyncIteration`,
    and run the same cleanup path used by early-close tests.
    """
    marker = f"bounded-stream-test-{uuid.uuid4()}"

    async with aclosing(
        podman_run_stream_lines(
            [
                "sh",
                "-c",
                f"marker={marker}; while true; do sleep 1; done",
            ],
            image="alpine:latest",
            timeout_seconds=1,
            max_line_size=1024,
        )
    ) as stream:
        start = time.monotonic()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        elapsed = time.monotonic() - start

    assert 0.8 <= elapsed < 4
    await assert_no_running_podman_command(marker)


@pytest.mark.asyncio
@pytest.mark.timeout(8)
async def test_podman_stream_lines_extra_read_after_finite_output_does_not_block():
    """
    Asking for one more line after finite output should observe EOF promptly.

    The container prints exactly two newline-terminated lines and exits. The
    first two `anext` calls should return those lines. The third `anext` call
    models a caller expecting N+1 lines from a command that produced only N; it
    should raise `StopAsyncIteration` quickly rather than blocking until the
    stream's wall-clock timeout.
    """
    async with aclosing(
        podman_run_stream_lines(
            ["sh", "-c", "printf 'one\\ntwo\\n'"],
            image="alpine:latest",
            timeout_seconds=5,
            max_line_size=1024,
        )
    ) as stream:
        assert await anext(stream) == "one"
        assert await anext(stream) == "two"

        start = time.monotonic()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        elapsed = time.monotonic() - start

    assert elapsed < 2


@pytest.mark.asyncio
async def test_podman_stream_lines_bounds_long_lines():
    """
    A line longer than `max_line_size` should not accumulate unbounded memory.

    The container writes a single six-byte line, while the stream is configured
    to retain at most three bytes of a pending line. This matches the existing
    interactive reader's policy: keep the most recent bytes and discard older
    bytes until a newline arrives.
    """
    lines = [
        line
        async for line in podman_run_stream_lines(
            ["sh", "-c", "printf 'abcdef\\n'"],
            image="alpine:latest",
            timeout_seconds=5,
            max_line_size=3,
        )
    ]

    assert lines == ["def"]

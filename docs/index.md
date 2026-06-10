# bounded_subprocess

## Why this library exists

Python's `subprocess` module is enough until the child process misbehaves.
A buggy program can:

- spawn grandchildren that outlive the parent's `timeout`;
- block on stdin and never read what you wrote;
- print gigabytes to stdout and exhaust the parent's memory before you ever
  call `.communicate()`.

`bounded_subprocess` is a small wrapper around `subprocess` that adds three
hard bounds:

1. **Process-group cleanup.** The child runs in its own session, so on
   timeout we kill the entire process group — including anything it forked.
2. **Bounded output capture.** We keep at most `max_output_size` bytes from
   each of stdout and stderr — the prefix by default, the suffix when
   `tail=True` — and discard the rest.
3. **Wall-clock timeout.** The run has a hard bound while collecting output and
   waiting for the child to exit. If you pass `stdin_data`, writing stdin has
   its own `stdin_write_timeout`; set it no larger than your desired overall
   deadline if that matters for your caller.

This is *not* isolation: the child can still touch the filesystem, the
network, or escape into a new session of its own. If you need isolation,
`podman_run` runs the same interface inside a container.

The library comes in several flavors, all built on the same primitives:

| Function / class | When to reach for it |
| --- | --- |
| [`run`](#bounded_subprocess.bounded_subprocess.run) | One-shot synchronous call. |
| [`run` (async)](#bounded_subprocess.bounded_subprocess_async.run) | The same, but `await`-able, and with an optional memory watchdog. |
| [`Interactive`](#bounded_subprocess.interactive.Interactive) | A long-lived child you talk to line by line. |
| [`podman_run`](#bounded_subprocess.bounded_subprocess_async.podman_run) | Async execution inside a podman container. |
| [`podman_run_stream_lines`](#bounded_subprocess.bounded_subprocess_async.podman_run_stream_lines) | Async line streaming from a podman container. |

## Quickstart

### Run a command synchronously

```python
from bounded_subprocess import run

result = run(["echo", "hello"], timeout_seconds=5)
print(result.exit_code)        # 0
print(result.stdout.strip())   # 'hello'
```

### Run a command asynchronously

```python
import asyncio
from bounded_subprocess.bounded_subprocess_async import run

async def main():
    result = await run(
        ["bash", "-lc", "echo ok; echo err 1>&2"],
        timeout_seconds=5,
    )
    print(result.exit_code, result.stdout.strip(), result.stderr.strip())

asyncio.run(main())
```

### Talk to a long-running child

```python
from bounded_subprocess.interactive import Interactive

proc = Interactive(["python3", "-iu"], read_buffer_size=4096)
proc.write(b"print(1 + 2)\n", timeout_seconds=1)
print(proc.read_line(timeout_seconds=1))  # b'3'
proc.close(nice_timeout_seconds=1)
```

### Run a command in a container

```python
import asyncio
from bounded_subprocess.bounded_subprocess_async import podman_run

async def main():
    result = await podman_run(
        ["cat"],
        image="alpine:latest",
        timeout_seconds=5,
        max_output_size=1024,
        stdin_data="hello\n",
    )
    print(result.stdout)

asyncio.run(main())
```

### Stream lines from a container

```python
import asyncio
from contextlib import aclosing
from bounded_subprocess.bounded_subprocess_async import podman_run_stream_lines

async def main():
    async with aclosing(podman_run_stream_lines(
        ["sh", "-c", "printf '%s\n' one two three"],
        image="alpine:latest",
        timeout_seconds=5,
        max_line_size=1024,
    )) as lines:
        async for line in lines:
            print(line)
            break

asyncio.run(main())
```

Each entry point takes plenty of additional knobs (working directory,
environment, stdin, memory limit, container volumes, …); see the reference
below.

## Timing contract for `run`

`timeout_seconds` bounds output collection and the final wait for the launched
child process. Two edge cases are worth knowing:

- If `stdin_data` is supplied, the write phase is controlled by
  `stdin_write_timeout` (default 15 seconds), not preempted by
  `timeout_seconds`.
- If a descendant process inherits stdout or stderr, the output pipe can remain
  open after the direct child exits. In that case `run` may wait until
  `timeout_seconds` before killing the process group, even if the direct child
  has already exited.

## API reference

### Synchronous execution

::: bounded_subprocess.bounded_subprocess.run

### Asynchronous execution

::: bounded_subprocess.bounded_subprocess_async.run

::: bounded_subprocess.bounded_subprocess_async.podman_run

::: bounded_subprocess.bounded_subprocess_async.podman_run_stream_lines

### Interactive execution

::: bounded_subprocess.interactive.Interactive

::: bounded_subprocess.interactive_async.Interactive

### Result type

::: bounded_subprocess.util.Result

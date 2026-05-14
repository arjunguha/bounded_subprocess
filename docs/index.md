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

1. **Process-group cleanup.** The child is started in a new session, so on
   timeout we can kill the entire process group — including anything it
   forked.
2. **Bounded output capture.** Stdout and stderr are each truncated to
   `max_output_size` bytes (the prefix by default, or the suffix if you set
   `tail=True`).
3. **Wall-clock timeout.** A single deadline governs the run, regardless of
   how the child is behaving on its pipes.

This is *not* isolation: the child can still touch the filesystem, the
network, or escape into a new session of its own. If you need isolation,
`podman_run` runs the same interface inside a container.

The library comes in four flavors, all built on the same primitives:

| Function / class | When to reach for it |
| --- | --- |
| [`run`](#bounded_subprocess.bounded_subprocess.run) | One-shot synchronous call. |
| [`run` (async)](#bounded_subprocess.bounded_subprocess_async.run) | The same, but `await`-able, and with an optional memory watchdog. |
| [`Interactive`](#bounded_subprocess.interactive.Interactive) | A long-lived child you talk to line by line. |
| [`podman_run`](#bounded_subprocess.bounded_subprocess_async.podman_run) | Async execution inside a podman container. |

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

Each entry point takes plenty of additional knobs (working directory,
environment, stdin, memory limit, container volumes, …); see the reference
below.

## API reference

### Synchronous execution

::: bounded_subprocess.bounded_subprocess.run

### Asynchronous execution

::: bounded_subprocess.bounded_subprocess_async.run

::: bounded_subprocess.bounded_subprocess_async.podman_run

### Interactive execution

::: bounded_subprocess.interactive.Interactive

::: bounded_subprocess.interactive_async.Interactive

### Result type

::: bounded_subprocess.util.Result

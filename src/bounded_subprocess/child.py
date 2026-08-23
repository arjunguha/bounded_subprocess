"""
A child process we can always let go of: kill the group, reap it, close the pipes.
"""

import asyncio
import logging
import os
import signal
import subprocess
import time
from typing import List, Optional

from .util import MAX_BYTES_PER_READ, set_nonblocking

logger = logging.getLogger(__name__)

RELEASE_TIMEOUT_SECONDS = 5.0
_REAP_POLL_SECONDS = 0.01


class Child:
    """
    A subprocess in its own session, and the operations for abandoning it.

    Spawning with `start_new_session=True` makes the child's pid its process
    group id, and that pgid is the handle for killing descendants the child
    leaves behind. Keeping the two together is the point of this class: a pgid
    recorded without the new session, or a kill that forgets the session, is a
    leaked process tree.

    This class holds no policy. It does not know about deadlines, output
    limits, or why it is being released -- callers decide that. What it
    guarantees is that releasing is safe to call from an `except` or `finally`
    block: every step is idempotent and swallows its own failures, so releasing
    a child never adds a second exception to the one already in flight.

    Reaping and releasing come in `_sync` and `_async` flavors because waiting
    is the one operation that cannot be shared between a blocking caller and an
    event loop. The rest is common to both.
    """

    def __init__(self, popen: subprocess.Popen, process_group_id: int) -> None:
        self.popen = popen
        self.process_group_id = process_group_id

    @classmethod
    def spawn(
        cls,
        args: List[str],
        *,
        env=None,
        cwd: Optional[str] = None,
        stdin: bool = False,
        stderr: bool = True,
    ) -> "Child":
        """
        Start `args` in a new session with its pipes nonblocking.

        Pass `stdin=True` for a writable stdin pipe; otherwise stdin is
        `/dev/null`, so a child that reads gets EOF instead of hanging. Pass
        `stderr=False` to let the child inherit our stderr instead of piping
        it, for callers that only care about stdout.
        """
        popen = subprocess.Popen(
            args,
            env=env,
            cwd=cwd,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if stderr else None,
            start_new_session=True,
            bufsize=MAX_BYTES_PER_READ,
        )
        # The child has not been waited for, so its /proc entry is still there
        # even if it has already exited.
        child = cls(popen, os.getpgid(popen.pid))
        for pipe in (popen.stdin, popen.stdout, popen.stderr):
            if pipe is not None:
                set_nonblocking(pipe)
        return child

    @property
    def stdin(self):
        return self.popen.stdin

    @property
    def stdout(self):
        return self.popen.stdout

    @property
    def stderr(self):
        return self.popen.stderr

    def poll(self) -> Optional[int]:
        """The child's exit code, or None while it is still running."""
        return self.popen.poll()

    def kill_group(self) -> None:
        """
        `SIGKILL` the whole process group.

        Worth doing even when the direct child has already exited: a descendant
        may still be running, and may still be holding the output pipes open.
        """
        try:
            os.killpg(self.process_group_id, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def close_stdin(self) -> None:
        """
        Close stdin to signal end of input.

        Distinct from `close_pipes`, which is cleanup: this one is meaningful to
        the child, which is expected to see EOF and act on it.
        """
        if self.popen.stdin is None:
            return
        try:
            self.popen.stdin.close()
        except (BrokenPipeError, BlockingIOError):
            pass

    def close_pipes(self) -> None:
        """
        Close our ends of the child's pipes.

        Closing stdin flushes it, which fails once the child is dead; closing
        after an interrupted write means there are buffered bytes to fail on.
        That is expected here, so each close swallows it -- releasing a child
        must not raise.
        """
        for pipe in (self.popen.stdin, self.popen.stdout, self.popen.stderr):
            if pipe is None:
                continue
            try:
                pipe.close()
            except (BrokenPipeError, BlockingIOError, ValueError, OSError):
                pass

    def reap_sync(self, timeout_seconds: float = RELEASE_TIMEOUT_SECONDS) -> bool:
        """
        Wait for the killed child to be reaped. Returns False if it outlasts
        the timeout.

        `SIGKILL` makes a process exit; it does not remove it. The kernel keeps
        the exit status until the parent waits, and this process *is* the
        parent: nothing else will reap it. `Popen.poll` reaps via
        `waitpid(WNOHANG)`, so polling is enough. A child stuck in an
        uninterruptible syscall can outlast the timeout, which is why this
        reports failure rather than waiting forever.
        """
        deadline = time.time() + timeout_seconds
        while self.poll() is None:
            if time.time() >= deadline:
                return False
            time.sleep(_REAP_POLL_SECONDS)
        return True

    async def reap_async(
        self, timeout_seconds: float = RELEASE_TIMEOUT_SECONDS
    ) -> bool:
        """
        Async counterpart of `reap_sync`.

        Polling beats `asyncio.to_thread(popen.wait)` here: giving up on the
        latter abandons a thread parked in `waitpid`, and the default executor
        has only so many threads to lose.
        """
        deadline = time.time() + timeout_seconds
        while self.poll() is None:
            if time.time() >= deadline:
                return False
            await asyncio.sleep(_REAP_POLL_SECONDS)
        return True

    def release_sync(self, timeout_seconds: float = RELEASE_TIMEOUT_SECONDS) -> None:
        """
        Let go of the child completely: kill its group, reap it, close its pipes.

        Killing precedes reaping on purpose. Reaping frees the child's pid, and
        the pgid *is* that pid, so a group killed after the reap is a group id
        that the kernel may already have handed to someone else.
        """
        self.kill_group()
        reaped = self.reap_sync(timeout_seconds)
        self.close_pipes()
        self._warn_if_unreaped(reaped, timeout_seconds)

    async def release_async(
        self, timeout_seconds: float = RELEASE_TIMEOUT_SECONDS
    ) -> None:
        """Async counterpart of `release_sync`, in the same order."""
        self.kill_group()
        reaped = await self.reap_async(timeout_seconds)
        self.close_pipes()
        self._warn_if_unreaped(reaped, timeout_seconds)

    def _warn_if_unreaped(self, reaped: bool, timeout_seconds: float) -> None:
        if not reaped:
            logger.warning(
                "child %d survived SIGKILL for %ss and was left unreaped",
                self.popen.pid,
                timeout_seconds,
            )

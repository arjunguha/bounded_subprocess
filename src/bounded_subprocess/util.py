import subprocess
import os
import fcntl
import signal

MAX_BYTES_PER_READ = 1024
SLEEP_BETWEEN_READS = 0.1


class Result:
    timeout: int
    exit_code: int
    stdout: str
    stderr: str

    def __init__(self, timeout, exit_code, stdout, stderr):
        self.timeout = timeout
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def set_nonblocking(reader):
    fd = reader.fileno()
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)


class BoundedSubprocessState:
    """
    This class lets us share code between the synchronous and asynchronous
    implementations.
    """

    def __init__(self, args, env, max_output_size):
        """
        Start the process in a new session.
        """
        p = subprocess.Popen(
            args,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=MAX_BYTES_PER_READ,
        )
        set_nonblocking(p.stdout)
        set_nonblocking(p.stderr)

        self.process_group_id = os.getpgid(p.pid)
        self.p = p
        self.exit_code = None
        self.stdout_saved_bytes = []
        self.stderr_saved_bytes = []
        self.stdout_bytes_read = 0
        self.stderr_bytes_read = 0
        self.max_output_size = max_output_size

    def try_read(self) -> bool:
        """
        Reads from the process. Returning False indicates that we should stop
        reading.
        """
        this_stdout_read = self.p.stdout.read(MAX_BYTES_PER_READ)
        this_stderr_read = self.p.stderr.read(MAX_BYTES_PER_READ)
        # this_stdout_read and this_stderr_read may be None if stdout or stderr
        # are closed. Without these checks, test_close_output fails.
        if (
            this_stdout_read is not None
            and self.stdout_bytes_read < self.max_output_size
        ):
            self.stdout_saved_bytes.append(this_stdout_read)
            self.stdout_bytes_read += len(this_stdout_read)
        if (
            this_stderr_read is not None
            and self.stderr_bytes_read < self.max_output_size
        ):
            self.stderr_saved_bytes.append(this_stderr_read)
            self.stderr_bytes_read += len(this_stderr_read)

        self.exit_code = self.p.poll()
        if self.exit_code is not None:
            # Finish reading output if needed.
            left_to_read = self.max_output_size - self.stdout_bytes_read
            if left_to_read <= 0:
                return False
            this_stdout_read = self.p.stdout.read(left_to_read)
            this_stderr_read = self.p.stderr.read(left_to_read)
            if this_stdout_read is not None:
                self.stdout_saved_bytes.append(this_stdout_read)
            if this_stderr_read is not None:
                self.stderr_saved_bytes.append(this_stderr_read)
            return False
        return True

    def terminate(self) -> Result:
        try:
            # Kills the process group. Without this line, test_fork_once fails.
            os.killpg(self.process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass

        timeout = self.exit_code is None
        exit_code = self.exit_code if self.exit_code is not None else -1
        stdout = b"".join(self.stdout_saved_bytes).decode("utf-8", errors="ignore")
        stderr = b"".join(self.stderr_saved_bytes).decode("utf-8", errors="ignore")
        return Result(
            timeout=timeout, exit_code=exit_code, stdout=stdout, stderr=stderr
        )

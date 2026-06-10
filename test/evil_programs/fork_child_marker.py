import subprocess
import sys
import time

marker = sys.argv[1]

subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import sys, time; marker = sys.argv[1]; time.sleep(60)",
        marker,
    ]
)
print("ready", flush=True)
time.sleep(60)

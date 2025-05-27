import time
import os
deadline = time.time() + 1
print("Will die before next newline", flush=True)
while time.time() < deadline:
    print("x" * 1024, end="", flush=True)
os.exit(1)

"""
Sleep, then read stdin until the bytes END appear, then print how many times
the bytes MARKER appeared in everything read so far.

The initial sleep lets the parent fill the stdin pipe to capacity while the
child is guaranteed not to be draining it. The final count tells the parent
exactly how many copies of a payload were actually delivered, which exposes
writes that are silently dropped or duplicated.
"""

import sys
import time

time.sleep(4)
data = b""
while b"END" not in data:
    chunk = sys.stdin.buffer.read1(65536)
    if not chunk:
        break
    data += chunk
print(data.count(b"MARKER"), flush=True)
time.sleep(60)

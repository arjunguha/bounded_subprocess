"""
Shared `/proc` helpers for tests that assert on processes we should have killed.

We read `/proc` directly rather than shelling out to `pgrep`: spawning a
process triggers `subprocess._cleanup`, which reaps abandoned children and
would erase the zombies these assertions look for.
"""

import os
from typing import List, Optional, Set, Tuple


def live_pids_matching(marker: str) -> List[int]:
    """
    Pids whose command line contains `marker`. Zombies have an empty command
    line, so they never appear here.
    """
    needle = marker.encode()
    pids = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            with open(f"/proc/{entry.name}/cmdline", "rb") as f:
                cmdline = f.read()
        except OSError:
            continue
        if needle in cmdline:
            pids.append(int(entry.name))
    return pids


def read_state_and_parent(pid: int) -> Optional[Tuple[str, int]]:
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
            stat = f.read()
    except OSError:
        return None
    right_paren = stat.rfind(")")
    if right_paren == -1:
        return None
    # After "comm)", the fields are: state, ppid, ...
    rest = stat[right_paren + 1 :].split()
    if len(rest) < 2:
        return None
    try:
        return rest[0], int(rest[1])
    except ValueError:
        return None


def zombie_children() -> Set[int]:
    """Pids of this process's unreaped children."""
    me = os.getpid()
    zombies = set()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        state_and_parent = read_state_and_parent(int(entry.name))
        if state_and_parent is None:
            continue
        state, ppid = state_and_parent
        if state == "Z" and ppid == me:
            zombies.add(int(entry.name))
    return zombies


def open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))

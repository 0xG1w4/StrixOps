"""Process identity checks: never send a signal using a recycled PID."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _darwin_info(pid: int) -> tuple[str, bool] | None:
    """Use the process birth timestamp, including microseconds, on macOS."""
    if sys.platform != "darwin":
        return None
    import ctypes

    class BsdInfo(ctypes.Structure):
        _fields_ = (
            [
                (name, ctypes.c_uint32)
                for name in (
                    "flags",
                    "status",
                    "xstatus",
                    "pid",
                    "ppid",
                    "uid",
                    "gid",
                    "ruid",
                    "rgid",
                    "svuid",
                    "svgid",
                    "rfu",
                )
            ]
            + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
            + [(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "pjobc", "e_tdev", "e_tpgid", "nice")]
            + [("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]
        )

    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        function = library.proc_pidinfo
        function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        function.restype = ctypes.c_int
        info = BsdInfo()
        size = ctypes.sizeof(info)
        if function(pid, 3, 0, ctypes.byref(info), size) != size or info.pid != pid or not info.start_sec:
            return None
        return f"darwin:{info.start_sec}:{info.start_usec}", info.status != 5
    except (OSError, AttributeError):
        return None


def process_identity(pid: int) -> str:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return ""
    try:
        darwin = _darwin_info(pid)
        if darwin is not None:
            return darwin[0] if darwin[1] else ""
        if Path("/proc/self/stat").exists():
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                return ""
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return f"{boot}:{fields[19]}"
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=", "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        value = result.stdout.strip().rsplit(None, 1)
        if result.returncode or len(value) != 2 or value[1].startswith("Z"):
            return ""
        return value[0]
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return ""


def process_alive(pid: int | None, identity: str = "") -> bool | None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    darwin = _darwin_info(pid)
    if darwin is not None:
        return darwin[1] and darwin[0] == identity if identity else None
    actual = process_identity(pid)
    if actual:
        return actual == identity if identity else None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return None
    # Zombies are no longer executing, including unreaped Popen children.
    try:
        if Path(f"/proc/{pid}/stat").exists():
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if state == "Z":
                return False
        else:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if result.stdout.strip().startswith("Z"):
                return False
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return None

"""Physical CPU cores, for the size of onnxruntime's thread pools."""

from __future__ import annotations

import ctypes
import os
import struct
import sys
from pathlib import Path

# One thread per physical core is fastest: on an 8-core, 16-thread laptop the name model took
# 0.51 s per 2 KB at 8 threads and 1.19 s at 16, and OCR was 55% slower at 16. Two threads on
# one core share its arithmetic units, so a second thread per core only adds contention.


def physical_cores() -> int:
    """Physical cores this process can use, or 0 when the system will not say.

    0 leaves the choice to onnxruntime, which also counts physical cores on the platforms it
    knows; stating it keeps a future default (or a logical count) from doubling the time.
    """
    try:
        if sys.platform == "win32":
            return _windows()
        if sys.platform == "darwin":
            return _sysctl(b"hw.physicalcpu")
        return _linux()
    except (OSError, ValueError, AttributeError, struct.error):
        return 0


def _windows() -> int:
    relation_processor_core = 0
    get_info = ctypes.windll.kernel32.GetLogicalProcessorInformationEx
    size = ctypes.c_ulong(0)
    get_info(relation_processor_core, None, ctypes.byref(size))  # asks for the buffer size
    buffer = ctypes.create_string_buffer(size.value)
    if not get_info(relation_processor_core, buffer, ctypes.byref(size)):
        return 0
    count, offset = 0, 0
    while offset < size.value:  # one variable-size record per core: (relationship, size, ...)
        relationship, record = struct.unpack_from("<II", buffer, offset)
        count += relationship == relation_processor_core
        offset += record
    return count


def _sysctl(name: bytes) -> int:
    value, size = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
    if ctypes.CDLL(None).sysctlbyname(name, ctypes.byref(value), ctypes.byref(size), None, 0):
        return 0
    return value.value


def _linux() -> int:
    """Distinct cores among the CPUs this process may run on: hyper-threads list the same siblings."""
    topology = Path("/sys/devices/system/cpu")
    cores = {(topology / f"cpu{cpu}" / "topology" / "thread_siblings_list").read_text().strip()
             for cpu in os.sched_getaffinity(0)}
    return len(cores)

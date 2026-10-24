"""
Windows Job Object limits for the device process (Windows only).

A Job Object is the Windows way to put hard limits on a group of processes.
The structures below mirror the ones in the Windows API documentation.
"""

import ctypes
import os
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateJobObjectW.restype = wintypes.HANDLE

JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x0008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x0100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4
EXTENDED_LIMIT_INFORMATION = 9
CPU_RATE_CONTROL_INFORMATION = 15


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", BasicLimits),
        ("IoInfo", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class CpuRateControl(ctypes.Structure):
    _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]


class MemoryCounters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            "PeakWorkingSetSize", "WorkingSetSize",
            "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
            "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
            "PagefileUsage", "PeakPagefileUsage",
        )
    ]


class Job:
    def __init__(self, handle):
        self.handle = handle

    def close(self):
        # Closing the last handle kills the device (KILL_ON_JOB_CLOSE).
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None


def _check(ok):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def apply(process, limits):
    """Put a started process into a new Job Object with the device limits."""
    job = Job(kernel32.CreateJobObjectW(None, None))
    _check(job.handle)

    info = ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    info.BasicLimitInformation.ActiveProcessLimit = 1
    info.ProcessMemoryLimit = limits["memory_mb"] * 1024 * 1024
    _check(kernel32.SetInformationJobObject(
        job.handle, EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)))

    # CpuRate is in 1/100 of a percent of the WHOLE machine, so divide the
    # share of one core by the number of cores.
    cpu = CpuRateControl()
    cpu.ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
    cpu.CpuRate = max(1, round(limits["cpu_percent"] * 100 / os.cpu_count()))
    _check(kernel32.SetInformationJobObject(
        job.handle, CPU_RATE_CONTROL_INFORMATION, ctypes.byref(cpu), ctypes.sizeof(cpu)))

    # subprocess keeps the process handle in the private _handle attribute.
    _check(kernel32.AssignProcessToJobObject(job.handle, wintypes.HANDLE(int(process._handle))))
    return job


def peak_working_set(process):
    """Peak resident memory (working set) of a process, in bytes."""
    counters = MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    _check(kernel32.K32GetProcessMemoryInfo(
        wintypes.HANDLE(int(process._handle)), ctypes.byref(counters), counters.cb))
    return counters.PeakWorkingSetSize

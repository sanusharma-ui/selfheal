"""Standalone process-tree owner; also embedded by the Rust verifier.

No SDK imports or app initialization. Windows descendants inherit a job whose
last handle lives only in this worker. Killing the worker closes that handle.
"""
from __future__ import annotations

import os
import subprocess
import sys
from threading import Thread
import time

_job = None


def python_worker_command(*arguments):
    """Keep Popen.pid equal to the worker PID, including Windows virtualenvs.

    CPython's Windows venv executable is a redirector process. Starting the
    underlying interpreter with its launcher marker preserves the venv while
    making parent verification and kill-on-close cleanup target the real worker.
    """
    environment = os.environ.copy()
    executable = sys.executable
    if os.name == "nt":
        executable = getattr(sys, "_base_executable", None) or sys.executable
        environment["__PYVENV_LAUNCHER__"] = sys.executable
    return [executable, *arguments], environment


def contain_current_process():
    global _job
    parent = os.getppid()

    def parent_watch():
        while True:
            time.sleep(0.1)
            if os.getppid() != parent:
                if os.name != "nt" and os.getpgrp() == os.getpid():
                    import signal
                    os.killpg(os.getpid(), signal.SIGKILL)
                os._exit(124)

    if os.name != "nt":
        # The launcher starts the worker in a fresh process group/session.
        Thread(target=parent_watch, daemon=True).start()
        return
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = Extended()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if (not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())):
        error = ctypes.get_last_error()
        kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    _job = job  # Deliberately retained until OS process teardown, never inherited.
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    owner_handle = kernel.OpenProcess(0x00100000, False, parent)
    if not owner_handle:
        raise ctypes.WinError(ctypes.get_last_error())

    def windows_parent_watch():
        result = kernel.WaitForSingleObject(owner_handle, 0xFFFFFFFF)
        # Owner exit, or monitoring failure: stop this owned job, never continue
        # unchecked after the application disappears.
        os._exit(124 if result == 0 else 125)

    Thread(target=windows_parent_watch, daemon=True).start()


def main():
    contain_current_process()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.call([sys.executable, *sys.argv[1:]], creationflags=flags)


if __name__ == "__main__":
    raise SystemExit(main())

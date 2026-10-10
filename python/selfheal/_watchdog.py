"""Independent fail-stop guard. Never imported by an application.

Only this owning process may be stopped. Timeout recovery is compare-before-write:
restore the retained snapshot only if source is still the exact candidate.
Normal cancellation/EOF does not implement general forced-kill crash recovery.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from threading import Event, Lock, Thread
import time


def _restore(record):
    if record is None:
        return
    root = Path(record["root"]).resolve(strict=True)
    target = Path(record["target"]).resolve(strict=True)
    target.relative_to(root)
    if hashlib.sha256(target.read_bytes()).hexdigest() != record["candidate_hash"]:
        return  # Source was never replaced, already restored, or externally edited.
    snapshot = Path(record["snapshot"]).resolve(strict=True)
    snapshot_root = Path(record["snapshot_root"]).resolve(strict=True)
    snapshot_root.relative_to(root)
    snapshot.relative_to(snapshot_root)
    original = snapshot.read_bytes()
    if hashlib.sha256(original).hexdigest() != record["original_hash"]:
        raise RuntimeError("Recovery snapshot hash changed")
    descriptor, temporary = tempfile.mkstemp(prefix=".selfheal-timeout-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(original)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, target.stat().st_mode)
        if hashlib.sha256(target.read_bytes()).hexdigest() != record["candidate_hash"]:
            return
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    initial = json.loads(sys.stdin.buffer.readline(16_001))
    owner = initial["pid"]
    if type(owner) is not int or owner != os.getppid():
        raise RuntimeError("Watchdog owner does not match its parent")
    hard_at = float(initial["hard_at"])
    # Retain a process handle to prevent killing a reused PID on Windows.
    kernel = handle = None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100001, False, owner)  # SYNCHRONIZE | TERMINATE
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
    stopped, changed, lock = Event(), Event(), Lock()
    state = {"recovery": None, "worker": None}

    def forget_worker():
        worker = state["worker"]
        if worker is not None and os.name == "nt":
            kernel.CloseHandle(worker[1])
        state["worker"] = None

    def stop_worker():
        worker = state["worker"]
        if worker is None:
            return
        try:
            if os.name == "nt":
                if kernel.WaitForSingleObject(worker[1], 0) != 0:
                    kernel.TerminateProcess(worker[1], 124)
                    kernel.WaitForSingleObject(worker[1], 2000)
            else:
                try:
                    os.killpg(worker[0], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            forget_worker()

    def control():
        try:
            while True:
                line = sys.stdin.buffer.readline(16_001)
                if not line:
                    stopped.set()
                    return
                data = json.loads(line)
                with lock:
                    if data.get("cancel") is True:
                        stopped.set()
                    elif "recovery" in data:
                        state["recovery"] = data["recovery"]
                    elif "worker_pid" in data:
                        forget_worker()
                        worker_pid = data["worker_pid"]
                        if type(worker_pid) is int and worker_pid > 0:
                            worker_handle = kernel.OpenProcess(0x00100001, False, worker_pid) if os.name == "nt" else None
                            if os.name != "nt" or worker_handle:
                                state["worker"] = (worker_pid, worker_handle)
                changed.set()
        except Exception:
            # A malformed channel cannot silently disarm the deadline.
            changed.set()

    Thread(target=control, daemon=True).start()
    sys.stdout.buffer.write(b"READY\n")
    sys.stdout.buffer.flush()
    try:
        while not stopped.is_set():
            remaining = hard_at - time.monotonic()
            if remaining <= 0:
                break
            changed.wait(min(remaining, 0.05))
            changed.clear()
        with lock:
            if stopped.is_set():
                stop_worker()  # Never leave a tracked worker behind on cancellation/EOF.
                return
            record = state["recovery"]
            print("SelfHeal: hard repair deadline exceeded; stopping the owning application (exit 124). Restart required.",
                  file=sys.stderr, flush=True)
            if os.name == "nt":
                if kernel.WaitForSingleObject(handle, 0) == 0:
                    return  # Owner already exited, never act on a reused PID.
                if not kernel.TerminateProcess(handle, 124):
                    raise OSError("Could not stop watchdog owner")
                if kernel.WaitForSingleObject(handle, 5000) != 0:
                    raise OSError("Owner termination could not be confirmed; source left untouched")
            else:
                if os.getppid() != owner:
                    return
                os.kill(owner, signal.SIGKILL)
                end = time.monotonic() + 5
                while os.getppid() == owner and time.monotonic() < end:
                    time.sleep(0.01)
                if os.getppid() == owner:
                    raise OSError("Owner termination could not be confirmed; source left untouched")
            stop_worker()
        try:
            _restore(record)
        except Exception:
            print("SelfHeal: timeout recovery could not restore source; inspect the retained snapshot before restarting.",
                  file=sys.stderr, flush=True)
    finally:
        with lock:
            stop_worker()
        if handle:
            kernel.CloseHandle(handle)


if __name__ == "__main__":
    main()

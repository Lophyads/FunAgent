"""Bounded output and process-tree cleanup, including after the parent exits."""
import ctypes
from ctypes import wintypes
import json
import os
import signal
import subprocess
import sys
from threading import Thread
import time

from ...frame.contracts import ControlError


class WindowsJob:
    def __init__(self, process):
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL)):
            function = getattr(self._api, name)
            function.argtypes, function.restype = args, result
        self._handle = self._api.CreateJobObjectW(None, None)
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if (not self._api.SetInformationJobObject(self._handle, 9, ctypes.byref(info), ctypes.sizeof(info))
                    or not self._api.AssignProcessToJobObject(self._handle, int(process._handle))):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self._api.CloseHandle(self._handle)
            raise

    def Close(self):
        if self._handle:
            self._api.TerminateJobObject(self._handle, 1)
            if not self._api.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None


def RunProcess(command, workdir, context, timeout, max_output=16000):
    context.CheckCancelled()
    inherited = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE",
        "APPDATA", "LOCALAPPDATA", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"}}
    process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "src.tool.backends.process_worker"],
        cwd=workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=inherited, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        start_new_session=os.name != "nt")
    job, readers, outputs, sizes, interrupted = None, [], [bytearray(), bytearray()], [0, 0], None
    def drain(stream, index):
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                sizes[index] += len(chunk)
                outputs[index].extend(chunk[:max(0, max_output - len(outputs[index]))])
        finally:
            stream.close()
    try:
        if os.name == "nt":
            job = WindowsJob(process)
        for index, stream in enumerate((process.stdout, process.stderr)):
            reader = Thread(target=drain, args=(stream, index), name="funagent-process-output", daemon=True)
            reader.start()
            readers.append(reader)
        context.CheckCancelled()
        process.stdin.write(json.dumps(command).encode("utf8") + b"\n")
        process.stdin.close()
        deadline = time.monotonic() + min(timeout, context.Remaining())
        while process.poll() is None:
            try:
                context.CheckCancelled()
            except ControlError as error:
                interrupted = error.code
                break
            if time.monotonic() >= deadline:
                interrupted = "PROCESS_TIMEOUT"
                break
            context.cancel.wait(0.02)
    finally:
        if job is not None:
            job.Close()
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        for reader in readers:
            reader.join(5)
            if reader.is_alive():
                raise RuntimeError("Process output did not drain")
        for stream in (process.stdout, process.stderr):
            if not stream.closed:
                stream.close()
    return {"exit_code": process.returncode, "stdout": outputs[0].decode("utf8", "replace"),
            "stderr": outputs[1].decode("utf8", "replace"), "truncated": any(n > max_output for n in sizes),
            "interrupted": interrupted}

"""Private, bounded evidence from the exact Popen child before its cleanup."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import stat
import threading
import time
import uuid

STDERR_LIMIT = 64 * 1024
STDERR_DRAIN_LIMIT = 256 * 1024
NATIVE_LOG_LIMIT = 2 * 1024 * 1024
RECEIPT_LIMIT = 128


class ChildExitDiagnostics:
    def __init__(self, directory: str | None):
        self.directory = Path(directory) if directory else None
        self._lock = threading.Lock()
        self._process = None
        self._identity = None
        self._stderr = b""
        self._stderr_eof = False
        self._stderr_discarded = 0
        self._drain_limit_reached = False
        self._native_log = None
        self._sequence = 0
        self.error = None
        if self.directory is not None:
            if not self.directory.is_absolute():
                raise ValueError("child exit diagnostics directory must be absolute")
            fd = self._open_directory()
            os.close(fd)

    def _open_directory(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(fd)
            raise ValueError("child exit diagnostics requires an owned private directory")
        return fd

    def bind(self, process):
        if self.directory is None:
            return
        with self._lock:
            self._process = process
            self._identity = {"pid": process.pid, "instance_id": uuid.uuid4().hex,
                              "started_utc": datetime.now(timezone.utc).isoformat(),
                              "started_monotonic": time.monotonic()}
            self._stderr = b""
            self._stderr_eof = False
            self._stderr_discarded = 0
            self._drain_limit_reached = False
            self._native_log = None
        self.observe(process, "spawned")

    def _drain_stderr(self, process):
        stream = process.stderr
        if stream is None or self._stderr_eof:
            return
        fd = stream.fileno()
        blocking = os.get_blocking(fd)
        try:
            os.set_blocking(fd, False)
            # read1 also preserves bytes already buffered by BufferedReader.
            drained = 0
            while drained < STDERR_DRAIN_LIMIT:
                block = stream.read1(min(4096, STDERR_DRAIN_LIMIT - drained))
                if block is None:
                    break
                if not block:
                    # BufferedReader.read1 can also return empty on EAGAIN.
                    # Only a raw empty read proves EOF.
                    block = os.read(fd, 1)
                    if not block:
                        self._stderr_eof = True
                        break
                drained += len(block)
                retained = block[:max(0, STDERR_LIMIT + 1 - len(self._stderr))]
                self._stderr += retained
                self._stderr_discarded += len(block) - len(retained)
            self._drain_limit_reached = drained == STDERR_DRAIN_LIMIT
        except BlockingIOError:
            pass
        finally:
            os.set_blocking(fd, blocking)

    @staticmethod
    def _write(directory_fd, name, data):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fd)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())

    def _preserve_native_log(self, directory_fd, path):
        if path is None or self._native_log is not None:
            return
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        with os.fdopen(fd, "rb") as native:
            info = os.fstat(native.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise ValueError("unbound native diagnostic log")
            offset = max(0, info.st_size - NATIVE_LOG_LIMIT)
            native.seek(offset)
            data = native.read(NATIVE_LOG_LIMIT)
        name = self._identity["instance_id"] + "-native.log"
        self._write(directory_fd, name, data)
        self._native_log = {"file": name, "sha256": hashlib.sha256(data).hexdigest(),
                            "bytes": len(data), "original_size": info.st_size,
                            "tail_offset": offset, "truncated": offset > 0}

    def observe(self, process, reason, *, planned_stop=False, native_log=None):
        if self.directory is None:
            return
        with self._lock:
            try:
                if process is not self._process or process.pid != self._identity["pid"]:
                    raise ValueError("child exit evidence is not bound to the spawned Popen object")
                if self._sequence >= RECEIPT_LIMIT:
                    raise ValueError("child exit diagnostic receipt limit reached")
                returncode = process.poll()
                if returncode is not None or reason == "request_transport_failure":
                    # A fatal handler can still be writing before the process
                    # exits. Bounded nonblocking drainage prevents its pipe
                    # from hiding that exit; alive is still reported honestly.
                    self._drain_stderr(process)
                signal_number = -returncode if returncode is not None and returncode < 0 else None
                try:
                    signal_name = signal.Signals(signal_number).name if signal_number else None
                except ValueError:
                    signal_name = "unknown"
                stderr = self._stderr[:STDERR_LIMIT]
                directory_fd = self._open_directory()
                try:
                    if returncode is not None:
                        self._preserve_native_log(directory_fd, native_log)
                    record = {**self._identity, "observed_utc": datetime.now(timezone.utc).isoformat(),
                              "observed_monotonic": time.monotonic(), "reason": reason,
                              "planned_stop_requested": planned_stop,
                              "returncode_source": "bound Popen.poll()", "returncode": returncode,
                              "signal_number": signal_number, "signal_name": signal_name,
                              "child_alive": returncode is None,
                              "stderr_base64": base64.b64encode(stderr).decode("ascii"),
                              "stderr_utf8": stderr.decode("utf-8", errors="replace"),
                              "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                              "stderr_available": process.stderr is not None,
                              "stderr_bytes": len(stderr), "stderr_eof": self._stderr_eof,
                              "stderr_truncated": len(self._stderr) > STDERR_LIMIT,
                              "stderr_discarded_bytes": self._stderr_discarded,
                              "stderr_drain_limit_reached": self._drain_limit_reached,
                              "native_log": self._native_log, "cause": "unclassified"}
                    name = f"{self._identity['instance_id']}-{self._sequence:03d}-{reason}.json"
                    self._write(directory_fd, name, (json.dumps(record, ensure_ascii=False) + "\n").encode())
                    self._sequence += 1
                finally:
                    os.close(directory_fd)
            except Exception as error:
                # Evidence failure never changes playback, teardown or recovery.
                self.error = type(error).__name__ + ": " + str(error)
                logging.getLogger("uvicorn.error").warning("Child exit evidence capture failed: %s", self.error)

    @property
    def stderr(self):
        return self._stderr[:STDERR_LIMIT]

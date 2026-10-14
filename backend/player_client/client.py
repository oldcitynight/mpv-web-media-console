from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from player_host.protocol import Envelope, ProtocolError
from .exit_diagnostics import ChildExitDiagnostics
from .gbm_selection import GBMSelectionError, child_environment


class PlayerHostError(RuntimeError):
    """Raised when the player-host process or transport is unavailable."""


class PlayerHostCommandRejected(PlayerHostError):
    """The private host returned a definite synchronous native rejection."""


class ProbeTransportError(PlayerHostError):
    """Fixed probe-only failure; no ordinary exit diagnostics or raw messages."""
    def __init__(self, reason, *, submitted=False):
        self.reason = reason
        self.submitted = submitted
        super().__init__(reason)


class PlayerHostProcess:
    """Own and communicate with a dedicated player-host child process."""

    def __init__(
        self,
        *,
        socket_path: Path | None = None,
        python_executable: str | None = None,
        startup_timeout: float = 5.0,
        request_timeout: float = 2.0,
        capture_audio_decoder_diagnostics: bool = False,
    ) -> None:
        self._socket_path = socket_path
        self._python = python_executable or sys.executable
        self._startup_timeout = startup_timeout
        self._request_timeout = request_timeout
        if not isinstance(capture_audio_decoder_diagnostics, bool):
            raise PlayerHostError("audio-decoder diagnostics switch must be boolean")
        self._capture_audio_decoder_diagnostics = capture_audio_decoder_diagnostics
        self._process: subprocess.Popen[bytes] | None = None
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._diagnostic_log_directory: tempfile.TemporaryDirectory[str] | None = None
        self._owns_socket = socket_path is None
        self._pid: int | None = None
        self._exit_diagnostics = ChildExitDiagnostics(os.environ.get("MPV_WEBD_PLAYER_EXIT_DIAGNOSTICS_DIR"))
        self._native_diagnostic_log: Path | None = None

    @property
    def socket_path(self) -> Path:
        if self._socket_path is None:
            raise PlayerHostError("player-host has not been started")
        return self._socket_path

    @property
    def pid(self) -> int:
        if self._pid is None:
            raise PlayerHostError("player-host has not been started")
        return self._pid

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> "PlayerHostProcess":
        if self.running:
            raise PlayerHostError("player-host is already running")
        try:
            environment = child_environment(os.environ)
        except (GBMSelectionError, OSError) as error:
            raise PlayerHostError("managed child GBM profile validation failed") from error
        if self._owns_socket and self._temporary_directory is None:
            self._temporary_directory = tempfile.TemporaryDirectory(prefix="mpv-player-host-")
            self._socket_path = Path(self._temporary_directory.name) / "player.sock"
        if self._socket_path is None:
            raise PlayerHostError("player-host socket path is not configured")
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.parent.chmod(0o700)
        if self.socket_path.exists():
            self.socket_path.unlink()
        repository_root = Path(__file__).resolve().parents[2]
        environment["MPV_WEBD_AUDIO_DECODER_DIAGNOSTICS"] = (
            "1" if self._capture_audio_decoder_diagnostics else "0"
        )
        if self._capture_audio_decoder_diagnostics:
            environment["MPV_VERBOSE"] = "2"
            if self._owns_socket:
                diagnostic_directory = self.socket_path.parent
            else:
                self._diagnostic_log_directory = tempfile.TemporaryDirectory(
                    prefix="mpv-player-host-diagnostics-"
                )
                diagnostic_directory = Path(self._diagnostic_log_directory.name)
            diagnostic_log_file = diagnostic_directory / "audio-decoder.log"
            self._native_diagnostic_log = diagnostic_log_file
            if diagnostic_log_file.exists() or diagnostic_log_file.is_symlink():
                raise PlayerHostError("refusing to reuse an audio-decoder diagnostic log")
            environment["MPV_WEBD_AUDIO_DECODER_LOG_FILE"] = str(diagnostic_log_file)
        else:
            environment.pop("MPV_WEBD_AUDIO_DECODER_LOG_FILE", None)
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(repository_root)
            if not existing_pythonpath
            else f"{repository_root}{os.pathsep}{existing_pythonpath}"
        )
        try:
            self._process = subprocess.Popen(
                [self._python, "-m", "player_host.host_process", "--socket", str(self.socket_path)],
                cwd=repository_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
        except Exception:
            if self._diagnostic_log_directory is not None:
                self._diagnostic_log_directory.cleanup()
                self._diagnostic_log_directory = None
            raise
        self._pid = self._process.pid
        self._exit_diagnostics.bind(self._process)
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self._observe_exit("startup_exit")
                raise PlayerHostError(self._process_error())
            if self.socket_path.exists():
                try:
                    self.ping()
                    return self
                except (PlayerHostError, OSError):
                    pass
            time.sleep(0.01)
        self.shutdown(force=True)
        raise PlayerHostError("timed out waiting for private player-host socket")

    def request(self, kind: str, payload: Mapping[str, Any] | None = None) -> Envelope:
        return self._request(kind, payload, timeout=self._request_timeout)

    def observe_probe_phase(self):
        return self._probe_request("observe-probe-phase")

    def observe_probe_performance(self, *, before_send=None):
        return self._probe_request("observe-probe-performance", before_send=before_send)

    def observe_probe_boundary(self, *, before_send=None):
        return self._probe_request("observe-probe-boundary", before_send=before_send)

    def _probe_request(self, kind, *, before_send=None):
        # Separate fixed transport: do not call _request/_observe_exit, whose
        # ordinary lifecycle receipts can read/fsync process diagnostics.
        if kind not in {"observe-probe-phase", "observe-probe-performance", "observe-probe-boundary"}:
            raise ProbeTransportError("invalid_probe_operation")
        process = self._process
        if process is None or process.poll() is not None or process.pid != self.pid:
            raise ProbeTransportError("owner_unavailable")
        started = time.monotonic_ns(); deadline = started + 250_000_000
        submitted = False
        def budget(connection):
            remaining = deadline - time.monotonic_ns()
            if remaining <= 0:
                raise ProbeTransportError("probe_deadline", submitted=submitted)
            connection.settimeout(remaining / 1_000_000_000)
        request = Envelope(1, uuid.uuid4().hex, kind, {})
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                budget(connection)
                connection.connect(str(self.socket_path))
                credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                pid, uid, gid = struct.unpack("3i", credentials)
                if (pid, uid, gid) != (process.pid, os.getuid(), os.getgid()):
                    raise ProbeTransportError("peer_mismatch")
                if self._process is not process or process.poll() is not None or self.pid != pid:
                    raise ProbeTransportError("owner_changed")
                if before_send is not None:
                    before_send()
                budget(connection)
                submitted = True
                connection.sendall(request.encode())
                data = bytearray()
                while True:
                    budget(connection)
                    chunk = connection.recv(min(4096, 16385 - len(data)))
                    if not chunk:
                        raise ProbeTransportError("probe_response_incomplete", submitted=True)
                    data.extend(chunk)
                    if len(data) > 16384:
                        raise ProbeTransportError("probe_response_limit", submitted=True)
                    if b"\n" in data:
                        if data.count(b"\n") != 1 or not data.endswith(b"\n"):
                            raise ProbeTransportError("probe_response_invalid", submitted=True)
                        break
                budget(connection)  # Aggregate deadline includes all chunks.
                response = Envelope.decode(bytes(data))
                finished = time.monotonic_ns()
                if finished > deadline:
                    raise ProbeTransportError("probe_deadline", submitted=True)
                if response.request_id != request.request_id:
                    raise ProbeTransportError("probe_response_invalid", submitted=True)
                if response.kind == "error":
                    raise ProbeTransportError("probe_remote_error", submitted=True)
                expected_kind = "probe-phase" if kind == "observe-probe-phase" else "performance"
                if response.kind != expected_kind:
                    raise ProbeTransportError("probe_response_invalid", submitted=True)
                if self._process is not process or process.poll() is not None:
                    raise ProbeTransportError("owner_changed", submitted=True)
                return replace(response, payload={
                    **response.payload, "probe_client_started_monotonic_ns": started,
                    "probe_client_finished_monotonic_ns": finished,
                })
        except ProbeTransportError:
            raise
        except (OSError, ProtocolError, ValueError) as error:
            raise ProbeTransportError("probe_transport_failed", submitted=submitted) from error

    def _request(self, kind: str, payload: Mapping[str, Any] | None, *, timeout: float) -> Envelope:
        if not self.running:
            self._observe_exit("request_already_stopped")
            raise PlayerHostError("player-host is not running")
        request = Envelope(1, uuid.uuid4().hex, kind, dict(payload or {}))
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout)
                connection.connect(str(self.socket_path))
                connection.sendall(request.encode())
                response = Envelope.decode(connection.makefile("rb").readline())
        except (OSError, ProtocolError) as error:
            self._observe_exit("request_transport_failure")
            raise PlayerHostError(f"player-host transport failure: {error}") from error
        if response.request_id != request.request_id:
            raise PlayerHostError("player-host response request_id mismatch")
        if response.kind == "error":
            if response.payload.get('native_outcome') == 'rejected':
                raise PlayerHostCommandRejected(str(response.payload.get('message', 'native command rejected')))
            raise PlayerHostError(str(response.payload.get("message", "player-host error")))
        return response

    def ping(self) -> Envelope:
        return self.request("ping")

    def initialize(self, options: Mapping[str, Any] | None = None) -> Envelope:
        return self.request("create", options)

    supports_native_episode = True

    def load(self, path: str, *, queue_intent_id: int | None = None) -> Envelope:
        payload = {"path": path}
        if queue_intent_id is not None:
            payload['queue_intent_id'] = queue_intent_id
        return self.request("load", payload)

    def play(self) -> Envelope:
        return self.request("play")

    def pause(self) -> Envelope:
        return self.request("pause")

    def resume(self) -> Envelope:
        return self.request("resume")

    def seek_absolute(self, seconds: float) -> Envelope:
        return self.request("seek-absolute", {"seconds": seconds})

    def seek_relative(self, seconds: float) -> Envelope:
        return self.request("seek-relative", {"seconds": seconds})

    def stop(self) -> Envelope:
        return self.request("stop")

    def set_volume(self, volume: float) -> Envelope:
        return self.request("set-volume", {"volume": volume})

    def set_speed(self, speed: float) -> Envelope:
        return self.request("set-speed", {"speed": speed})

    def select_track(self, track_id: int, track_type: str) -> Envelope:
        return self.request(
            "select-track",
            {"track_id": track_id, "type": track_type},
        )

    def snapshot(self) -> Envelope:
        return self.request("snapshot")

    def observe_restart_options(self, option_names: list[str] | tuple[str, ...]) -> Mapping[str, Any]:
        response = self.request(
            "observe-restart-options",
            {"option_names": list(option_names)},
        )
        observed = response.payload.get("options")
        if not isinstance(observed, Mapping):
            raise PlayerHostError("player-host restart-option readback is invalid")
        return observed

    def observe_drm_mode(self) -> Mapping[str, Any]:
        response = self.request("observe-drm-mode")
        if response.kind != "drm-mode" or not isinstance(response.payload.get("observation"), Mapping):
            raise PlayerHostError("player-host DRM timing response is invalid")
        return response.payload["observation"]

    def observe_native_video_capabilities(self) -> Mapping[str, Any]:
        """Request only the owned host's fixed native video capability record."""
        response = self.request("observe-native-video-capabilities")
        if response.kind != "native-video-capabilities" or not isinstance(response.payload.get("capabilities"), Mapping):
            raise PlayerHostError("player-host native video capability response is invalid")
        return response.payload["capabilities"]

    def observe_hwdec_current(self) -> str | None:
        """Read the one fixed active-decoder property; callers cannot name properties."""

        response = self.request("observe-hwdec-current")
        if response.kind != "hwdec-current":
            raise PlayerHostError("player-host active-decoder response is invalid")
        value = response.payload.get("value")
        if value is not None and not isinstance(value, str):
            raise PlayerHostError("player-host active-decoder value is invalid")
        return value

    def observe_audio_decoder(self) -> Mapping[str, Any]:
        """Read the fixed, parameterless decoder-policy diagnostics record."""

        response = self.request("observe-audio-decoder")
        if response.kind != "audio-decoder":
            raise PlayerHostError("player-host audio-decoder response is invalid")
        value = response.payload
        selected = value.get("selected_decoder")
        unrecognized_hash = value.get("unrecognized_decoder_name_sha256")
        if (
            not isinstance(value.get("enabled"), bool)
            or any(
                isinstance(value.get(name), bool)
                or not isinstance(value.get(name), int)
                or value.get(name) < 0
                for name in (
                    "log_events_seen",
                    "decoder_selection_marker_events_seen",
                    "audio_decoder_prefix_events_seen",
                    "diagnostic_log_file_bytes_seen",
                )
            )
            or value.get("diagnostic_log_file_state")
            not in ("disabled", "waiting", "active", "invalid")
            or (value.get("path") is not None and not isinstance(value.get("path"), str))
            or (selected is not None and selected not in ("orender", "lavc"))
            or (
                unrecognized_hash is not None
                and (
                    not isinstance(unrecognized_hash, str)
                    or len(unrecognized_hash) != 64
                    or any(character not in "0123456789abcdef" for character in unrecognized_hash)
                )
            )

        ):
            raise PlayerHostError("player-host audio-decoder diagnostics are invalid")
        return dict(value)

    def shutdown(self, *, force: bool = False) -> None:
        process = self._process
        if process is None:
            if self._diagnostic_log_directory is not None:
                self._diagnostic_log_directory.cleanup()
                self._diagnostic_log_directory = None
            return
        self._observe_exit("before_shutdown", planned_stop=True)
        try:
            if not force and process.poll() is None:
                self.request("shutdown")
            try:
                process.wait(timeout=self._request_timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=self._request_timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=self._request_timeout)
        finally:
            self._observe_exit("before_cleanup", planned_stop=True)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            self._process = None
            if self._socket_path is not None and self._socket_path.exists():
                self._socket_path.unlink()
            if self._temporary_directory is not None:
                self._temporary_directory.cleanup()
                self._temporary_directory = None
                if self._owns_socket:
                    self._socket_path = None
            if self._diagnostic_log_directory is not None:
                self._diagnostic_log_directory.cleanup()
                self._diagnostic_log_directory = None

    def _process_error(self) -> str:
        if self._process is None:
            return "player-host process unavailable"
        if self._exit_diagnostics.directory is not None:
            error = self._exit_diagnostics.stderr.decode("utf-8", errors="replace")
        else:
            error = self._process.stderr.read().decode("utf-8", errors="replace") if self._process.stderr else ""
        return error.strip() or f"player-host exited with code {self._process.returncode}"

    def _observe_exit(self, reason, *, planned_stop=False):
        if self._process is not None:
            self._exit_diagnostics.observe(self._process, reason, planned_stop=planned_stop,
                                           native_log=self._native_diagnostic_log)

    def __enter__(self) -> "PlayerHostProcess":
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.shutdown()

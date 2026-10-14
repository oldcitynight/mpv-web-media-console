from __future__ import annotations

import math
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from .options import (
    DEFAULT_OPTION_CATALOG,
    OptionCatalog,
    OptionValidationError,
    OptionValueType,
)


class RestartablePlayerHost(Protocol):
    def snapshot(self) -> Any: ...
    def observe_restart_options(self, option_names: list[str] | tuple[str, ...]) -> Mapping[str, Any]: ...
    def shutdown(self, *, force: bool = False) -> None: ...
    def start(self) -> Any: ...
    def initialize(self, options: Mapping[str, Any] | None = None) -> Any: ...
    def load(self, path: str) -> Any: ...
    def seek_absolute(self, seconds: float) -> Any: ...
    def select_track(self, track_id: int, track_type: str) -> Any: ...
    def pause(self) -> Any: ...
    def resume(self) -> Any: ...


class RestartApplicationError(RuntimeError):
    """A requested reconfiguration or deterministic playback restore failed."""


@dataclass(frozen=True, slots=True)
class SelectedTrack:
    track_id: int
    track_type: str


@dataclass(frozen=True, slots=True)
class PlaybackSnapshot:
    path: str | None
    position: float | None
    selected_tracks: tuple[SelectedTrack, ...]
    paused: bool

    @classmethod
    def from_observed(cls, values: Mapping[str, Any]) -> "PlaybackSnapshot":
        if not isinstance(values, Mapping):
            raise RestartApplicationError("player snapshot is invalid")
        path = values.get("path")
        if path is not None and (not isinstance(path, str) or not path or "\x00" in path):
            raise RestartApplicationError("player snapshot path is invalid")
        raw_position = values.get("time-pos")
        if raw_position is None:
            position = None
        elif (
            isinstance(raw_position, bool)
            or not isinstance(raw_position, (int, float))
            or not math.isfinite(float(raw_position))
            or raw_position < 0
        ):
            raise RestartApplicationError("player snapshot position is invalid")
        else:
            position = float(raw_position)
        paused = values.get("pause", False)
        if not isinstance(paused, bool):
            raise RestartApplicationError("player snapshot pause state is invalid")

        raw_tracks = values.get("track-list", ())
        if not isinstance(raw_tracks, (list, tuple)):
            raise RestartApplicationError("player snapshot track list is invalid")
        selected: list[SelectedTrack] = []
        for track in raw_tracks:
            if not isinstance(track, Mapping):
                continue
            if track.get("selected") is not True:
                continue
            track_id = track.get("id")
            track_type = track.get("type")
            if (
                isinstance(track_id, bool)
                or not isinstance(track_id, int)
                or track_id < 0
                or track_type not in {"audio", "sub", "video"}
            ):
                raise RestartApplicationError("selected track metadata is invalid")
            selected.append(SelectedTrack(track_id, track_type))
        selected.sort(key=lambda track: ({"audio": 0, "sub": 1, "video": 2}[track.track_type], track.track_id))
        types = [track.track_type for track in selected]
        if len(types) != len(set(types)):
            raise RestartApplicationError("snapshot contains multiple selected tracks of one type")
        return cls(path, position, tuple(selected), paused)


@dataclass(frozen=True, slots=True)
class RestartApplicationReport:
    observed_options: Mapping[str, bool | int | float | str]
    pending_restart_options: Mapping[str, bool | int | float | str]
    playback_snapshot: PlaybackSnapshot

    @property
    def applied_options(self) -> Mapping[str, bool | int | float | str]:
        """Compatibility alias for options confirmed by runtime readback."""

        return self.observed_options


class ProfileRestartApplier:
    """Rebuild the owned player host and deterministically restore playback."""

    def __init__(
        self,
        player_host: RestartablePlayerHost,
        *,
        catalog: OptionCatalog = DEFAULT_OPTION_CATALOG,
        restore_timeout: float = 5.0,
        poll_interval: float = 0.025,
    ) -> None:
        if restore_timeout <= 0 or poll_interval <= 0:
            raise ValueError("restore timing values must be positive")
        self._player_host = player_host
        self._catalog = catalog
        self._restore_timeout = restore_timeout
        self._poll_interval = poll_interval

    def apply(
        self,
        restart_options: Mapping[str, Any],
        *,
        base_options: Mapping[str, Any] | None = None,
    ) -> RestartApplicationReport:
        if not isinstance(restart_options, Mapping):
            raise TypeError("restart options must be a mapping")
        validated: dict[str, bool | int | float | str] = {}
        for name, raw_value in restart_options.items():
            try:
                definition = self._catalog.require(name)
                if not definition.restart_required:
                    raise OptionValidationError("option does not require a restart")
                validated[name] = definition.validate(raw_value)
            except OptionValidationError as error:
                raise RestartApplicationError("restart option is not catalog-valid") from error

        base = dict(base_options or {})
        current_snapshot = PlaybackSnapshot.from_observed(_payload(self._player_host.snapshot()))
        try:
            self._rebuild_and_restore(_merged_options(base, validated), current_snapshot)
        except Exception as error:
            raise RestartApplicationError(
                "profile restart or playback restoration failed"
            ) from error

        try:
            observed = self._player_host.observe_restart_options(tuple(validated))
        except Exception as error:
            raise RestartApplicationError(
                "restarted player could not verify profile options"
            ) from error
        if not isinstance(observed, Mapping):
            raise RestartApplicationError("restart-option readback is invalid")
        observed_options: dict[str, bool | int | float | str] = {}
        pending_options: dict[str, bool | int | float | str] = {}
        for name, requested in validated.items():
            actual = observed.get(name)
            definition = self._catalog.require(name)
            if option_value_matches(definition, requested, actual):
                observed_options[name] = actual
            else:
                pending_options[name] = requested

        return RestartApplicationReport(
            observed_options=MappingProxyType(observed_options),
            pending_restart_options=MappingProxyType(pending_options),
            playback_snapshot=current_snapshot,
        )

    def _rebuild_and_restore(
        self,
        initialization_options: Mapping[str, Any],
        snapshot: PlaybackSnapshot,
    ) -> None:
        self._player_host.shutdown()
        self._player_host.start()
        self._player_host.initialize(initialization_options)

        expected_tracks = {(track.track_id, track.track_type) for track in snapshot.selected_tracks}
        if snapshot.path is not None:
            self._player_host.load(snapshot.path)
            self._wait_for(
                lambda state: state.get("path") == snapshot.path
                and expected_tracks <= _available_track_keys(state.get("track-list")),
                "media and selected tracks did not become available",
            )
            for track in snapshot.selected_tracks:
                self._player_host.select_track(track.track_id, track.track_type)
            if snapshot.position is not None and snapshot.position > 0:
                self._player_host.seek_absolute(snapshot.position)
            if snapshot.paused:
                self._player_host.pause()
            else:
                self._player_host.resume()
        elif snapshot.paused:
            self._player_host.pause()

        self._wait_for(lambda state: _restored(state, snapshot), "playback state did not restore")

    def _wait_for(self, predicate, reason: str) -> None:
        deadline = time.monotonic() + self._restore_timeout
        while True:
            try:
                state = _payload(self._player_host.snapshot())
                if predicate(state):
                    return
            except Exception:
                pass
            if time.monotonic() >= deadline:
                raise RestartApplicationError(reason)
            time.sleep(self._poll_interval)


def _payload(response: Any) -> Mapping[str, Any]:
    payload = getattr(response, "payload", response)
    if not isinstance(payload, Mapping):
        raise RestartApplicationError("player-host snapshot payload is invalid")
    return payload


def _merged_options(
    base: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    return {**base, **profile}


def _available_track_keys(raw_tracks: Any) -> set[tuple[int, str]]:
    if not isinstance(raw_tracks, (list, tuple)):
        return set()
    return {
        (track["id"], track["type"])
        for track in raw_tracks
        if isinstance(track, Mapping)
        and isinstance(track.get("id"), int)
        and not isinstance(track.get("id"), bool)
        and isinstance(track.get("type"), str)
    }


def _restored(state: Mapping[str, Any], snapshot: PlaybackSnapshot) -> bool:
    if state.get("path") != snapshot.path or state.get("pause", False) is not snapshot.paused:
        return False
    if snapshot.position is not None:
        position = state.get("time-pos")
        if (
            isinstance(position, bool)
            or not isinstance(position, (int, float))
            or not math.isfinite(float(position))
            or abs(float(position) - snapshot.position) > 0.5
        ):
            return False
    selected = {
        (track["id"], track["type"])
        for track in state.get("track-list", ())
        if isinstance(track, Mapping) and track.get("selected") is True
    }
    return {(track.track_id, track.track_type) for track in snapshot.selected_tracks} <= selected


def option_value_matches(definition, requested: Any, observed: Any) -> bool:
    """Compare a typed requested option with a fixed runtime readback value."""

    value_type = definition.value_type
    if value_type is OptionValueType.BOOLEAN:
        return isinstance(observed, bool) and observed is requested
    if value_type is OptionValueType.INTEGER:
        return (
            isinstance(observed, int)
            and not isinstance(observed, bool)
            and observed == requested
        )
    if value_type is OptionValueType.NUMBER:
        return (
            isinstance(observed, (int, float))
            and not isinstance(observed, bool)
            and math.isfinite(float(observed))
            and abs(float(observed) - float(requested)) <= 0.001
        )
    return isinstance(observed, str) and observed == requested

"""Fixed, private native-position receipts for semantic playback boundaries."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


class PositionCheckpointError(ValueError):
    pass


class CheckpointReceiptRejected(PositionCheckpointError):
    """Deterministic pre-write rejection; never a persistence failure."""


def native_seek_context(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Actual pre-command context; a superseded seek may still be moving.

    This context cannot produce a receipt: both post-command reads must pass
    native_position, which requires an explicitly settled native position.
    """
    if not isinstance(snapshot, Mapping):
        raise PositionCheckpointError("native snapshot is not an object")
    path = snapshot.get("path")
    if type(path) is not str or not path or "\x00" in path:
        raise PositionCheckpointError("native media is unavailable")
    position = _number(snapshot.get("time-pos"))
    raw_duration = snapshot.get("duration")
    duration = None if raw_duration is None else _number(raw_duration)
    if type(snapshot.get("seeking")) is not bool or snapshot.get("idle-active") is not False:
        raise PositionCheckpointError("native position is not settled")
    if snapshot.get("eof-reached") is True:
        raise PositionCheckpointError("native media has ended")
    pause = snapshot.get("pause")
    if not isinstance(pause, bool):
        raise PositionCheckpointError("native pause is unavailable")
    return {"path": path, "position": position, "duration": duration, "pause": pause}


def native_position(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Accept only a settled fixed snapshot, with an explicit current media."""
    values = native_seek_context(snapshot)
    if snapshot.get("seeking") is not False:
        raise PositionCheckpointError("native position is not settled")
    return values


def verified_position(before: Mapping[str, Any], first: Mapping[str, Any],
                      second: Mapping[str, Any], *, origin: str,
                      before_generation: int, first_generation: int,
                      second_generation: int, requested_seconds: float | None = None) -> dict[str, Any]:
    """Verify actual values; requested seconds are a check, never a stored value."""
    if type(origin) is not str or origin not in {"pause", "seek-absolute", "seek-relative"}:
        raise PositionCheckpointError("unsupported checkpoint origin")
    if any(type(g) is not int or g < 0 for g in
           (before_generation, first_generation, second_generation)):
        raise PositionCheckpointError("invalid native generation")
    if first_generation != second_generation or first_generation < before_generation:
        raise PositionCheckpointError("native generation changed")
    old = native_position(before) if origin == "pause" else native_seek_context(before)
    one, two = native_position(first), native_position(second)
    if one["path"] != old["path"] or two["path"] != old["path"]:
        raise PositionCheckpointError("native media changed")
    expected_pause = True if origin == "pause" else old["pause"]
    if one["pause"] != expected_pause or two["pause"] != expected_pause:
        raise PositionCheckpointError("native pause did not settle")
    if origin == "pause":
        if first_generation != before_generation:
            raise PositionCheckpointError("pause generation changed")
    else:
        if first_generation <= before_generation:
            raise PositionCheckpointError("seek generation is stale")
        requested = _number(requested_seconds, signed=origin == "seek-relative")
        expected = requested if origin == "seek-absolute" else max(0.0, old["position"] + requested)
        # Existing end-of-media policy will clear a verified terminal position.
        if two["duration"] is not None:
            expected = min(expected, two["duration"])
        if any(abs(row["position"] - expected) > 0.5 for row in (one, two)):
            raise PositionCheckpointError("seek target did not settle")
    if abs(one["position"] - two["position"]) > (0.05 if expected_pause else 0.5):
        raise PositionCheckpointError("native position changed during verification")
    return two


def validate_checkpoint_event(event: Any) -> None:
    proof = event.payload.get("position_checkpoint")
    if not isinstance(proof, Mapping) or set(proof) != {
        "origin", "player_id", "session_id", "path", "position", "duration", "pause",
        "seeking", "generation_before", "generation_after",
    }:
        raise PositionCheckpointError("invalid position checkpoint receipt")
    state = event.payload.get("state", {})
    props = state.get("properties", {}) if isinstance(state, Mapping) else {}
    if not isinstance(state, Mapping) or not isinstance(props, Mapping):
        raise PositionCheckpointError("checkpoint state is not an object")
    # Receipt scalars are untrusted JSON. Check types before membership or
    # identity comparison; malformed containers must become typed rejection.
    if any(type(value) is not str or not value or "\x00" in value for value in
           (proof["origin"], proof["player_id"], proof["session_id"],
            state.get("player_id"), state.get("session_id"))):
        raise PositionCheckpointError("invalid checkpoint identity scalar")
    if (proof["origin"] not in {"pause", "seek-absolute", "seek-relative"}
            or proof["player_id"] != event.player_id or proof["session_id"] != event.session_id
            or state.get("player_id") != event.player_id or state.get("session_id") != event.session_id
            or type(proof["generation_before"]) is not int or proof["generation_before"] < 0
            or type(proof["generation_after"]) is not int
            or proof["generation_before"] != proof["generation_after"]):
        raise PositionCheckpointError("checkpoint identity or generation mismatch")
    parsed = native_position({"path": proof["path"], "time-pos": proof["position"],
                              "duration": proof["duration"], "pause": proof["pause"],
                              "seeking": proof["seeking"], "idle-active": False})
    native_position({"path": props.get("path"), "time-pos": props.get("time_pos"),
                     "duration": props.get("duration"), "pause": props.get("pause"),
                     "seeking": False, "idle-active": False})
    if any(props.get(name) != parsed[key] for name, key in
           (("path", "path"), ("time_pos", "position"), ("duration", "duration"), ("pause", "pause"))):
        raise PositionCheckpointError("checkpoint values differ from native receipt")
    if proof["origin"] == "pause" and parsed["pause"] is not True:
        raise PositionCheckpointError("pause checkpoint is not paused")


def _number(value: Any, *, signed: bool = False) -> float:
    if type(value) not in (int, float):
        raise PositionCheckpointError("native position is unavailable")
    try:
        number = float(value)
    except OverflowError as error:
        # JSON integers can exceed a finite native double. Only conversion
        # overflow is classified here; storage/write errors remain visible.
        raise PositionCheckpointError("native position exceeds numeric range") from error
    if not math.isfinite(number):
        raise PositionCheckpointError("native position is unavailable")
    if not signed and number < 0:
        raise PositionCheckpointError("native position is negative")
    return number

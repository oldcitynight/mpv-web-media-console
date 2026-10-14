from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .options import DEFAULT_OPTION_CATALOG, OptionCatalog, OptionValidationError
from .schema import ProfileResolution


class RuntimeApplicationError(RuntimeError):
    """A runtime option could not be applied and confirmed from observed state."""

    def __init__(self, option: str, reason: str) -> None:
        self.option = option
        self.reason = reason
        super().__init__(f"runtime application failed for {option!r}: {reason}")


@dataclass(frozen=True, slots=True)
class RuntimeApplicationReport:
    applied_options: Mapping[str, bool | int | float | str]
    observed_options: Mapping[str, bool | int | float | str]
    pending_restart_options: Mapping[str, bool | int | float | str]


class ProfileRuntimeApplier:
    """Apply only catalog-declared runtime properties and verify observations.

    The applier receives fixed property handlers from its owner; it has no
    generic mpv command or arbitrary property-setting path.
    """

    def __init__(
        self,
        handlers: Mapping[str, Callable[[Any], None]],
        observe: Callable[[], Mapping[str, Any]],
        *,
        catalog: OptionCatalog = DEFAULT_OPTION_CATALOG,
    ) -> None:
        if not isinstance(handlers, Mapping) or not callable(observe):
            raise TypeError("runtime handlers and observer are required")
        fixed_handlers: dict[str, Callable[[Any], None]] = {}
        for name, handler in handlers.items():
            definition = catalog.require(name)
            if not definition.mutable_runtime:
                raise ValueError(f"{name} is not declared runtime-mutable")
            if not callable(handler):
                raise TypeError("runtime handlers must be callable")
            fixed_handlers[name] = handler
        self._handlers = MappingProxyType(fixed_handlers)
        self._observe = observe
        self._catalog = catalog

    def apply(self, resolution: ProfileResolution) -> RuntimeApplicationReport:
        if not isinstance(resolution, ProfileResolution):
            raise TypeError("runtime application requires a resolved profile selection")

        runtime_values: dict[str, bool | int | float | str] = {}
        pending_restart: dict[str, bool | int | float | str] = {}
        for name in sorted(resolution.options):
            try:
                definition = self._catalog.require(name)
                value = definition.validate(resolution.options[name])
            except OptionValidationError as error:
                raise RuntimeApplicationError(name, "option is not catalog-valid") from error
            if definition.mutable_runtime:
                if name not in self._handlers:
                    raise RuntimeApplicationError(name, "fixed runtime handler is unavailable")
                runtime_values[name] = value
            elif definition.restart_required:
                pending_restart[name] = value
            else:
                raise RuntimeApplicationError(name, "option has no supported application boundary")

        for name, value in runtime_values.items():
            try:
                self._handlers[name](value)
            except Exception as error:
                raise RuntimeApplicationError(name, "player-host rejected the property") from error

        if runtime_values:
            try:
                raw_observed = self._observe()
            except Exception as error:
                raise RuntimeApplicationError("observed-state", "player-host state is unavailable") from error
            if not isinstance(raw_observed, Mapping):
                raise RuntimeApplicationError("observed-state", "player-host state is invalid")
            observed: dict[str, bool | int | float | str] = {}
            for name, expected in runtime_values.items():
                if name not in raw_observed:
                    raise RuntimeApplicationError(name, "property is missing from observed state")
                actual = raw_observed[name]
                if not _observed_value_matches(expected, actual):
                    raise RuntimeApplicationError(name, "observed value does not match the request")
                observed[name] = actual
        else:
            observed = {}

        return RuntimeApplicationReport(
            applied_options=MappingProxyType(dict(runtime_values)),
            observed_options=MappingProxyType(observed),
            pending_restart_options=MappingProxyType(dict(pending_restart)),
        )


def _observed_value_matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual is expected
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False
        return math.isfinite(float(actual)) and math.isclose(
            float(expected), float(actual), rel_tol=0.0, abs_tol=0.001
        )
    return isinstance(actual, str) and actual == expected

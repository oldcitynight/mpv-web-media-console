"""Composable, provenance-aware mpv profile definitions."""

from .application import (
    ProfileRuntimeApplier,
    RuntimeApplicationError,
    RuntimeApplicationReport,
)
from .restart import (
    PlaybackSnapshot,
    ProfileRestartApplier,
    RestartApplicationError,
    RestartApplicationReport,
    SelectedTrack,
    option_value_matches,
)
from .schema import (
    ProfileCategory,
    ProfileCompositionError,
    ProfileConflict,
    ProfileDefinition,
    ProfileResolution,
    ProfileResolver,
)
from .options import (
    DEFAULT_OPTION_CATALOG,
    OptionCatalog,
    OptionDefinition,
    OptionScope,
    OptionSecurityClass,
    OptionValidationError,
    OptionValueType,
)

__all__ = [
    "ProfileRuntimeApplier",
    "PlaybackSnapshot",
    "ProfileRestartApplier",
    "RestartApplicationError",
    "RestartApplicationReport",
    "SelectedTrack",
    "option_value_matches",
    "RuntimeApplicationError",
    "RuntimeApplicationReport",
    "ProfileCategory",
    "ProfileCompositionError",
    "ProfileConflict",
    "ProfileDefinition",
    "ProfileResolution",
    "ProfileResolver",
    "DEFAULT_OPTION_CATALOG",
    "OptionCatalog",
    "OptionDefinition",
    "OptionScope",
    "OptionSecurityClass",
    "OptionValidationError",
    "OptionValueType",
]

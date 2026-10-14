from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


class TrackSelectionError(ValueError):
    """Raised when a track or chapter is not valid for the current media."""


@dataclass(frozen=True, slots=True)
class NormalizedTrack:
    id: int
    type: str
    title: str | None = None
    language: str | None = None
    codec: str | None = None
    selected: bool = False


@dataclass(frozen=True, slots=True)
class NormalizedChapter:
    id: int
    title: str | None
    time: float


class MediaTrackCatalog:
    """Hold current-media tracks/chapters and validate safe selections."""

    _SELECTABLE_TYPES = frozenset({"audio", "sub", "video"})

    def __init__(self) -> None:
        self._tracks: tuple[NormalizedTrack, ...] = ()
        self._chapters: tuple[NormalizedChapter, ...] = ()

    def replace(
        self,
        tracks: Iterable[NormalizedTrack],
        chapters: Iterable[NormalizedChapter],
    ) -> None:
        self._tracks = tuple(tracks)
        self._chapters = tuple(chapters)

    @property
    def tracks(self) -> tuple[NormalizedTrack, ...]:
        return self._tracks

    @property
    def chapters(self) -> tuple[NormalizedChapter, ...]:
        return self._chapters

    def select_track(self, track_id: int, track_type: str) -> NormalizedTrack:
        if track_type not in self._SELECTABLE_TYPES:
            raise TrackSelectionError("track type is not selectable")
        matching = [track for track in self._tracks if track.id == track_id and track.type == track_type]
        if not matching:
            raise TrackSelectionError("track is not present in the current media")
        selected = matching[0]
        self._tracks = tuple(
            NormalizedTrack(
                id=track.id,
                type=track.type,
                title=track.title,
                language=track.language,
                codec=track.codec,
                selected=track is selected if track.type == track_type else track.selected,
            )
            for track in self._tracks
        )
        return next(track for track in self._tracks if track.id == track_id and track.type == track_type)

    def chapter(self, chapter_id: int) -> NormalizedChapter:
        for chapter in self._chapters:
            if chapter.id == chapter_id:
                return chapter
        raise TrackSelectionError("chapter is not present in the current media")

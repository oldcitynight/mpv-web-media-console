import type {
  Chapter,
  PlayerOutputStatus,
  PlayerState,
  Profile,
  ProfileApplyResponse,
  QueueState,
  Track,
} from "./api-client.ts";

export type SelectableTrackType = "audio" | "sub" | "video";

export interface PlaybackViewModel {
  player: {
    id: string;
    sessionId: string | null;
    status: string;
  };
  media: {
    path: string | null;
  };
  controls: {
    paused: boolean;
    positionSeconds: number | null;
    durationSeconds: number | null;
    volumePercent: number | null;
    speedMultiplier: number | null;
  };
}

export interface TrackViewModel {
  id: number;
  type: string;
  title: string | null;
  language: string | null;
  codec: string | null;
  selected: boolean;
  selectableType: SelectableTrackType | null;
}

export interface ChapterViewModel {
  id: number;
  title: string | null;
  timeSeconds: number;
}

export interface QueueItemViewModel {
  id: string;
  mediaPath: string;
  ordinal: number;
  active: boolean;
}

export interface QueueViewModel {
  id: string;
  name: string;
  revision: number;
  items: QueueItemViewModel[];
}

export interface OutputProfileViewModel extends Profile {
  // The category is supplied by the backend profile catalog. The UI forwards
  // it and does not infer output or device capabilities from IDs or names.
  category: string;
}

export interface ProfileApplySummaryViewModel {
  applied: boolean;
  observedOptionCount: number;
  pendingRestartOptionCount: number;
}

/** Fixed canonical output facts; this adapter deliberately performs no inference. */
export interface RemoteOutputStatusViewModel extends PlayerOutputStatus {}

/** Adapt only the canonical API snapshot; raw mpv property maps are not input. */
export function toPlaybackViewModel(state: PlayerState | null): PlaybackViewModel | null {
  if (state === null) return null;
  const status = typeof state.status === "string" ? state.status : "unknown";
  return {
    player: {
      id: state.player_id,
      sessionId: state.session_id ?? null,
      status,
    },
    media: {
      path: state.path ?? null,
    },
    controls: {
      paused: state.pause === true || status.toLowerCase() === "paused",
      positionSeconds: state.position ?? null,
      durationSeconds: state.duration ?? null,
      volumePercent: state.volume ?? null,
      speedMultiplier: state.speed ?? null,
    },
  };
}

export function toRemoteOutputStatusViewModel(
  status: PlayerOutputStatus | null,
): RemoteOutputStatusViewModel | null {
  if (status === null) return null;
  return {
    source_dynamic_range: status.source_dynamic_range,
    source_width: status.source_width,
    source_height: status.source_height,
    source_frame_rate: status.source_frame_rate,
    source_audio_format: status.source_audio_format ?? null,
    target_dynamic_range: status.target_dynamic_range ?? null,
    display_signal_state: status.display_signal_state ?? null,
    display_signal_reason: status.display_signal_reason ?? null,
    display_signal_evidence: status.display_signal_evidence ?? null,
    scanout_format: status.scanout_format ?? null,
    scanout_bit_depth: status.scanout_bit_depth ?? null,
    connector_colorspace: status.connector_colorspace ?? null,
    connector_hdr_eotf: status.connector_hdr_eotf ?? null,
    source_frame_rate_basis: status.source_frame_rate_basis ?? null,
    display_route: status.display_route ?? null,
    audio_route: status.audio_route ?? null,
    display_sink_evidence: status.display_sink_evidence ?? null,
    audio_sink_evidence: status.audio_sink_evidence ?? null,
    display_sink: status.display_sink,
    audio_sink: status.audio_sink,
    audio_layout: status.audio_layout,
    renderer_library_loaded: status.renderer_library_loaded,
  };
}

function selectableTrackType(type: string): SelectableTrackType | null {
  return type === "audio" || type === "sub" || type === "video" ? type : null;
}

export function toTrackViewModels(tracks: readonly Track[]): TrackViewModel[] {
  return tracks.map((track) => ({
    id: track.id,
    type: track.type,
    title: track.title ?? null,
    language: track.language ?? null,
    codec: track.codec ?? null,
    selected: track.selected,
    selectableType: selectableTrackType(track.type),
  }));
}

export function toChapterViewModels(chapters: readonly Chapter[]): ChapterViewModel[] {
  return chapters.map((chapter) => ({
    id: chapter.id,
    title: chapter.title ?? null,
    timeSeconds: chapter.time,
  }));
}

export function toQueueViewModel(queue: QueueState | null): QueueViewModel | null {
  if (queue === null) return null;
  return {
    id: queue.id,
    name: queue.name,
    revision: queue.revision,
    items: queue.items.map((item) => ({
      id: item.id,
      mediaPath: item.path,
      ordinal: item.ordinal,
      active: item.active,
    })),
  };
}

export function toOutputProfileViewModels(
  profiles: readonly Profile[],
): OutputProfileViewModel[] {
  return profiles.map((profile) => ({
    id: profile.id,
    name: profile.name,
    category: profile.category,
    enabled: profile.enabled,
    ...(profile.kind ? { kind: profile.kind } : {}),
    ...(profile.route !== undefined ? { route: profile.route } : {}),
    ...(profile.device_reported !== undefined ? { device_reported: profile.device_reported } : {}),
  }));
}

export function toProfileApplySummaryViewModel(
  result: ProfileApplyResponse,
): ProfileApplySummaryViewModel {
  return {
    applied: result.applied,
    observedOptionCount: Object.keys(result.observed_options).length,
    pendingRestartOptionCount: Object.keys(result.pending_restart_options).length,
  };
}

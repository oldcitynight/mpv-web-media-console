export type JsonScalar = string | number | boolean | null;
export type JsonObject = Record<string, JsonScalar>;

export interface PlayerState {
  player_id: string;
  session_id: string | null;
  status: string;
  position: number | null;
  duration: number | null;
  seekable?: boolean | null;
  path: string | null;
  volume: number | null;
  speed: number | null;
  pause: boolean;
}

export const DISPLAY_SIGNAL_STATES = ['unknown', 'sdr', 'hdr10', 'hlg'] as const;
export const DISPLAY_SIGNAL_REASONS = [
  'not_observed', 'no_current_frame', 'unsupported_renderer', 'unbound_route',
  'permission_denied', 'unavailable', 'invalid_readback', 'connector_disconnected',
  'inactive_scanout', 'missing_connector_properties', 'malformed_metadata',
  'unknown_scanout_format', 'conflicting_evidence', 'stale_observation',
  'epoch_invalidated', 'connector_changed', 'verified_hdr10', 'verified_hlg', 'verified_sdr',
] as const;
export const SCANOUT_FORMAT_DEPTH = {
  XR24: 8, AR24: 8, XB24: 8, AB24: 8,
  XR30: 10, AR30: 10, XB30: 10, AB30: 10,
} as const;
export const CONNECTOR_COLORSPACES = [
  'Default', 'SMPTE_170M_YCC', 'BT709_YCC', 'XVYCC_601', 'XVYCC_709', 'SYCC_601',
  'opYCC_601', 'opRGB', 'BT2020_CYCC', 'BT2020_RGB', 'BT2020_YCC',
  'DCI-P3_RGB_D65', 'DCI-P3_RGB_Theater', 'RGB_WIDE_FIXED', 'RGB_WIDE_FLOAT', 'BT601_YCC',
] as const;

/** Kernel-observed output signaling is separate from source format and renderer intent. */
export interface DisplaySignalStatus {
  display_signal_state?: (typeof DISPLAY_SIGNAL_STATES)[number] | null;
  display_signal_reason?: (typeof DISPLAY_SIGNAL_REASONS)[number] | null;
  display_signal_evidence?: 'drm_connector_state' | null;
  scanout_format?: keyof typeof SCANOUT_FORMAT_DEPTH | null;
  scanout_bit_depth?: 8 | 10 | null;
  connector_colorspace?: (typeof CONNECTOR_COLORSPACES)[number] | null;
  connector_hdr_eotf?: 0 | 1 | 2 | 3 | null;
  target_dynamic_range?: string | null;
}

/** Defense in depth: a renderer target or source HDR flag alone is never signal proof. */
export function displaySignalReadbackIsConsistent(status: DisplaySignalStatus): boolean {
  const state = status.display_signal_state;
  if (state !== 'sdr' && state !== 'hdr10' && state !== 'hlg') return false;
  if (status.display_signal_evidence !== 'drm_connector_state' ||
      status.display_signal_reason !== `verified_${state}` ||
      !status.scanout_format || !Object.hasOwn(SCANOUT_FORMAT_DEPTH, status.scanout_format) ||
      SCANOUT_FORMAT_DEPTH[status.scanout_format] !== status.scanout_bit_depth) return false;
  if (state === 'sdr') return status.target_dynamic_range === 'SDR' &&
    (status.connector_colorspace === 'Default' || status.connector_colorspace === 'BT709_YCC') &&
    (status.connector_hdr_eotf === null || status.connector_hdr_eotf === 0);
  return status.scanout_bit_depth === 10 && status.connector_colorspace === 'BT2020_RGB' &&
    status.target_dynamic_range === (state === 'hdr10' ? 'HDR10' : 'HLG') &&
    status.connector_hdr_eotf === (state === 'hdr10' ? 2 : 3);
}

/** Fixed canonical status fields carried by authenticated WebSocket snapshots. */
export interface PlayerOutputStatus extends DisplaySignalStatus {
  source_audio_format?: string | null;
  target_dynamic_range?: string | null;
  source_frame_rate_basis?: string | null;
  display_route?: string | null;
  audio_route?: string | null;
  display_sink_evidence?: string | null;
  audio_sink_evidence?: string | null;
  source_dynamic_range: string | null;
  source_width: number | null;
  source_height: number | null;
  source_frame_rate: number | null;
  display_sink: string | null;
  audio_sink: string | null;
  audio_layout: string | null;
  renderer_library_loaded: boolean | null;
}

/** WebSocket state extends the REST transport snapshot with typed output facts. */
export interface PlayerSnapshot extends PlayerState {
  output_status: PlayerOutputStatus | null;
}

export interface CommandResponse {
  accepted: boolean;
  request_id: string | null;
}

export interface Track {
  id: number;
  type: "audio" | "sub" | "video" | string;
  title: string | null;
  language: string | null;
  codec?: string | null;
  selected: boolean;
}

export interface Chapter {
  id: number;
  title: string | null;
  time: number;
}

export interface MediaBrowserItem {
  name: string;
  path: string;
  is_directory: boolean;
  size_bytes: number | null;
  modified_at: string | null;
  media_kind: "directory" | "audio" | "video";
  has_preview: boolean;
  preview_kind: "embedded_art" | "sibling_art" | "video_frame" | "placeholder" | "none";
  presentation: MediaPresentation;
}

export interface MediaPresentation {
  hdr_format: string;
  hdr_compatibility: string[];
  audio_formats: string[];
  video_codec: string;
}

export interface MediaMetadataResponse {
  path: string;
  preview_kind: MediaBrowserItem["preview_kind"];
  has_preview: boolean;
  presentation: MediaPresentation;
}

export interface MediaBrowserResponse {
  directory: string | null;
  parent_directory: string | null;
  items: MediaBrowserItem[];
  truncated: boolean;
  total?: number;
  total_files?: number;
  offset?: number;
  next_offset?: number | null;
  version?: string;
}

export interface QueueItem {
  id: string;
  path: string;
  ordinal: number;
  active: boolean;
}

export interface QueueState {
  id: string;
  name: string;
  revision: number;
  items: QueueItem[];
  playback?: { intent_id: number; item_id: string | null;
    phase: "prepared" | "submitted" | "confirmed" | "failed" | "uncertain" | "superseded" | "removed" | "cancelled";
    detail: string | null } | null;
}

export interface OutputDevice {
  id: string;
  name: string | null;
  method: "ALSA" | "PipeWire" | "DRM";
  route: string;
  kind: "audio_device" | "audio_sink" | "display_connector";
  profile_ids: string[];
}

export interface OutputInventory {
  devices: OutputDevice[];
  unavailable: string[];
}

export interface Profile {
  id: string;
  name: string;
  category: string;
  enabled: boolean;
  kind?: "profile" | "backend" | "device" | "no_output" | "decoder" | "render_context";
  route?: string | null;
  device_reported?: boolean | null;
}

export interface ProfileApplyResponse {
  accepted: boolean;
  applied: boolean;
  profile_ids: string[];
  effective_options: Record<string, JsonScalar>;
  sources: Record<string, string>;
  conflicts: Array<Record<string, unknown>>;
  observed_options: Record<string, JsonScalar>;
  pending_restart_options: Record<string, JsonScalar>;
}

export interface ApiClientOptions {
  baseUrl?: string;
  token?: string;
  fetcher?: typeof fetch;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly correlationId: string | null;

  constructor(status: number, code: string, message: string, correlationId: string | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.correlationId = correlationId;
  }
}

export function resolveApiOrigin(baseUrl?: string): string {
  const fallback =
    typeof globalThis.location === "undefined"
      ? "http://127.0.0.1:8765"
      : globalThis.location.origin;
  const parsed = new URL(baseUrl ?? fallback);
  if (
    (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash ||
    (parsed.pathname !== "/" && parsed.pathname !== "")
  ) {
    throw new TypeError("baseUrl must be an HTTP(S) origin without credentials or a path");
  }
  return parsed.origin;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function validateRequestKey(value: string | undefined): void {
  if (value !== undefined && (!value || value.length > 128 || value.trim() !== value)) {
    throw new TypeError("requestKey must contain 1-128 non-whitespace-surrounded characters");
  }
}

function withRequestKey<T extends Record<string, unknown>>(
  value: T,
  requestKey: string | undefined,
): T | (T & { request_key: string }) {
  validateRequestKey(requestKey);
  return requestKey === undefined ? value : { ...value, request_key: requestKey };
}

export function createApiClient(options: ApiClientOptions = {}) {
  const origin = resolveApiOrigin(options.baseUrl);
  const token = options.token;
  if (token !== undefined && (!token || token.trim() !== token)) {
    throw new TypeError("token must be non-empty and have no surrounding whitespace");
  }
  const fetcher = options.fetcher ?? globalThis.fetch.bind(globalThis);
  if (typeof fetcher !== "function") {
    throw new TypeError("a Fetch-compatible implementation is required");
  }

  async function request<T>(
    method: string,
    route: string,
    body?: unknown,
    requestKey?: string,
    signal?: AbortSignal,
  ): Promise<T> {
    validateRequestKey(requestKey);
    const headers = new Headers({ Accept: "application/json" });
    if (token !== undefined) headers.set("Authorization", `Bearer ${token}`);
    if (body !== undefined) headers.set("Content-Type", "application/json");
    if (requestKey !== undefined) headers.set("Idempotency-Key", requestKey);

    const response = await fetcher(new URL(route, `${origin}/`), {
      method,
      ...(signal ? { signal } : {}),
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
      redirect: "error",
      cache: "no-store",
    });
    if (response.status === 204) return undefined as T;

    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    const data = isRecord(payload) ? payload : {};
    if (!response.ok) {
      throw new ApiError(
        response.status,
        typeof data.code === "string" ? data.code : "http_error",
        typeof data.message === "string" ? data.message : `Request failed (${response.status}).`,
        typeof data.correlation_id === "string" ? data.correlation_id : null,
      );
    }
    return payload as T;
  }

  async function requestBlob(route: string): Promise<Blob> {
    const headers = new Headers({ Accept: "image/avif,image/webp,image/png,image/jpeg,image/svg+xml" });
    if (token !== undefined) headers.set("Authorization", `Bearer ${token}`);
    const response = await fetcher(new URL(route, `${origin}/`), {
      method: "GET",
      headers,
      credentials: "same-origin",
      redirect: "error",
      cache: "no-store",
    });
    if (!response.ok) {
      let payload: unknown;
      try {
        payload = await response.json();
      } catch {
        payload = null;
      }
      const data = isRecord(payload) ? payload : {};
      throw new ApiError(
        response.status,
        typeof data.code === "string" ? data.code : "http_error",
        typeof data.message === "string" ? data.message : `Request failed (${response.status}).`,
        typeof data.correlation_id === "string" ? data.correlation_id : null,
      );
    }
    return response.blob();
  }

  return {
    getPlayerState: () => request<PlayerState>("GET", "/api/v1/player/state"),
    browseMedia: (directory?: string | null, search = "", limit = 100, page: { offset?: number; version?: string; sort?: string; details?: boolean; signal?: AbortSignal } = {}) => {
      if (directory !== undefined && directory !== null && (!directory || directory.length > 4096)) {
        throw new TypeError("directory must contain 1-4096 characters");
      }
      if (typeof search !== "string" || search.length > 128) {
        throw new TypeError("search must contain at most 128 characters");
      }
      if (!Number.isSafeInteger(limit) || limit < 1 || limit > 200) {
        throw new TypeError("limit must be an integer from 1 to 200");
      }
      const query = new URLSearchParams();
      if (directory !== undefined && directory !== null) query.set("directory", directory);
      if (search) query.set("search", search);
      query.set("limit", String(limit));
      if (page.offset !== undefined) query.set("offset", String(page.offset));
      if (page.version) query.set("version", page.version);
      if (page.sort) query.set("sort", page.sort);
      if (page.details === false) query.set("details", "false");
      return request<MediaBrowserResponse>("GET", `/api/v1/media/browse?${query.toString()}`, undefined, undefined, page.signal);
    },
    getMediaMetadata: (path: string) => {
      if (!path || path.length > 4096) throw new TypeError("media metadata path is invalid");
      return request<MediaMetadataResponse>("GET", `/api/v1/media/metadata?path=${encodeURIComponent(path)}`);
    },
    getMediaPreview: (path: string) => {
      if (!path || path.length > 4096) throw new TypeError("media preview path is invalid");
      return requestBlob(`/api/v1/media/preview?path=${encodeURIComponent(path)}`);
    },
    load: (path: string, requestKey?: string) =>
      request<CommandResponse>(
        "POST",
        "/api/v1/player/load",
        withRequestKey({ path }, requestKey),
        requestKey,
      ),
    play: (requestKey?: string) =>
      request<CommandResponse>("POST", "/api/v1/player/play", undefined, requestKey),
    pause: (requestKey?: string) =>
      request<CommandResponse>("POST", "/api/v1/player/pause", undefined, requestKey),
    stop: (requestKey?: string) =>
      request<CommandResponse>("POST", "/api/v1/player/stop", undefined, requestKey),
    seek: (seconds: number, relative = false, requestKey?: string) => {
      if (!Number.isFinite(seconds) || (!relative && seconds < 0)) {
        throw new TypeError(
          relative
            ? "relative seek offset must be finite per the API contract"
            : "absolute seek position must be finite and non-negative per the API contract",
        );
      }
      return request<CommandResponse>(
        "POST",
        "/api/v1/player/seek",
        withRequestKey({ seconds, relative }, requestKey),
        requestKey,
      );
    },
    setVolume: (value: number, requestKey?: string) => {
      if (!Number.isFinite(value)) throw new TypeError("volume must be finite");
      return request<CommandResponse>(
        "POST",
        "/api/v1/player/volume",
        withRequestKey({ value }, requestKey),
        requestKey,
      );
    },
    setSpeed: (value: number, requestKey?: string) => {
      if (!Number.isFinite(value) || value <= 0) throw new TypeError("speed must be positive and finite");
      return request<CommandResponse>(
        "POST",
        "/api/v1/player/speed",
        withRequestKey({ value }, requestKey),
        requestKey,
      );
    },
    getTracks: () => request<Track[]>("GET", "/api/v1/player/tracks"),
    selectTrack: (trackId: number, type: "audio" | "sub" | "video", requestKey?: string) => {
      if (!Number.isSafeInteger(trackId) || trackId < 0) throw new TypeError("trackId must be non-negative");
      return request<CommandResponse>(
        "POST",
        `/api/v1/player/tracks/${trackId}/select`,
        withRequestKey({ type }, requestKey),
        requestKey,
      );
    },
    getChapters: () => request<Chapter[]>("GET", "/api/v1/player/chapters"),
    getQueue: () => request<QueueState>("GET", "/api/v1/queue"),
    addQueueItem: (path: string, ordinal?: number, expectedRevision?: number) =>
      request<QueueItem>("POST", "/api/v1/queue", {
        path,
        ...(ordinal === undefined ? {} : { ordinal }),
        ...(expectedRevision === undefined ? {} : { expected_revision: expectedRevision }),
      }),
    clearQueue: (expectedRevision?: number) => {
      const query =
        expectedRevision === undefined ? "" : `?expected_revision=${encodeURIComponent(String(expectedRevision))}`;
      return request<QueueState>("DELETE", `/api/v1/queue${query}`);
    },
    removeQueueItem: (itemId: string, expectedRevision?: number) => {
      const query =
        expectedRevision === undefined ? "" : `?expected_revision=${encodeURIComponent(String(expectedRevision))}`;
      return request<QueueState>(
        "DELETE",
        `/api/v1/queue/items/${encodeURIComponent(itemId)}${query}`,
      );
    },
    moveQueueItem: (itemId: string, ordinal: number, expectedRevision?: number) =>
      request<QueueState>("PATCH", `/api/v1/queue/items/${encodeURIComponent(itemId)}`, {
        ordinal,
        ...(expectedRevision === undefined ? {} : { expected_revision: expectedRevision }),
      }),
    activateQueueItem: (itemId: string, expectedRevision?: number) =>
      request<QueueState>("POST", `/api/v1/queue/items/${encodeURIComponent(itemId)}/activate`, {
        ...(expectedRevision === undefined ? {} : { expected_revision: expectedRevision }),
      }),
    getOutputs: () => request<OutputInventory>("GET", "/api/v1/outputs"),
    getProfiles: () => request<Profile[]>("GET", "/api/v1/profiles"),
    applyProfiles: (
      profileIds: string[],
      overrides: JsonObject = {},
      requestKey?: string,
    ) =>
      request<ProfileApplyResponse>("POST", "/api/v1/profiles/apply", {
        profile_ids: profileIds,
        overrides,
        ...(requestKey === undefined ? {} : { request_key: requestKey }),
      }),
  };
}

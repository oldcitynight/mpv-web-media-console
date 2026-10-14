import {
  CONNECTOR_COLORSPACES,
  DISPLAY_SIGNAL_REASONS,
  DISPLAY_SIGNAL_STATES,
  SCANOUT_FORMAT_DEPTH,
  displaySignalReadbackIsConsistent,
  resolveApiOrigin,
  type PlayerOutputStatus,
  type PlayerSnapshot,
} from "./api-client.ts";

export type ConnectionStatus = "idle" | "connecting" | "open" | "backoff" | "error" | "closed";

export interface PlayerStoreView {
  connection: ConnectionStatus;
  snapshot: PlayerSnapshot | null;
  lastSequence: number | null;
  lastEventType: string | null;
  error: string | null;
}

export interface SocketLike {
  onopen: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onerror: ((event: unknown) => void) | null;
  onclose: ((event: { code: number; reason?: string }) => void) | null;
  close(code?: number, reason?: string): void;
}

export type WebSocketFactory = (url: string, protocols: string[]) => SocketLike;
export type TimerHandle = ReturnType<typeof globalThis.setTimeout>;

export interface PlayerStateStoreOptions {
  baseUrl?: string;
  token?: string;
  webSocketFactory?: WebSocketFactory;
  setTimeoutFn?: (callback: () => void, delayMs: number) => TimerHandle;
  clearTimeoutFn?: (handle: TimerHandle) => void;
  initialRetryMs?: number;
  maximumRetryMs?: number;
}

export const APP_WEBSOCKET_PROTOCOL = "mpv-webd.v1";
const AUTH_PROTOCOL_PREFIX = "mpv-auth.";

export function browserWebSocketProtocols(token?: string): string[] {
  if (token === undefined || token === "") return [APP_WEBSOCKET_PROTOCOL];
  if (!/^[A-Za-z0-9._~-]{1,512}$/.test(token)) {
    throw new TypeError("WebSocket bearer token contains unsupported characters");
  }
  return [APP_WEBSOCKET_PROTOCOL, `${AUTH_PROTOCOL_PREFIX}${token}`];
}

export function websocketUrl(baseUrl?: string, lastSequence?: number | null): string {
  const url = new URL("/api/v1/ws", `${resolveApiOrigin(baseUrl)}/`);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  if (lastSequence !== undefined && lastSequence !== null) {
    if (!Number.isSafeInteger(lastSequence) || lastSequence < 0) {
      throw new TypeError("lastSequence must be a non-negative safe integer");
    }
    url.searchParams.set("last_sequence", String(lastSequence));
  }
  return url.toString();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

interface EventEnvelope {
  schema_version: 1;
  type: string;
  sequence: number;
  timestamp: string | number;
  player_id: string;
  session_id: string;
  correlation_id: string;
  payload: Record<string, unknown>;
}

function parseEnvelope(data: unknown): EventEnvelope {
  if (typeof data !== "string") throw new TypeError("WebSocket message must be JSON text");
  const value: unknown = JSON.parse(data);
  if (!isRecord(value)) throw new TypeError("event envelope must be an object");
  if (
    value.schema_version !== 1 ||
    typeof value.type !== "string" ||
    value.type.length === 0 ||
    !Number.isSafeInteger(value.sequence) ||
    (value.sequence as number) < (value.type === "state.snapshot" ? 0 : 1) ||
    !(typeof value.timestamp === "string" ||
      (typeof value.timestamp === "number" && Number.isFinite(value.timestamp))) ||
    typeof value.player_id !== "string" ||
    value.player_id.length === 0 ||
    typeof value.session_id !== "string" ||
    value.session_id.length === 0 ||
    typeof value.correlation_id !== "string" ||
    value.correlation_id.length === 0 ||
    !isRecord(value.payload)
  ) {
    throw new TypeError("event envelope does not match schema version 1");
  }
  return value as unknown as EventEnvelope;
}

function readOptionalNumber(value: unknown, field: string): number | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new TypeError(`invalid ${field} in player state`);
  }
  return value;
}

function readOptionalString(value: unknown, field: string): string | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "string") throw new TypeError(`invalid ${field} in player state`);
  return value;
}

const OUTPUT_STATUS_FIELDS = new Set<keyof PlayerOutputStatus>([
  "source_audio_format",
  "target_dynamic_range",
  "source_frame_rate_basis",
  "display_route",
  "audio_route",
  "display_sink_evidence",
  "audio_sink_evidence",
  "source_dynamic_range",
  "source_width",
  "source_height",
  "source_frame_rate",
  "display_sink",
  "audio_sink",
  "audio_layout",
  "renderer_library_loaded",
  "display_signal_state",
  "display_signal_reason",
  "display_signal_evidence",
  "scanout_format",
  "scanout_bit_depth",
  "connector_colorspace",
  "connector_hdr_eotf",
]);

function readStatusLabel(value: unknown, field: string): string | null {
  const label = readOptionalString(value, field);
  if (
    label !== null &&
    (!label || label.trim() !== label || label.length > 120 || /[\u0000-\u001f\u007f]/.test(label))
  ) {
    throw new TypeError(`invalid ${field} in player output status`);
  }
  return label;
}

function readOptionalPositiveInteger(value: unknown, field: string): number | null {
  const number = readOptionalNumber(value, field);
  if (number !== null && (!Number.isSafeInteger(number) || number <= 0)) {
    throw new TypeError(`invalid ${field} in player output status`);
  }
  return number;
}

function readOptionalEnum<T extends string | number>(value: unknown, choices: readonly T[], field: string): T | null {
  if (value === undefined || value === null) return null;
  if (!choices.some((choice) => choice === value)) throw new TypeError(`invalid ${field} in player output status`);
  return value as T;
}

function normalizeOutputStatus(value: unknown): PlayerOutputStatus | null {
  if (value === undefined || value === null) return null;
  if (!isRecord(value)) throw new TypeError("output_status must be an object or null");
  const unknown = Object.keys(value).filter(
    (key) => !OUTPUT_STATUS_FIELDS.has(key as keyof PlayerOutputStatus),
  );
  if (unknown.length) throw new TypeError("output_status contains unsupported fields");

  const sourceFrameRate = readOptionalNumber(value.source_frame_rate, "source_frame_rate");
  if (sourceFrameRate !== null && sourceFrameRate <= 0) {
    throw new TypeError("invalid source_frame_rate in player output status");
  }
  const rendererLoaded = value.renderer_library_loaded;
  if (rendererLoaded != null && typeof rendererLoaded !== "boolean") {
    throw new TypeError("invalid renderer_library_loaded in player output status");
  }
  const status: PlayerOutputStatus = {
    source_dynamic_range: readStatusLabel(value.source_dynamic_range, "source_dynamic_range"),
    source_width: readOptionalPositiveInteger(value.source_width, "source_width"),
    source_height: readOptionalPositiveInteger(value.source_height, "source_height"),
    source_frame_rate: sourceFrameRate,
    source_audio_format: readStatusLabel(value.source_audio_format, "source_audio_format"),
    target_dynamic_range: readOptionalEnum(value.target_dynamic_range, ['Unknown', 'SDR', 'HDR10', 'HLG'] as const, "target_dynamic_range"),
    source_frame_rate_basis: readStatusLabel(value.source_frame_rate_basis, "source_frame_rate_basis"),
    display_route: readStatusLabel(value.display_route, "display_route"),
    audio_route: readStatusLabel(value.audio_route, "audio_route"),
    display_sink_evidence: readStatusLabel(value.display_sink_evidence, "display_sink_evidence"),
    audio_sink_evidence: readStatusLabel(value.audio_sink_evidence, "audio_sink_evidence"),
    display_sink: readStatusLabel(value.display_sink, "display_sink"),
    audio_sink: readStatusLabel(value.audio_sink, "audio_sink"),
    audio_layout: readStatusLabel(value.audio_layout, "audio_layout"),
    renderer_library_loaded: rendererLoaded ?? null,
    display_signal_state: readOptionalEnum(value.display_signal_state, DISPLAY_SIGNAL_STATES, "display_signal_state"),
    display_signal_reason: readOptionalEnum(value.display_signal_reason, DISPLAY_SIGNAL_REASONS, "display_signal_reason"),
    display_signal_evidence: readOptionalEnum(value.display_signal_evidence, ['drm_connector_state'] as const, "display_signal_evidence"),
    scanout_format: readOptionalEnum(value.scanout_format, Object.keys(SCANOUT_FORMAT_DEPTH) as (keyof typeof SCANOUT_FORMAT_DEPTH)[], "scanout_format"),
    scanout_bit_depth: readOptionalEnum(value.scanout_bit_depth, [8, 10] as const, "scanout_bit_depth"),
    connector_colorspace: readOptionalEnum(value.connector_colorspace, CONNECTOR_COLORSPACES, "connector_colorspace"),
    connector_hdr_eotf: readOptionalEnum(value.connector_hdr_eotf, [0, 1, 2, 3] as const, "connector_hdr_eotf"),
  };
  const claimsSignal = status.display_signal_state != null && status.display_signal_state !== 'unknown';
  const claimsVerified = status.display_signal_reason?.startsWith('verified_') === true;
  if ((claimsSignal || claimsVerified) &&
      (value.connector_hdr_eotf === undefined || !displaySignalReadbackIsConsistent(status))) {
    status.display_signal_state = 'unknown';
    status.display_signal_reason = 'conflicting_evidence';
    status.display_signal_evidence = null;
  }
  return status;
}

function firstPresent(primary: unknown, secondary: unknown, tertiary: unknown): unknown {
  if (primary !== undefined) return primary;
  if (secondary !== undefined) return secondary;
  return tertiary;
}

function normalizePlayerState(
  value: unknown,
  envelope: Pick<EventEnvelope, "player_id" | "session_id">,
): PlayerSnapshot {
  if (!isRecord(value)) throw new TypeError("snapshot state must be an object");
  const properties = isRecord(value.properties) ? value.properties : value;
  const playerId = value.player_id ?? envelope.player_id;
  const sessionId = value.session_id ?? envelope.session_id;
  if (typeof playerId !== "string" || playerId.length === 0) {
    throw new TypeError("player state has no player_id");
  }
  if (typeof sessionId !== "string" || sessionId.length === 0) {
    throw new TypeError("player state has no session_id");
  }
  if (typeof value.status !== "string" || value.status.length === 0) {
    throw new TypeError("player state has no status");
  }
  const pause = firstPresent(value.pause, properties.pause, undefined);
  if (pause !== undefined && typeof pause !== "boolean") {
    throw new TypeError("invalid pause in player state");
  }
  const seekable = value.seekable ?? properties.seekable;
  if (seekable !== undefined && seekable !== null && typeof seekable !== "boolean") throw new TypeError("invalid seekable in player state");
  return {
    player_id: playerId,
    session_id: sessionId,
    status: value.status,
    position: readOptionalNumber(
      firstPresent(value.position, properties.time_pos, properties["time-pos"]),
      "position",
    ),
    duration: readOptionalNumber(value.duration ?? properties.duration, "duration"),
    path: readOptionalString(value.path ?? properties.path, "path"),
    volume: readOptionalNumber(value.volume ?? properties.volume, "volume"),
    speed: readOptionalNumber(value.speed ?? properties.speed, "speed"),
    pause: pause === true,
    seekable: typeof seekable === "boolean" ? seekable : null,
    output_status: normalizeOutputStatus(value.output_status),
  };
}

function defaultWebSocketFactory(url: string, protocols: string[]): SocketLike {
  if (typeof globalThis.WebSocket === "undefined") {
    throw new Error("WebSocket is not available in this browser");
  }
  return new globalThis.WebSocket(url, protocols) as unknown as SocketLike;
}

export class PlayerStateStore {
  private readonly baseUrl: string | undefined;
  private readonly token: string | undefined;
  private readonly webSocketFactory: WebSocketFactory;
  private readonly schedule: (callback: () => void, delayMs: number) => TimerHandle;
  private readonly cancel: (handle: TimerHandle) => void;
  private readonly initialRetryMs: number;
  private readonly maximumRetryMs: number;
  private readonly listeners = new Set<(view: PlayerStoreView) => void>();
  private view: PlayerStoreView = {
    connection: "idle",
    snapshot: null,
    lastSequence: null,
    lastEventType: null,
    error: null,
  };
  private socket: SocketLike | null = null;
  private timer: TimerHandle | null = null;
  private retryAttempt = 0;
  private forceSnapshot = false;
  private signalSnapshotReady = false;
  private stopped = true;

  constructor(options: PlayerStateStoreOptions) {
    browserWebSocketProtocols(options.token);
    this.baseUrl = options.baseUrl;
    this.token = options.token;
    this.webSocketFactory = options.webSocketFactory ?? defaultWebSocketFactory;
    this.schedule = options.setTimeoutFn ?? ((callback, delay) => globalThis.setTimeout(callback, delay));
    this.cancel = options.clearTimeoutFn ?? ((handle) => globalThis.clearTimeout(handle));
    this.initialRetryMs = options.initialRetryMs ?? 500;
    this.maximumRetryMs = options.maximumRetryMs ?? 15000;
    if (this.initialRetryMs < 1 || this.maximumRetryMs < this.initialRetryMs) {
      throw new TypeError("retry delays must be positive and maximumRetryMs >= initialRetryMs");
    }
  }

  getView(): PlayerStoreView {
    return { ...this.view, snapshot: this.view.snapshot ? { ...this.view.snapshot } : null };
  }

  subscribe(listener: (view: PlayerStoreView) => void): () => void {
    this.listeners.add(listener);
    listener(this.getView());
    return () => this.listeners.delete(listener);
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer !== null) {
      this.cancel(this.timer);
      this.timer = null;
    }
    const current = this.socket;
    this.socket = null;
    if (current) current.close(1000, "client stopped");
    this.publish({ connection: "closed", error: null });
  }

  private connect(): void {
    if (this.stopped) return;
    this.signalSnapshotReady = false;
    this.publish({ connection: "connecting", error: null });
    const cursor = this.forceSnapshot ? null : this.view.lastSequence;
    let socket: SocketLike;
    try {
      socket = this.webSocketFactory(
        websocketUrl(this.baseUrl, cursor),
        browserWebSocketProtocols(this.token),
      );
    } catch {
      this.publish({ connection: "backoff", error: "websocket_unavailable" });
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    let opened = false;
    socket.onopen = () => {
      if (this.socket !== socket || this.stopped) return;
      opened = true;
      this.publish({ connection: "open", error: null });
    };
    socket.onmessage = (event) => {
      if (this.socket !== socket || this.stopped || !opened) return;
      try {
        this.acceptEnvelope(parseEnvelope(event.data));
      } catch {
        this.requestFreshSnapshot(socket, "invalid_event");
      }
    };
    socket.onerror = () => {
      if (this.socket === socket && !this.stopped) {
        this.publish({ error: "websocket_transport_error" });
      }
    };
    socket.onclose = (event) => {
      if (this.socket !== socket) return;
      opened = false;
      this.socket = null;
      if (this.stopped) return;
      if (event.code === 1008) {
        this.stopped = true;
        this.publish({ connection: "error", error: "websocket_authentication_failed" });
        return;
      }
      if (event.code === 4009 || event.code === 1012 || event.code === 1013) {
        this.forceSnapshot = true;
      }
      if (event.code === 1000 && !this.forceSnapshot) {
        this.publish({ connection: "closed" });
        return;
      }
      this.publish({ connection: "backoff" });
      this.scheduleReconnect();
    };
  }

  private acceptEnvelope(event: EventEnvelope): void {
    if (event.type === "state.snapshot") {
      const snapshot = normalizePlayerState(event.payload, event);
      this.forceSnapshot = false;
      this.signalSnapshotReady = true;
      this.retryAttempt = 0;
      this.publish({
        snapshot,
        lastSequence: event.sequence,
        lastEventType: event.type,
        connection: "open",
        error: null,
      });
      return;
    }

    if (this.forceSnapshot) {
      this.resync("snapshot_required");
      return;
    }

    const currentSequence = this.view.lastSequence;
    const currentSnapshot = this.view.snapshot;
    if (currentSequence === null || currentSnapshot === null) {
      this.resync("snapshot_required");
      return;
    }
    // A new player session can restart numbering below the old stream.
    if (event.session_id !== currentSnapshot.session_id || event.player_id !== currentSnapshot.player_id) {
      this.resync("event_sequence_gap");
      return;
    }
    if (event.sequence <= currentSequence) return;
    if (
      (event.payload.stream_previous_sequence !== undefined ? event.payload.stream_previous_sequence !== currentSequence : event.sequence !== currentSequence + 1) ||
      event.session_id !== currentSnapshot.session_id ||
      event.player_id !== currentSnapshot.player_id
    ) {
      this.resync("event_sequence_gap");
      return;
    }

    const canonicalState = event.payload.state;
    if (!isRecord(canonicalState)) {
      this.resync("event_state_missing");
      return;
    }
    const snapshot = normalizePlayerState(canonicalState, event);
    const signal = snapshot.output_status;
    if (!this.signalSnapshotReady && signal &&
        (signal.display_signal_state != null || signal.display_signal_evidence != null)) {
      // Cursor replay can carry previously valid proof; only this connection's fresh snapshot unlocks it.
      this.resync("snapshot_required");
      return;
    }
    this.publish({
      snapshot,
      lastSequence: event.sequence,
      lastEventType: event.type,
      connection: "open",
      error: null,
    });
  }

  private requestFreshSnapshot(socket: SocketLike, reason: string): void {
    if (this.socket !== socket) return;
    this.forceSnapshot = true;
    this.publish({ connection: "backoff", error: reason });
    socket.close(4000, "snapshot resynchronization required");
  }

  private resync(reason: string): void {
    const socket = this.socket;
    if (!socket) return;
    this.requestFreshSnapshot(socket, reason);
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.timer !== null) return;
    const delay = Math.min(
      this.initialRetryMs * 2 ** Math.min(this.retryAttempt, 10),
      this.maximumRetryMs,
    );
    this.retryAttempt += 1;
    this.timer = this.schedule(() => {
      this.timer = null;
      this.connect();
    }, delay);
  }

  private publish(update: Partial<PlayerStoreView>): void {
    this.view = { ...this.view, ...update };
    if (this.view.connection !== 'open' || this.view.error !== null) this.signalSnapshotReady = false;
    const output = this.view.snapshot?.output_status;
    if ((this.view.connection !== 'open' || this.view.error !== null) && output &&
        (output.display_signal_state != null || output.display_signal_evidence != null)) {
      this.forceSnapshot = true;
      // Retained playback snapshots cannot keep claiming current physical-path observations.
      this.view.snapshot = {...this.view.snapshot!, output_status: {...output,
        display_signal_state:'unknown', display_signal_reason:'stale_observation', display_signal_evidence:null,
        scanout_format:null, scanout_bit_depth:null, connector_colorspace:null, connector_hdr_eotf:null,
      }};
    }
    const snapshot = this.getView();
    for (const listener of this.listeners) {
      try {
        listener(snapshot);
      } catch {
        // A view subscriber must not interrupt event processing for other subscribers.
      }
    }
  }
}

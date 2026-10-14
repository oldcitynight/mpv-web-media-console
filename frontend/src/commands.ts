import {
  type ApiClientOptions,
  type CommandResponse,
  createApiClient,
} from "./api-client.ts";
export type { CommandResponse, Track } from "./api-client.ts";

type ApiClient = ReturnType<typeof createApiClient>;

export type SelectableTrackType = "audio" | "sub" | "video";

export interface LoadCommandRequest {
  path: string;
  requestKey?: string;
}

export interface SeekCommandRequest {
  seconds: number;
  relative?: boolean;
  requestKey?: string;
}

export interface TrackSelectionRequest {
  trackId: number;
  type: SelectableTrackType;
  requestKey?: string;
}

export interface PlayerCommands {
  load(request: LoadCommandRequest): Promise<CommandResponse>;
  play(requestKey?: string): Promise<CommandResponse>;
  pause(requestKey?: string): Promise<CommandResponse>;
  stop(requestKey?: string): Promise<CommandResponse>;
  seek(seconds: number, relative?: boolean, requestKey?: string): Promise<CommandResponse>;
  seekAbsolute(seconds: number, requestKey?: string): Promise<CommandResponse>;
  seekRelative(seconds: number, requestKey?: string): Promise<CommandResponse>;
  setVolume(value: number, requestKey?: string): Promise<CommandResponse>;
  setSpeed(value: number, requestKey?: string): Promise<CommandResponse>;
  selectTrack(request: TrackSelectionRequest): Promise<CommandResponse>;
}

/** Fixed typed playback operations; arbitrary mpv property/command access is absent. */
export function createPlayerCommands(client: ApiClient): PlayerCommands {
  const commands: PlayerCommands = {
    load: ({ path, requestKey }) => client.load(path, requestKey),
    play: (requestKey) => client.play(requestKey),
    pause: (requestKey) => client.pause(requestKey),
    stop: (requestKey) => client.stop(requestKey),
    seek: (seconds, relative = false, requestKey) =>
      client.seek(seconds, relative, requestKey),
    seekAbsolute: (seconds, requestKey) => client.seek(seconds, false, requestKey),
    seekRelative: (seconds, requestKey) => client.seek(seconds, true, requestKey),
    setVolume: (value, requestKey) => client.setVolume(value, requestKey),
    setSpeed: (value, requestKey) => client.setSpeed(value, requestKey),
    selectTrack: ({ trackId, type, requestKey }) =>
      client.selectTrack(trackId, type, requestKey),
  };
  return Object.freeze(commands);
}

/** Compose a playback-only client when read/queue/profile operations are unnecessary. */
export function createPlayerCommandClient(options: ApiClientOptions = {}): PlayerCommands {
  return createPlayerCommands(createApiClient(options));
}

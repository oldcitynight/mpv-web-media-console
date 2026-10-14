import { translate, type AppLocale } from "./i18n.ts";
import type { PlayerCommands } from "./commands.ts";
import type { PlayerState } from "./api-client.ts";
import type { PlayerStoreView } from "./player-state-store.ts";

export type PlaybackApi = Pick<PlayerCommands, "play" | "pause" | "seek" | "setVolume" | "setSpeed">;

export const SKIP_INTERVAL_SECONDS = 10;
export const DEFAULT_UNMUTE_VOLUME = 50;

export function togglePlayback(api: PlaybackApi, player: PlayerState | null) {
  if (!player) throw new TypeError("player state is required to toggle playback");
  return player.pause || player.status === "paused" ? api.play() : api.pause();
}

export function skipPlayback(
  api: PlaybackApi,
  position: number | null,
  direction: "backward" | "forward",
) {
  if (position === null || !Number.isFinite(position) || position < 0) {
    throw new TypeError("a finite observed position is required to skip");
  }
  if (direction === "forward") return api.seek(SKIP_INTERVAL_SECONDS, true);
  return api.seek(Math.max(0, position - SKIP_INTERVAL_SECONDS), false);
}

export function seekTo(api: PlaybackApi, position: number, duration: number | null) {
  if (!Number.isFinite(position) || position < 0) {
    throw new TypeError("seek position must be finite and non-negative");
  }
  if (duration !== null && (!Number.isFinite(duration) || duration < 0)) {
    throw new TypeError("duration must be finite and non-negative");
  }
  return api.seek(duration === null ? position : Math.min(position, duration), false);
}

export function setPlaybackVolume(api: PlaybackApi, volume: number) {
  if (!Number.isFinite(volume)) throw new TypeError("volume must be finite");
  return api.setVolume(Math.min(100, Math.max(0, volume)));
}

export function toggleMute(
  api: PlaybackApi,
  currentVolume: number,
  lastAudibleVolume: number,
) {
  if (!Number.isFinite(currentVolume) || currentVolume < 0 || currentVolume > 100) {
    throw new TypeError("current volume must be between 0 and 100");
  }
  if (!Number.isFinite(lastAudibleVolume) || lastAudibleVolume < 0 || lastAudibleVolume > 100) {
    throw new TypeError("last audible volume must be between 0 and 100");
  }
  if (currentVolume > 0) return api.setVolume(0);
  return api.setVolume(lastAudibleVolume > 0 ? lastAudibleVolume : DEFAULT_UNMUTE_VOLUME);
}

export function setPlaybackSpeed(api: PlaybackApi, speed: number) {
  if (!Number.isFinite(speed) || speed <= 0 || speed > 16) {
    throw new TypeError("speed must be greater than 0 and at most 16");
  }
  return api.setSpeed(speed);
}

export function mediaTitle(path: string | null, locale: AppLocale = "en"): string {
  if (!path) return translate(locale, "noMediaLoaded");
  const normalized = path.replace(/[\\/]+$/, "");
  const leaf = normalized.split(/[\\/]/).at(-1);
  return leaf || path;
}

export function formatPlaybackTime(value: number | null): string {
  if (value === null || !Number.isFinite(value) || value < 0) return "--:--";
  const totalSeconds = Math.floor(value);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  const hours = Math.floor(minutes / 60);
  const shortMinutes = minutes % 60;
  return hours > 0
    ? `${hours}:${String(shortMinutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${shortMinutes}:${String(seconds).padStart(2, "0")}`;
}

export function connectionLabel(view: PlayerStoreView): string {
  if (view.error === "websocket_authentication_failed") return "Authentication failed";
  if (view.snapshot?.status === "error") return "Player degraded";
  if (view.connection === "open" && view.snapshot) return "Connected";
  if (view.connection === "backoff") return "Reconnecting";
  if (view.connection === "connecting") return "Connecting";
  if (view.connection === "error") return "Needs attention";
  if (view.connection === "closed") return "Disconnected";
  return "Not connected";
}

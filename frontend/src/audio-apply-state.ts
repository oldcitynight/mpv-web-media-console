import type { Profile, ProfileApplyResponse } from "./api-client.ts";

export function isPhysicalAudioDevice(profile: Pick<Profile, "id" | "kind">): boolean {
  // These frozen profiles select an AO backend even when the minimal backend
  // annotates its current configured route as kind=device.
  return profile.kind === "device" && profile.id !== "linux.audio-alsa" && profile.id !== "linux.audio-pipewire";
}

export interface CommandOperation { generation: number; id: number }

/** A reply can settle only the request that still owns the current connection. */
export class CommandScope {
  generation = 0;
  private nextId = 0;
  private active: CommandOperation | null = null;

  reset(): void { this.generation += 1; this.active = null; }
  begin(): CommandOperation | null {
    if (this.active) return null;
    return this.active = { generation: this.generation, id: ++this.nextId };
  }
  isCurrent(operation: CommandOperation): boolean { return this.active === operation; }
  finish(operation: CommandOperation): boolean {
    if (!this.isCurrent(operation)) return false;
    this.active = null;
    return true;
  }
}

const AUDIO_OPTIONS = new Set(["ao", "audio-device", "audio-exclusive", "audio-fallback-to-null"]);

/** audio_sink proves audio-device only; it cannot prove AO or exclusive flags. */
export function audioReceiptState(
  receipt: ProfileApplyResponse,
  profileId: string,
  profileRoute: string | null | undefined,
  sink: string | null,
  routeChangedSinceRequest: boolean,
): "audioPending" | "audioApplied" | "audioDraft" {
  if (!receipt.accepted || !receipt.profile_ids.includes(profileId)) return "audioDraft";
  const expected = Object.entries(receipt.effective_options).filter(([key]) => AUDIO_OPTIONS.has(key));
  if (!expected.length) return "audioDraft";
  const requestedDevice = receipt.effective_options["audio-device"];
  if (profileRoute && requestedDevice !== profileRoute) return "audioPending";
  const routeConfirmed = routeChangedSinceRequest && typeof requestedDevice === "string" && sink === requestedDevice;
  const pending = Object.entries(receipt.pending_restart_options).filter(([key]) => AUDIO_OPTIONS.has(key));
  if (pending.some(([key, value]) => key !== "audio-device" || !routeConfirmed || value !== requestedDevice)) return "audioPending";
  const optionsConfirmed = receipt.applied || expected.every(([key, value]) =>
    (key === "audio-device" && routeConfirmed) || receipt.observed_options[key] === value);
  if (!optionsConfirmed) return "audioPending";
  if (typeof requestedDevice === "string" && sink !== requestedDevice) return "audioPending";
  return "audioApplied";
}

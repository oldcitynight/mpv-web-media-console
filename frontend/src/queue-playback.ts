import type { QueueState } from "./api-client.ts";
import { adjacentQueueItem, type QueueDirection } from "./remote-controls.ts";
import { toQueueViewModel } from "./view-models.ts";

export interface QueuePlaybackApi {
  getQueue(): Promise<QueueState>;
  activateQueueItem(id: string, revision?: number): Promise<QueueState>;
}

export function playQueueItem(api: QueuePlaybackApi, id: string, revision: number) {
  return api.activateQueueItem(id, revision);
}

export async function navigateQueueOnce(api: QueuePlaybackApi, direction: QueueDirection) {
  const current = await api.getQueue();
  const target = adjacentQueueItem(toQueueViewModel(current)?.items ?? [], direction);
  return target ? playQueueItem(api, target.id, current.revision) : current;
}

export function queueResponseIsCurrent(current: QueueState | null, incoming: QueueState): boolean {
  if (!current || current.id !== incoming.id) return true;
  if (incoming.revision < current.revision) return false;
  const previous = current.playback?.intent_id ?? 0;
  const next = incoming.playback?.intent_id ?? 0;
  if (next < previous) return false;
  const pending = new Set(["prepared", "submitted"]);
  if (next === previous && current.playback && incoming.playback &&
      !pending.has(current.playback.phase) && pending.has(incoming.playback.phase)) return false;
  return true;
}

export function applyQueueResponse(holder: { value: QueueState | null }, incoming: QueueState): boolean {
  if (!queueResponseIsCurrent(holder.value, incoming)) return false;
  holder.value = incoming;
  return true;
}

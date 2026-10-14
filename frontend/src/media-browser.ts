import type { MediaBrowserItem, QueueState } from "./api-client.ts";

export function toggleMediaPath(
  selectedPaths: ReadonlySet<string>,
  path: string,
  selected: boolean,
): Set<string> {
  const next = new Set(selectedPaths);
  if (selected) next.add(path);
  else next.delete(path);
  return next;
}

export function selectionForListing(
  selectedPaths: ReadonlySet<string>,
  items: readonly MediaBrowserItem[],
): Set<string> {
  const visiblePaths = new Set(
    items.filter((item) => !item.is_directory).map((item) => item.path),
  );
  return new Set([...selectedPaths].filter((path) => visiblePaths.has(path)));
}

export function orderedSelectedMediaItems(
  items: readonly MediaBrowserItem[],
  selectedPaths: ReadonlySet<string>,
): MediaBrowserItem[] {
  return items.filter((item) => !item.is_directory && selectedPaths.has(item.path));
}

export function rootsFromListing(items: readonly MediaBrowserItem[]): string[] {
  return items.filter((item) => item.is_directory).map((item) => item.path);
}

export type MediaQueueStopReason = "conflict" | "uncertain" | "refresh";

export interface MediaQueueBatchOutcome {
  added: string[];
  reconciled: string[];
  failed: Array<{ name: string; message: string }>;
  uncertain: Array<{ name: string; message: string }>;
  stopped: boolean;
  stoppedReason: MediaQueueStopReason | null;
}

export function emptyMediaQueueBatchOutcome(): MediaQueueBatchOutcome {
  return {
    added: [],
    reconciled: [],
    failed: [],
    uncertain: [],
    stopped: false,
    stoppedReason: null,
  };
}

export function recordMediaQueueFailure(
  outcome: MediaQueueBatchOutcome,
  name: string,
  message: string,
  code?: string,
): boolean {
  outcome.failed.push({ name, message });
  if (code === "conflict") {
    outcome.stopped = true;
    outcome.stoppedReason = "conflict";
  }
  return outcome.stopped;
}

export function recordMediaQueueUncertainty(
  outcome: MediaQueueBatchOutcome,
  name: string,
  message: string,
): void {
  outcome.uncertain.push({ name, message });
  outcome.stopped = true;
  outcome.stoppedReason = "uncertain";
}

/**
 * A queue snapshot confirms an append only when it contains a new server item
 * id for the requested path. A pre-existing matching path is insufficient;
 * retrying an uncertain request could otherwise create a duplicate.
 */
export function queueAddWasObserved(
  before: QueueState,
  after: QueueState,
  path: string,
): boolean {
  const beforeIds = new Set(before.items.map((item) => item.id));
  return after.items.some((item) => item.path === path && !beforeIds.has(item.id));
}

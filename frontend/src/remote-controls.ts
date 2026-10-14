import type { QueueItemViewModel } from "./view-models.ts";

export type QueueDirection = "previous" | "next";

/** Return only an adjacent item to the backend-declared active queue entry. */
export function adjacentQueueItem(
  items: readonly QueueItemViewModel[],
  direction: QueueDirection,
): QueueItemViewModel | null {
  const ordered = [...items].sort(
    (left, right) => left.ordinal - right.ordinal || left.id.localeCompare(right.id),
  );
  const activeIndex = ordered.findIndex((item) => item.active);
  if (activeIndex < 0) return null;
  const adjacentIndex = activeIndex + (direction === "previous" ? -1 : 1);
  return ordered[adjacentIndex] ?? null;
}

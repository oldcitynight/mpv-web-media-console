export interface SeekDraftState {
  active: boolean;
  position: number | null;
}

export interface SeekDraftCommit {
  state: SeekDraftState;
  target: number | null;
}

export function idleSeekDraft(): SeekDraftState {
  return { active: false, position: null };
}

function usableDuration(duration: number): boolean {
  return Number.isFinite(duration) && duration > 0;
}

function clampPosition(position: number, duration: number): number | null {
  if (!Number.isFinite(position) || !usableDuration(duration)) return null;
  return Math.min(duration, Math.max(0, position));
}

export function beginSeekDraft(
  observedPosition: number | null,
  duration: number,
): SeekDraftState {
  if (!usableDuration(duration)) return idleSeekDraft();
  const initialPosition = observedPosition !== null && Number.isFinite(observedPosition)
    ? observedPosition
    : 0;
  return { active: true, position: clampPosition(initialPosition, duration) };
}

export function updateSeekDraft(
  current: SeekDraftState,
  position: number,
  duration: number,
): SeekDraftState {
  const clamped = clampPosition(position, duration);
  if (clamped === null) return current;
  return { active: true, position: clamped };
}

export function displayedSeekPosition(
  draft: SeekDraftState,
  observedPosition: number | null,
): number | null {
  return draft.active ? draft.position : observedPosition;
}

export function commitSeekDraft(draft: SeekDraftState): SeekDraftCommit {
  return {
    state: idleSeekDraft(),
    target: draft.active ? draft.position : null,
  };
}

export function cancelSeekDraft(): SeekDraftState {
  return idleSeekDraft();
}

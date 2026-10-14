export const MEDIA_SESSION_ACTIONS = [
  "play",
  "pause",
  "seekto",
  "seekbackward",
  "seekforward",
  "previoustrack",
  "nexttrack",
] as const satisfies readonly MediaSessionAction[];

export type FixedMediaSessionAction = (typeof MEDIA_SESSION_ACTIONS)[number];
export type FixedMediaSessionHandler = (details: MediaSessionActionDetails) => void;

export interface MediaSessionSnapshot {
  title: string | null;
  playbackState: MediaSessionPlaybackState;
  positionSeconds: number | null;
  durationSeconds: number | null;
  playbackRate: number | null;
}

type MediaSessionSurface = Pick<
  MediaSession,
  "metadata" | "playbackState" | "setActionHandler" | "setPositionState"
>;
type MetadataFactory = ((title: string) => MediaMetadata) | null;

export interface MediaSessionController {
  setActionHandler(
    action: FixedMediaSessionAction,
    handler: FixedMediaSessionHandler | null,
  ): void;
  update(snapshot: MediaSessionSnapshot): void;
  destroy(): void;
}

/** A fixed, best-effort bridge to the browser Media Session API. */
export function createMediaSessionController(
  session: MediaSessionSurface | null | undefined,
  makeMetadata: MetadataFactory,
): MediaSessionController | null {
  if (!session) return null;

  const installed = new Set<FixedMediaSessionAction>();
  const unsupported = new Set<FixedMediaSessionAction>();
  let destroyed = false;

  return {
    setActionHandler(action, handler) {
      if (destroyed) return;
      if (handler === null && !installed.has(action)) return;
      if (handler !== null && unsupported.has(action)) return;
      try {
        session.setActionHandler(action, handler);
        if (handler === null) installed.delete(action);
        else installed.add(action);
      } catch {
        if (handler !== null) unsupported.add(action);
      }
    },
    update(snapshot) {
      if (destroyed) return;
      try {
        session.playbackState = snapshot.playbackState;
      } catch {
        // Older or partial implementations must not affect ordinary controls.
      }

      try {
        session.metadata = snapshot.title && makeMetadata
          ? makeMetadata(snapshot.title)
          : null;
      } catch {
        // Metadata is optional; the fixed action handlers remain useful.
      }

      const { positionSeconds, durationSeconds, playbackRate } = snapshot;
      if (
        positionSeconds !== null && Number.isFinite(positionSeconds) && positionSeconds >= 0 &&
        durationSeconds !== null && Number.isFinite(durationSeconds) && durationSeconds > 0
      ) {
        const rate = playbackRate !== null && Number.isFinite(playbackRate) && playbackRate > 0
          ? playbackRate
          : 1;
        try {
          session.setPositionState({
            duration: durationSeconds,
            position: Math.min(positionSeconds, durationSeconds),
            playbackRate: rate,
          });
        } catch {
          // Position reporting is an optional part of the browser API.
        }
      } else {
        try {
          session.setPositionState();
        } catch {
          // Position reporting is an optional part of the browser API.
        }
      }
    },
    destroy() {
      if (destroyed) return;
      for (const action of installed) {
        try {
          session.setActionHandler(action, null);
        } catch {
          // Continue clearing the remaining browser handlers.
        }
      }
      installed.clear();
      unsupported.clear();
      try {
        session.metadata = null;
      } catch {
        // Ignore a read-only/partial browser implementation.
      }
      try {
        session.playbackState = "none";
      } catch {
        // Ignore a read-only/partial browser implementation.
      }
      try {
        session.setPositionState();
      } catch {
        // Position reporting is optional.
      }
      destroyed = true;
    },
  };
}

export function createBrowserMediaSessionController(): MediaSessionController | null {
  if (typeof navigator === "undefined") return null;
  let session: MediaSession | undefined;
  try {
    session = navigator.mediaSession;
  } catch {
    return null;
  }
  if (!session) return null;
  const makeMetadata: MetadataFactory = typeof MediaMetadata === "undefined"
    ? null
    : (title) => new MediaMetadata({ title });
  return createMediaSessionController(session, makeMetadata);
}

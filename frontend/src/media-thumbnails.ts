import type { MediaBrowserItem } from "./api-client.ts";

export const MEDIA_THUMBNAIL_CONCURRENCY = 4;

export type MediaThumbnailStatus = "idle" | "queued" | "loading" | "loaded" | "error";

export interface MediaThumbnailState {
  status: MediaThumbnailStatus;
  url: string | null;
}

interface MediaThumbnailEntry extends MediaThumbnailState {
  requestId: number | null;
  error: unknown | null;
  waiters: Array<{
    resolve: (url: string) => void;
    reject: (error: unknown) => void;
  }>;
}

export interface MediaThumbnailLoaderOptions {
  loadPreview: (path: string) => Promise<Blob>;
  createObjectUrl?: (blob: Blob) => string;
  revokeObjectUrl?: (url: string) => void;
  concurrency?: number;
  onChange?: () => void;
}

export function canLoadMediaThumbnail(
  item: Pick<MediaBrowserItem, "is_directory" | "media_kind" | "has_preview" | "preview_kind">,
): boolean {
  // Listing metadata is deliberately budgeted and may be a placeholder (or
  // absent) even when the authenticated preview route can produce artwork or
  // a video frame. The browser owns a bounded, lazy queue for that fallback;
  // folders remain ineligible and the server has already filtered file kinds.
  return !item.is_directory && (item.media_kind === "audio" || item.media_kind === "video");
}

/**
 * Owns the bounded image-fetch queue used by media-list thumbnails.
 *
 * The loader deliberately owns object URLs so repeated directory navigation
 * can release stale images even when an in-flight request resolves late.
 */
export class MediaThumbnailLoader {
  private readonly loadPreview: (path: string) => Promise<Blob>;
  private readonly createObjectUrl: (blob: Blob) => string;
  private readonly revokeObjectUrl: (url: string) => void;
  private readonly concurrency: number;
  private readonly onChange: (() => void) | undefined;
  private readonly entries = new Map<string, MediaThumbnailEntry>();
  private pending: string[] = [];
  private active = 0;
  private nextRequestId = 0;
  private disposed = false;

  constructor(options: MediaThumbnailLoaderOptions) {
    this.loadPreview = options.loadPreview;
    this.createObjectUrl = options.createObjectUrl ?? ((blob) => URL.createObjectURL(blob));
    this.revokeObjectUrl = options.revokeObjectUrl ?? ((url) => URL.revokeObjectURL(url));
    this.concurrency = Math.max(1, Math.min(16, Math.floor(options.concurrency ?? MEDIA_THUMBNAIL_CONCURRENCY)));
    this.onChange = options.onChange;
  }

  state(path: string): MediaThumbnailState {
    const entry = this.entries.get(path);
    return entry ? { status: entry.status, url: entry.url } : { status: "idle", url: null };
  }

  snapshot(): Map<string, MediaThumbnailState> {
    return new Map(
      [...this.entries].map(([path, entry]) => [path, { status: entry.status, url: entry.url }]),
    );
  }

  /** Keep cache entries for the current listing and release stale entries. */
  retain(paths: Iterable<string>): void {
    const wanted = new Set(paths);
    for (const path of this.entries.keys()) {
      if (!wanted.has(path)) this.remove(path);
    }
    this.pending = this.pending.filter((path) => wanted.has(path));
    this.emit();
    this.drain();
  }

  /** Queue a visible thumbnail without retrying a known failure. */
  ensure(path: string): void {
    if (this.disposed) return;
    const entry = this.getOrCreate(path);
    if (entry.status !== "idle") return;
    entry.status = "queued";
    this.pending.push(path);
    this.emit();
    this.drain();
  }

  /** Load a preview immediately through the same bounded queue. */
  loadNow(path: string): Promise<string> {
    if (this.disposed) return Promise.reject(new Error("media thumbnail loader is disposed"));
    const entry = this.getOrCreate(path);
    if (entry.status === "loaded" && entry.url) return Promise.resolve(entry.url);
    if (entry.status === "error") {
      entry.status = "idle";
      entry.error = null;
    }
    const promise = new Promise<string>((resolve, reject) => {
      entry.waiters.push({ resolve, reject });
    });
    this.ensure(path);
    return promise;
  }

  /** Mark a browser image decode failure and release its object URL. */
  markError(path: string, error = new Error("media thumbnail image failed to decode")): void {
    const entry = this.entries.get(path);
    if (!entry) return;
    if (entry.url) this.revokeObjectUrl(entry.url);
    entry.url = null;
    entry.status = "error";
    entry.error = error;
    this.rejectWaiters(entry, error);
    this.emit();
  }

  remove(path: string): void {
    const entry = this.entries.get(path);
    if (!entry) return;
    this.entries.delete(path);
    this.pending = this.pending.filter((candidate) => candidate !== path);
    if (entry.url) this.revokeObjectUrl(entry.url);
    this.rejectWaiters(entry, new Error("media thumbnail is no longer listed"));
    this.emit();
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.pending = [];
    for (const entry of this.entries.values()) {
      if (entry.url) this.revokeObjectUrl(entry.url);
      this.rejectWaiters(entry, new Error("media thumbnail loader is disposed"));
    }
    this.entries.clear();
    this.emit();
  }

  private getOrCreate(path: string): MediaThumbnailEntry {
    const existing = this.entries.get(path);
    if (existing) return existing;
    const entry: MediaThumbnailEntry = {
      status: "idle",
      url: null,
      requestId: null,
      error: null,
      waiters: [],
    };
    this.entries.set(path, entry);
    return entry;
  }

  private drain(): void {
    if (this.disposed) return;
    while (this.active < this.concurrency && this.pending.length) {
      const path = this.pending.shift();
      if (!path) continue;
      const entry = this.entries.get(path);
      if (!entry || entry.status !== "queued") continue;
      const requestId = ++this.nextRequestId;
      entry.requestId = requestId;
      entry.status = "loading";
      this.active += 1;
      this.emit();
      void this.loadPreview(path)
        .then((blob) => {
          const url = this.createObjectUrl(blob);
          const current = this.entries.get(path);
          if (!current || current.requestId !== requestId || this.disposed) {
            this.revokeObjectUrl(url);
            return;
          }
          current.url = url;
          current.status = "loaded";
          current.error = null;
          this.resolveWaiters(current, url);
        })
        .catch((error: unknown) => {
          const current = this.entries.get(path);
          if (!current || current.requestId !== requestId || this.disposed) return;
          current.url = null;
          current.status = "error";
          current.error = error;
          this.rejectWaiters(current, error);
        })
        .finally(() => {
          const current = this.entries.get(path);
          if (current?.requestId === requestId) current.requestId = null;
          this.active -= 1;
          this.emit();
          this.drain();
        });
    }
  }

  private resolveWaiters(entry: MediaThumbnailEntry, url: string): void {
    const waiters = entry.waiters.splice(0);
    for (const waiter of waiters) waiter.resolve(url);
  }

  private rejectWaiters(entry: MediaThumbnailEntry, error: unknown): void {
    const waiters = entry.waiters.splice(0);
    for (const waiter of waiters) waiter.reject(error);
  }

  private emit(): void {
    this.onChange?.();
  }
}

<script setup lang="ts">
import { playQueueItem, applyQueueResponse, navigateQueueOnce, queueResponseIsCurrent } from "./queue-playback.ts";
import { computed, onBeforeUnmount, ref, watch } from "vue";
import {
  ApiError,
  createApiClient,
  type Chapter,
  type MediaBrowserItem,
  type MediaBrowserResponse,
  type Profile,
  type OutputInventory,
  type ProfileApplyResponse,
  type QueueState,
  type Track,
} from "./api-client.ts";
import { createPlayerCommands, type PlayerCommands } from "./commands.ts";
import {
  connectionLabel,
  formatPlaybackTime,
  mediaTitle,
  SKIP_INTERVAL_SECONDS,
  seekTo,
  setPlaybackSpeed,
  setPlaybackVolume,
  skipPlayback,
  toggleMute,
  togglePlayback,
} from "./player-controls.ts";
import { PlayerStateStore, type PlayerStoreView } from "./player-state-store.ts";
import { CommandScope, audioReceiptState, isPhysicalAudioDevice } from "./audio-apply-state.ts";
import {
  createBrowserMediaSessionController,
  type FixedMediaSessionAction,
  type FixedMediaSessionHandler,
  type MediaSessionController,
} from "./media-session.ts";
import {
  toChapterViewModels,
  toOutputProfileViewModels,
  toPlaybackViewModel,
  toProfileApplySummaryViewModel,
  toQueueViewModel,
  toRemoteOutputStatusViewModel,
  toTrackViewModels,
  type ChapterViewModel,
  type QueueItemViewModel,
  type TrackViewModel,
} from "./view-models.ts";
import {
  resolveLocale,
  translate,
  type AppLocale,
  type MessageKey,
} from "./i18n.ts";
import { apiErrorMessageKey, connectionErrorMessage, queueErrorMessage } from "./error-presentation.ts";
import { videoReceiptState } from "./video-apply-state.ts";
import AllFilesDialog from "./AllFilesDialog.vue";
import RemoteView from "./Remote.vue";
import TransportBar, { type TransportBarLabels } from "./TransportBar.vue";
import { adjacentQueueItem, type QueueDirection } from "./remote-controls.ts";
import SeekBar from "./SeekBar.vue";
import AudioSelector from "./AudioSelector.vue";
import SubtitleSelector from "./SubtitleSelector.vue";
import BrowserPager from "./BrowserPager.vue";
import { sinkLabel } from "./status-presentation.ts";
import {
  emptyMediaQueueBatchOutcome,
  queueAddWasObserved,
  rootsFromListing,
  recordMediaQueueFailure,
  recordMediaQueueUncertainty,
  toggleMediaPath,
  type MediaQueueBatchOutcome,
} from "./media-browser.ts";
import { presentationSummary, trackCodecLabel } from "./media-presentation.ts";
import {
  canLoadMediaThumbnail,
  MediaThumbnailLoader,
  type MediaThumbnailState,
} from "./media-thumbnails.ts";

const pages = ["Remote", "Library", "Queue", "Info"] as const;
const pageMessageKeys: Record<(typeof pages)[number], MessageKey> = {
  Remote: "remote",
  Library: "localLibrary",
  Queue: "queue",
  Info: "info",
};
const activePage = ref<(typeof pages)[number]>("Remote");
function initialLocale(): AppLocale {
  let savedLocale: string | null = null;
  try {
    savedLocale = window.localStorage.getItem("mpv-web-locale");
  } catch {
    // Browser storage may be disabled; language selection still works in memory.
  }
  const browserLocales = navigator.languages?.length ? navigator.languages : [navigator.language];
  return resolveLocale(savedLocale, browserLocales);
}

const locale = ref<AppLocale>(initialLocale());
function t(key: MessageKey, values: Record<string, string | number> = {}): string {
  return translate(locale.value, key, values);
}

watch(
  locale,
  (nextLocale) => {
    document.documentElement.lang = nextLocale;
    const description = document.querySelector('meta[name="description"]');
    description?.setAttribute("content", translate(nextLocale, "appDescription"));
    try {
      window.localStorage.setItem("mpv-web-locale", nextLocale);
    } catch {
      // The selected language remains active for this page session.
    }
  },
  { immediate: true },
);

const accessToken = ref("");
const state = ref<PlayerStoreView>({
  connection: "idle",
  snapshot: null,
  lastSequence: null,
  lastEventType: null,
  error: null,
});
const commandPending = ref(false);
const commandError = ref<MessageKey | null>(null);
const secondaryError = ref<MessageKey | null>(null);
const secondaryLoading = ref(false);
const lastAudibleVolume = ref(50);
const tracks = ref<Track[]>([]);
const chapters = ref<Chapter[]>([]);
const mediaBrowser = ref<MediaBrowserResponse | null>(null);
const mediaSearch = ref("");
const mediaSort = ref("name");
const mediaOffset = ref(0);
const coarsePointer = window.matchMedia("(pointer: coarse)");
const browserRowCount = () => window.innerWidth >= 1100 && !coarsePointer.matches ? 20 : 5;
const mediaRows = ref(browserRowCount());
const allFilesOpen = ref(false);
let modalHistoryPending = false;
let reopenAllFiles = false;
const modalHistoryId = `mpv-all-files-${Array.from(crypto.getRandomValues(new Uint32Array(4)), value => value.toString(16).padStart(8, "0")).join("")}`;
function openAllFiles() {
  if (modalHistoryPending) { reopenAllFiles = true; return; }
  if (allFilesOpen.value) return;
  window.history.pushState({ ...window.history.state, mpvAllFiles: modalHistoryId }, "");
  allFilesOpen.value = true;
}
function closeAllFiles() {
  allFilesOpen.value = false;
  if (!modalHistoryPending && window.history.state?.mpvAllFiles === modalHistoryId) {
    modalHistoryPending = true; window.history.back();
  }
}
function modalHistoryChange() {
  if (window.history.state?.mpvAllFiles === modalHistoryId && mediaBrowser.value && canControl.value) { modalHistoryPending = false; allFilesOpen.value = true; return; }
  allFilesOpen.value = false; modalHistoryPending = false;
  if (reopenAllFiles) { reopenAllFiles = false; openAllFiles(); }
}
window.addEventListener("popstate", modalHistoryChange);

const selectedMediaEntries = ref<Map<string, MediaBrowserItem>>(new Map());
const allMediaSelection = ref<{ total: number; files: number; version: string; excluded: Set<string> } | null>(null);
function resizeMediaPage() {
  const rows = browserRowCount();
  if (rows === mediaRows.value) return;
  mediaRows.value = rows;
  const offset = Math.floor(mediaOffset.value / rows) * rows;
  void loadMediaBrowser(requestedMediaDirectory, mediaSearch.value, offset);
}
window.addEventListener("resize", resizeMediaPage);
coarsePointer.addEventListener("change", resizeMediaPage);

let mediaPageAbort: AbortController | null = null;
const mediaBrowserLoading = ref(false);
const mediaBrowserError = ref<MessageKey | null>(null);
const mediaRootPaths = ref<string[]>([]);
const mediaRootLabels = ref<Record<string, string>>({});
const selectedMediaPaths = ref<Set<string>>(new Set());
const mediaPreviewItem = ref<MediaBrowserItem | null>(null);
const mediaPreviewUrl = ref<string | null>(null);
const mediaPreviewLoading = ref(false);
const mediaPreviewError = ref<MessageKey | null>(null);
const mediaThumbnailStates = ref<Map<string, MediaThumbnailState>>(new Map());
const mediaQueueBatchPending = ref(false);
const mediaQueueFeedback = ref<MediaQueueBatchOutcome | null>(null);
const queue = ref<QueueState | null>(null);
const profiles = ref<Profile[]>([]);
const outputInventory = ref<OutputInventory | null>(null);
const selectedProfileIds = ref<string[]>([]);
const audioApplyFailed = ref(false);
const audioApplyPending = ref(false);
const audioApplyResult = ref<ProfileApplyResponse | null>(null);
const audioRouteRevision = ref(0);
let audioRequestRouteRevision = 0;
let audioSelectionRevision = 0;
const videoApplyPending = ref(false), videoApplyFailed = ref(false);
const videoApplyResult = ref<ProfileApplyResponse | null>(null);
let videoSelectionRevision = 0;
const videoProfiles = computed(() => profileViews.value.filter(profile => profile.category === "display"));
const selectedVideoId = computed(() => selectedProfileIds.value.find(id => profiles.value.find(profile => profile.id === id)?.category === "display") ?? "");
const selectedVideoProfile = computed(() => videoProfiles.value.find(profile => profile.id === selectedVideoId.value));
const videoApplyState = computed(() => {
  if (!canControl.value) return "audioDisconnected";
  if (!selectedVideoProfile.value) return "videoChoose";
  if (!selectedVideoProfile.value.enabled) return "audioUnavailable";
  if (videoApplyPending.value) return "audioApplying";
  if (videoApplyFailed.value) return "audioFailed";
  const receipt = videoApplyResult.value;
  if (!receipt?.profile_ids.includes(selectedVideoId.value)) return "audioDraft";
  return videoReceiptState(receipt, selectedVideoId.value, selectedVideoProfile.value.kind === "device" ? selectedVideoProfile.value.route : null);
});
function selectVideo(event: Event) {
  videoSelectionRevision++;
  videoApplyResult.value = null; videoApplyFailed.value = false;
  selectedProfileIds.value = selectedProfileIds.value.filter(id => profiles.value.find(profile => profile.id === id)?.category !== "display");
  const id = (event.target as HTMLSelectElement).value;
  if (id) selectedProfileIds.value.push(id);
}
function videoOptionLabel(profile: Profile) {
  const label = localizedProfileName(profile);
  const duplicate = videoProfiles.value.filter(item => localizedProfileName(item) === label).length > 1;
  return `${label}${duplicate && profile.route ? ` · ${profile.route}` : ""}${profile.enabled ? "" : ` · ${t("audioUnavailableShort")}`}`;
}

let audioApplyOperation = 0;
const profileApplyResult = ref<ProfileApplyResponse | null>(null);
const selectedAudioId = computed(() => selectedProfileIds.value.find(id => profiles.value.find(profile => profile.id === id)?.category === "audio") ?? "");
const audioProfiles = computed(() => profileViews.value.filter(profile => profile.category === "audio"));
const selectedAudioProfile = computed(() => audioProfiles.value.find(profile => profile.id === selectedAudioId.value));
const appliedAudioRoute = computed(() => canControl.value ? state.value.snapshot?.output_status?.audio_route ?? null : null);
const appliedAudioLabel = computed(() => canControl.value ? sinkLabel(state.value.snapshot?.output_status?.audio_sink, appliedAudioRoute.value, locale.value) : t("audioDisconnected"));
const audioApplyState = computed(() => {
  if (!canControl.value) return "audioDisconnected";
  if (!selectedAudioProfile.value) return "audioChoose";
  if (!selectedAudioProfile.value.enabled) return "audioUnavailable";
  if (audioApplyPending.value) return "audioApplying";
  if (audioApplyFailed.value) return "audioFailed";
  if (audioApplyResult.value?.profile_ids.includes(selectedAudioId.value)) {
    return audioReceiptState(audioApplyResult.value, selectedAudioId.value,
      isPhysicalAudioDevice(selectedAudioProfile.value) ? selectedAudioProfile.value.route : null,
      appliedAudioRoute.value, audioRouteRevision.value > audioRequestRouteRevision);
  }
  return "audioDraft";
});
const speedChoices = [0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4];
const playbackView = computed(() => toPlaybackViewModel(state.value.snapshot));
const transportLabels = computed<TransportBarLabels>(() => ({
  controls: t("playbackControls"),
  previous: t("previousQueueItem"),
  previousShort: t("previousShort"),
  skipBackward: t("skipBackward"),
  skipBackwardShort: t("skipBackwardShort"),
  play: t("play"),
  pause: t("pause"),
  skipForward: t("skipForward"),
  skipForwardShort: t("skipForwardShort"),
  next: t("nextQueueItem"),
  nextShort: t("nextShort"),
  sending: t("sendingCommand"),
}));
const remoteOutputStatus = computed(() =>
  toRemoteOutputStatusViewModel(state.value.snapshot?.output_status ?? null),
);
const trackViews = computed(() => toTrackViewModels(tracks.value));
const audioTrackViews = computed(() => trackViews.value.filter((track) => track.type === "audio"));
const subtitleTrackViews = computed(() => trackViews.value.filter((track) => track.type === "sub"));
const otherTrackViews = computed(() =>
  trackViews.value.filter((track) => track.type !== "audio" && track.type !== "sub"),
);
const chapterViews = computed(() => toChapterViewModels(chapters.value));
const queueView = computed(() => toQueueViewModel(queue.value));
const profileViews = computed(() => toOutputProfileViewModels(profiles.value));
const profileApplySummary = computed(() =>
  profileApplyResult.value?.profile_ids.some(id => profiles.value.find(profile => profile.id === id)?.category !== "audio")
    ? toProfileApplySummaryViewModel(profileApplyResult.value) : null,
);
const canControl = computed(
  () => state.value.connection === "open" && state.value.snapshot !== null,
);
const playerTitle = computed(() => {
  const path = playbackView.value?.media.path ?? null;
  return path ? mediaTitle(path, locale.value) : t("noMediaLoaded");
});
const speedOptions = computed(() => {
  const current = playbackView.value?.controls.speedMultiplier;
  return current === null || current === undefined || speedChoices.includes(current)
    ? speedChoices
    : [...speedChoices, current].sort((left, right) => left - right);
});
const displayedSpeed = computed(() => String(playbackView.value?.controls.speedMultiplier ?? 1));
const localizedConnectionLabel = computed(() => {
  const keyByLabel: Record<string, MessageKey> = {
    "Authentication failed": "authenticationFailed",
    "Player degraded": "playerDegraded",
    Connected: "connected",
    Reconnecting: "reconnecting",
    Connecting: "connecting",
    "Needs attention": "needsAttention",
    Disconnected: "disconnected",
    "Not connected": "notConnected",
  };
  return t(keyByLabel[connectionLabel(state.value)] ?? "notConnected");
});
const orderedQueueItems = computed(() =>
  [...(queueView.value?.items ?? [])].sort((left, right) => left.ordinal - right.ordinal),
);
const previousQueueItem = computed(() => adjacentQueueItem(orderedQueueItems.value, "previous"));
const nextQueueItem = computed(() => adjacentQueueItem(orderedQueueItems.value, "next"));
const visibleMediaFiles = computed(() =>
  (mediaBrowser.value?.items ?? []).filter((item) => !item.is_directory),
);
const selectedMediaItems = computed(() => {
  const entries = [...selectedMediaEntries.value.values()].filter(item => selectedMediaPaths.value.has(item.path));
  const compare = (a: string,b: string) => a < b ? -1 : a > b ? 1 : 0;
  return entries.sort((a,b) => mediaSort.value === 'modified' ? compare(b.modified_at ?? '',a.modified_at ?? '') || compare(a.name,b.name) : (mediaSort.value === 'name_desc' ? -1 : 1) * (compare(a.name.toLowerCase(),b.name.toLowerCase()) || compare(a.name,b.name) || compare(a.path,b.path)));
});
const selectedMediaCount = computed(() => allMediaSelection.value ? allMediaSelection.value.total - allMediaSelection.value.excluded.size : selectedMediaItems.value.length);
const allVisibleMediaSelected = computed(
  () => visibleMediaFiles.value.length > 0 && visibleMediaFiles.value.every((item) => isMediaSelected(item)),
);
const mediaBreadcrumbs = computed(() => {
  const directory = mediaBrowser.value?.directory;
  if (!directory) return [{ label: t("mediaRoots"), path: null as string | null }];
  const root = mediaRootPaths.value.find(
    (candidate) => directory === candidate || directory.startsWith(`${candidate}/`),
  );
  if (!root) return [{ label: directory, path: directory }];
  const relative = directory.slice(root.length).split("/").filter(Boolean);
  const breadcrumbs = [{ label: mediaRootLabels.value[root] ?? root.split("/").filter(Boolean).at(-1) ?? root, path: root }];
  let current = root;
  for (const segment of relative) {
    current += `/${segment}`;
    breadcrumbs.push({ label: segment, path: current });
  }
  return breadcrumbs;
});


function pageLabel(page: (typeof pages)[number]): string {
  return t(pageMessageKeys[page]);
}

function localizedPlayerStatus(status: string): string {
  const keys: Record<string, MessageKey> = {
    playing: "playing",
    paused: "paused",
    stopped: "stopped",
    idle: "idle",
    error: "errorStatus",
    loading: "loadingStatus",
    recovering: "recoveringStatus",
    ended: "endedStatus",
  };
  const key = Object.hasOwn(keys, status.toLowerCase()) ? keys[status.toLowerCase()] : null;
  return key ? t(key) : t("unknownStatus");
}

function localizedTrackType(type: string): string {
  const keys: Record<string, MessageKey> = {
    audio: "trackAudio",
    video: "trackVideo",
    sub: "trackSubtitle",
  };
  const key = Object.hasOwn(keys, type.toLowerCase()) ? keys[type.toLowerCase()] : null;
  return key ? t(key) : t("trackOther");
}

function localizedProfileName(profile: Profile): string {
  const keys: Record<string, MessageKey> = {
    "linux.audio-alsa": "profileAlsa", "linux.audio-pipewire": "profilePipeWire",
    "linux.desktop": "profileDesktop", "linux.drm-direct": "profileDrm",
    "linux.headless": "profileHeadless", "video.vaapi": "profileVaapiVideo",
    "display.vaapi-drm": "profileVaapiDrm",
  };
  return Object.hasOwn(keys, profile.id) ? t(keys[profile.id]) : profile.name;
}

function profileRole(profile: Profile): string {
  const keys: Record<string, MessageKey> = {
    backend: "outputBackend", device: "outputDevice", decoder: "outputDecoder",
    render_context: "outputRenderContext", no_output: "outputNoVideo", profile: "outputProfile",
  };
  const kind = profile.kind ?? "profile";
  return t(Object.hasOwn(keys, kind) ? keys[kind] : "outputProfile");
}

function localizedApiError(error: ApiError): MessageKey {
  return apiErrorMessageKey(error);
}

const localizedConnectionError = computed(() => connectionErrorMessage(state.value.error, locale.value));

let store: PlayerStateStore | null = null;
let unsubscribe: (() => void) | null = null;
let apiClient: ReturnType<typeof createApiClient> | null = null;
const commandScope = new CommandScope();
let mediaSession: MediaSessionController | null = null;
let mediaThumbnailLoader: MediaThumbnailLoader | null = null;
let mediaThumbnailObserver: IntersectionObserver | null = null;
let mediaThumbnailFallbackHandler: (() => void) | null = null;
const mediaThumbnailTargets = new Map<string, Element>();
let pageLoadGeneration = 0;
let mediaLoadGeneration = 0;
let requestedMediaDirectory: string | null = null;
let mediaPreviewGeneration = 0;
let infoTrackRefreshGeneration = 0;
const mediaDirectoryLabel = computed(() => {
  const directory = mediaBrowser.value?.directory;
  if (!directory) return t("mediaRoots");
  const segments = directory.split(/[\\/]/).filter(Boolean);
  return segments[segments.length - 1] ?? t("mediaRoots");
});

watch(
  () => state.value.snapshot?.volume,
  (volume) => {
    if (volume !== null && volume !== undefined && Number.isFinite(volume) && volume > 0) {
      lastAudibleVolume.value = volume;
    }
  },
);

watch(
  [activePage, canControl],
  ([page, ready]) => {
    if (!ready) return;
    if (page === "Remote") void loadRemoteQueue();
    else void loadPageData(page);
  },
);

watch(
  [() => state.value.lastEventType, () => state.value.lastSequence],
  ([eventType, sequence], [_previousEventType, previousSequence]) => {
    if (sequence !== previousSequence && ["media.started", "native.end-file", "native.terminal-pending", "load.accepted", "stopped"].includes(eventType ?? "")) {
      if (activePage.value === "Queue") void loadPageData("Queue");
      else if (activePage.value === "Remote") void loadRemoteQueue();
    }
    if (
      sequence !== previousSequence &&
      (eventType === "track" || eventType === "output.status" || eventType === "file-loaded") &&
      activePage.value === "Remote"
    ) {
      void refreshInfoTracksFromStatus();
    }
  },
);

watch(
  [canControl, playbackView, previousQueueItem, nextQueueItem],
  () => syncMediaSession(),
  { immediate: true },
);

function resetConnectionCommands(): void {
  commandScope.reset();
  videoApplyPending.value = false; videoApplyFailed.value = false; videoApplyResult.value = null;
  commandPending.value = false;
  commandError.value = null;
  audioApplyOperation += 1;
  audioApplyResult.value = null;
  profileApplyResult.value = null;
  audioApplyPending.value = false;
  audioApplyFailed.value = false;
  audioRouteRevision.value = 0;
}

function disconnect(): void {
  resetConnectionCommands();
  closeMediaPreview();
  clearMediaThumbnailTargets();
  mediaThumbnailLoader?.dispose();
  mediaThumbnailLoader = null;
  mediaThumbnailStates.value = new Map();
  mediaSession?.destroy();
  mediaSession = null;
  unsubscribe?.();
  unsubscribe = null;
  store?.stop();
  store = null;
  apiClient = null;
  commandPending.value = false;
  commandError.value = null;
  secondaryError.value = null;
  secondaryLoading.value = false;
  pageLoadGeneration += 1;
  infoTrackRefreshGeneration += 1;
  tracks.value = [];
  chapters.value = [];
  mediaBrowser.value = null;
  mediaSearch.value = "";
  mediaBrowserLoading.value = false;
  mediaBrowserError.value = null;
  mediaRootPaths.value = [];
  mediaRootLabels.value = {};
  selectedMediaPaths.value = new Set();
  selectedMediaEntries.value = new Map();
  allMediaSelection.value = null;
  mediaQueueFeedback.value = null;
  mediaQueueBatchPending.value = false;
  mediaLoadGeneration += 1;
  mediaPageAbort?.abort(); mediaPageAbort = null;
  requestedMediaDirectory = null;
  reopenAllFiles = false;
  closeAllFiles();
  queue.value = null;
  // Output drafts survive reconnect; fresh discovery revalidates availability.
  outputInventory.value = null;
  profileApplyResult.value = null;
  audioApplyResult.value = null;
  audioApplyPending.value = false;
  audioApplyFailed.value = false;
  state.value = {
    connection: "closed",
    snapshot: null,
    lastSequence: null,
    lastEventType: null,
    error: null,
  };
}

function connect(): void {
  disconnect();
  const token = accessToken.value;
  if (token.trim() !== token) {
    state.value = {
      connection: "error",
      snapshot: null,
      lastSequence: null,
      lastEventType: null,
      error: "token_whitespace",
    };
    return;
  }
  try {
    const authOptions = token ? { token } : {};
    apiClient = createApiClient({ baseUrl: window.location.origin, ...authOptions });
    mediaThumbnailLoader = new MediaThumbnailLoader({
      loadPreview: (path) => apiClient?.getMediaPreview(path) ?? Promise.reject(new Error("media API is unavailable")),
      onChange: () => {
        mediaThumbnailStates.value = mediaThumbnailLoader?.snapshot() ?? new Map();
      },
    });
    mediaSession = createBrowserMediaSessionController();
    installMediaSessionActions();
    const connectionStore = new PlayerStateStore({ baseUrl: window.location.origin, ...authOptions });
    store = connectionStore;
    let wasReady = false;
    unsubscribe = connectionStore.subscribe((next) => {
      if (store !== connectionStore) return;
      const ready = next.connection === "open" && next.snapshot !== null;
      if (wasReady && !ready) resetConnectionCommands();
      if (ready && wasReady && next.snapshot?.output_status?.audio_route !== state.value.snapshot?.output_status?.audio_route) {
        audioRouteRevision.value += 1;
      }
      wasReady = ready;
      state.value = next;
    });
    store.start();
    accessToken.value = "";
  } catch {
    disconnect();
    state.value = {
      connection: "error",
      snapshot: null,
      lastSequence: null,
      lastEventType: null,
      error: "connection_start_failed",
    };
  }
}

function setMediaSessionAction(
  action: FixedMediaSessionAction,
  handler: FixedMediaSessionHandler | null,
): void {
  mediaSession?.setActionHandler(action, handler);
}

function installMediaSessionActions(): void {
  if (!mediaSession) return;
  setMediaSessionAction("play", () => {
    void issuePlayerCommand((commands) => commands.play());
  });
  setMediaSessionAction("pause", () => {
    void issuePlayerCommand((commands) => commands.pause());
  });
  setMediaSessionAction("seekto", ({ seekTime }) => {
    const duration = playbackView.value?.controls.durationSeconds ?? null;
    if (typeof seekTime !== "number" || !Number.isFinite(seekTime)) return;
    void issuePlayerCommand((commands) => seekTo(commands, seekTime, duration));
  });
  setMediaSessionAction("seekbackward", ({ seekOffset }) => {
    issueMediaSessionRelativeSeek("backward", seekOffset);
  });
  setMediaSessionAction("seekforward", ({ seekOffset }) => {
    issueMediaSessionRelativeSeek("forward", seekOffset);
  });
}

function issueMediaSessionRelativeSeek(
  direction: "backward" | "forward",
  requestedOffset: number | undefined,
): void {
  const positiveOffset = typeof requestedOffset === "number" &&
    Number.isFinite(requestedOffset) && requestedOffset > 0
    ? Math.min(requestedOffset, 3600)
    : SKIP_INTERVAL_SECONDS;
  void issuePlayerCommand((commands) =>
    commands.seekRelative(direction === "backward" ? -positiveOffset : positiveOffset),
  );
}

function syncMediaSession(): void {
  if (!mediaSession) return;
  const playback = playbackView.value;
  const path = playback?.media.path ?? null;
  const status = playback?.player.status.toLowerCase();
  const playbackState = !canControl.value || !playback
    ? "none"
    : playback.controls.paused || status === "paused"
      ? "paused"
      : status === "playing"
        ? "playing"
        : "none";
  mediaSession.update({
    title: canControl.value && path ? mediaTitle(path, locale.value) : null,
    playbackState,
    positionSeconds: canControl.value ? playback?.controls.positionSeconds ?? null : null,
    durationSeconds: canControl.value ? playback?.controls.durationSeconds ?? null : null,
    playbackRate: canControl.value ? playback?.controls.speedMultiplier ?? null : null,
  });
  setMediaSessionAction(
    "previoustrack",
    canControl.value && previousQueueItem.value
      ? () => void navigateQueue("previous")
      : null,
  );
  setMediaSessionAction(
    "nexttrack",
    canControl.value && nextQueueItem.value
      ? () => void navigateQueue("next")
      : null,
  );
}

async function issueCommand<T>(
  command: (api: ReturnType<typeof createApiClient>) => Promise<T>,
): Promise<T | undefined> {
  return runCommand((api) => command(api));
}

async function issuePlayerCommand<T>(
  command: (commands: PlayerCommands) => Promise<T>,
): Promise<T | undefined> {
  return runCommand((api) => command(createPlayerCommands(api)));
}

async function runCommand<T>(
  command: (api: ReturnType<typeof createApiClient>) => Promise<T>,
): Promise<T | undefined> {
  if (!apiClient || !canControl.value || commandPending.value) return undefined;
  const connection = apiClient;
  const operation = commandScope.begin();
  if (!operation) return undefined;
  commandPending.value = true;
  commandError.value = null;
  try {
    const result = await command(connection);
    return commandScope.isCurrent(operation) ? result : undefined;
  } catch (error) {
    if (!commandScope.isCurrent(operation)) return undefined;
    commandError.value =
      error instanceof ApiError ? localizedApiError(error) : "commandError";
    if (error instanceof ApiError && error.code === "conflict") {
      if (activePage.value === "Queue") void loadPageData("Queue");
      else if (activePage.value === "Remote") void loadRemoteQueue();
    }
    return undefined;
  } finally {
    if (commandScope.finish(operation)) commandPending.value = false;
  }
}

function onTimelineSeek(target: number): void {
  const snapshot = state.value.snapshot;
  if (!snapshot) return;
  void issuePlayerCommand((commands) => seekTo(commands, target, snapshot.duration));
}

function onVolumeChange(event: Event): void {
  const input = event.target;
  if (!(input instanceof HTMLInputElement)) return;
  const volume = Number(input.value);
  if (volume > 0) lastAudibleVolume.value = volume;
  void issuePlayerCommand((commands) => setPlaybackVolume(commands, volume));
}

function onSpeedChange(event: Event): void {
  const input = event.target;
  if (!(input instanceof HTMLSelectElement)) return;
  void issuePlayerCommand((commands) => setPlaybackSpeed(commands, Number(input.value)));
}

function onPlaybackToggle(): void {
  void issuePlayerCommand((commands) => togglePlayback(commands, state.value.snapshot));
}

function onSkip(direction: "backward" | "forward"): void {
  const position = state.value.snapshot?.position ?? null;
  void issuePlayerCommand((commands) => skipPlayback(commands, position, direction));
}

function onMuteToggle(): void {
  const volume = state.value.snapshot?.volume;
  if (volume === null || volume === undefined) return;
  void issuePlayerCommand((commands) => toggleMute(commands, volume, lastAudibleVolume.value));
}

async function loadPageData(page: (typeof pages)[number]): Promise<void> {
  const api = apiClient;
  if (!api || !canControl.value || page === "Remote") return;
  const generation = ++pageLoadGeneration;
  const controlGeneration = commandScope.generation;
  const trackGeneration = ++infoTrackRefreshGeneration;
  secondaryLoading.value = true;
  secondaryError.value = null;
  try {
    if (page === "Library") {
      void loadMediaBrowser(mediaBrowser.value?.directory ?? null);
    } else if (page === "Queue") {
      const nextQueue = await api.getQueue();
      if (generation !== pageLoadGeneration || controlGeneration !== commandScope.generation || !queueResponseIsCurrent(queue.value, nextQueue)) return;
      queue.value = nextQueue;
    } else {
      const [nextProfiles, nextTracks, nextOutputs] = await Promise.all([api.getProfiles(), api.getTracks(), api.getOutputs()]);
      if (generation !== pageLoadGeneration) return;
      outputInventory.value = nextOutputs;
      if (trackGeneration === infoTrackRefreshGeneration) tracks.value = nextTracks;
      // Keep selected unavailable identities visible, so hotplug cannot silently
      // redirect or clear a user's selection.
      for (const previous of profiles.value) {
        if (selectedProfileIds.value.includes(previous.id) && !nextProfiles.some(profile => profile.id === previous.id))
          nextProfiles.push({ ...previous, enabled: false, device_reported: false });
      }
      profiles.value = nextProfiles;
    }
  } catch (error) {
    if (generation === pageLoadGeneration) {
      secondaryError.value =
        error instanceof ApiError ? localizedApiError(error) : "sectionLoadError";
    }
  } finally {
    if (generation === pageLoadGeneration) secondaryLoading.value = false;
  }
}

async function refreshInfoTracksFromStatus(): Promise<void> {
  const api = apiClient;
  if (!api || !canControl.value || activePage.value !== "Remote") return;
  const generation = ++infoTrackRefreshGeneration;
  try {
    const [nextTracks, nextChapters] = await Promise.all([api.getTracks(), api.getChapters()]);
    if (generation === infoTrackRefreshGeneration && api === apiClient && canControl.value && activePage.value === "Remote") {
      tracks.value = nextTracks;
      chapters.value = nextChapters;
    }
  } catch (error) {
    if (generation === infoTrackRefreshGeneration && api === apiClient && canControl.value && activePage.value === "Remote") {
      secondaryError.value =
        error instanceof ApiError ? localizedApiError(error) : "sectionLoadError";
    }
  }
}

type MediaThumbnailDisplayState =
  | MediaThumbnailState
  | { status: "unavailable"; url: null };

function clearMediaThumbnailTargets(): void {
  mediaThumbnailObserver?.disconnect();
  mediaThumbnailObserver = null;
  mediaThumbnailTargets.clear();
  if (mediaThumbnailFallbackHandler) {
    window.removeEventListener("scroll", mediaThumbnailFallbackHandler);
    window.removeEventListener("resize", mediaThumbnailFallbackHandler);
    mediaThumbnailFallbackHandler = null;
  }
}

function scanMediaThumbnailTargets(): void {
  const loader = mediaThumbnailLoader;
  if (!loader) return;
  const viewportBottom = window.innerHeight + 240;
  for (const [path, target] of mediaThumbnailTargets) {
    const bounds = target.getBoundingClientRect();
    if (bounds.top < viewportBottom && bounds.bottom > -240) loader.ensure(path);
  }
}

function ensureMediaThumbnailObserver(): void {
  if (mediaThumbnailObserver || mediaThumbnailFallbackHandler) return;
  if (typeof IntersectionObserver !== "undefined") {
    mediaThumbnailObserver = new IntersectionObserver(
      (entries) => {
        const loader = mediaThumbnailLoader;
        if (!loader) return;
        for (const entry of entries) {
          if (!entry.isIntersecting && entry.intersectionRatio <= 0) continue;
          const path = entry.target.getAttribute("data-media-thumbnail-path");
          if (path) loader.ensure(path);
        }
      },
      { rootMargin: "240px 0px", threshold: 0.01 },
    );
    for (const target of mediaThumbnailTargets.values()) mediaThumbnailObserver.observe(target);
    return;
  }
  mediaThumbnailFallbackHandler = () => scanMediaThumbnailTargets();
  window.addEventListener("scroll", mediaThumbnailFallbackHandler, { passive: true });
  window.addEventListener("resize", mediaThumbnailFallbackHandler);
  scanMediaThumbnailTargets();
}

function setMediaThumbnailTarget(path: string, element: unknown): void {
  const target =
    element && typeof element === "object" && "getBoundingClientRect" in element
      ? (element as Element)
      : null;
  const previous = mediaThumbnailTargets.get(path);
  if (previous && previous !== target) mediaThumbnailObserver?.unobserve(previous);
  if (!target) {
    mediaThumbnailTargets.delete(path);
    return;
  }
  mediaThumbnailTargets.set(path, target);
  ensureMediaThumbnailObserver();
  mediaThumbnailObserver?.observe(target);
  if (mediaThumbnailFallbackHandler) scanMediaThumbnailTargets();
}

function retainMediaThumbnailListing(items: MediaBrowserItem[]): void {
  const paths = new Set(
    items.filter((item) => canLoadMediaThumbnail(item)).map((item) => item.path),
  );
  for (const [path, target] of mediaThumbnailTargets) {
    if (paths.has(path)) continue;
    mediaThumbnailObserver?.unobserve(target);
    mediaThumbnailTargets.delete(path);
  }
  mediaThumbnailLoader?.retain(paths);
  mediaThumbnailStates.value = mediaThumbnailLoader?.snapshot() ?? new Map();
}

function mediaThumbnailState(item: MediaBrowserItem): MediaThumbnailDisplayState {
  if (!canLoadMediaThumbnail(item)) return { status: "unavailable", url: null };
  return mediaThumbnailStates.value.get(item.path) ?? { status: "idle", url: null };
}

function mediaThumbnailPlaceholder(item: MediaBrowserItem, state: MediaThumbnailDisplayState): string {
  if (state.status === "loading") return "…";
  if (state.status === "error") return "!";
  return item.media_kind === "video" ? "▶" : "♪";
}

function mediaThumbnailAriaLabel(item: MediaBrowserItem, state: MediaThumbnailDisplayState): string {
  if (state.status === "loading") return `${item.name}: ${t("mediaPreviewLoading")}`;
  if (state.status === "error" || state.status === "unavailable") {
    return `${item.name}: ${t("mediaPreviewUnavailable")}`;
  }
  return `${item.name}: ${mediaPreviewSourceLabel(item)}`;
}

function handleMediaThumbnailError(path: string): void {
  mediaThumbnailLoader?.markError(path);
}

async function loadMediaBrowser(
  directory: string | null = requestedMediaDirectory,
  search = mediaSearch.value,
  offset = 0,
  conflictRetry = false,
): Promise<void> {
  const api = apiClient;
  if (!api || !canControl.value || activePage.value !== "Library") return;
  const previousDirectory = requestedMediaDirectory;
  requestedMediaDirectory = directory;
  if (directory !== previousDirectory) {
    closeAllFiles();
    clearMediaSelection();
    closeMediaPreview();
    mediaQueueFeedback.value = null;
  }
  const generation = ++mediaLoadGeneration;
  mediaPageAbort?.abort();
  const abort = new AbortController(); mediaPageAbort = abort;
  mediaBrowserLoading.value = true;
  mediaBrowserError.value = null;
  try {
    const listing = await api.browseMedia(directory, search, mediaRows.value, { offset, signal: abort.signal, sort: mediaSort.value, version: offset > 0 ? mediaBrowser.value?.version : undefined });
    if (generation !== mediaLoadGeneration || activePage.value !== "Library") return;
    if (mediaBrowser.value?.version && listing.version !== mediaBrowser.value.version) clearMediaSelection();
    mediaOffset.value = offset;
    mediaBrowser.value = listing;
    retainMediaThumbnailListing(listing.items);
    if (listing.directory === null) {
      const rootItems = listing.items.filter((item) => item.is_directory);
      mediaRootPaths.value = rootsFromListing(listing.items);
      mediaRootLabels.value = Object.fromEntries(rootItems.map((item) => [item.path, item.name]));
    }

    if (
      mediaPreviewItem.value &&
      !listing.items.some((item) => item.path === mediaPreviewItem.value?.path)
    ) {
      closeMediaPreview();
    }
  } catch (error) {
    if (generation === mediaLoadGeneration) {
      if (error instanceof ApiError && error.status === 409 && !conflictRetry) {
        clearMediaSelection();
        await loadMediaBrowser(directory, search, 0, true);
        return;
      }
      mediaBrowserError.value =
        error instanceof ApiError ? localizedApiError(error) : "sectionLoadError";
    }
  } finally {
    if (generation === mediaLoadGeneration) mediaBrowserLoading.value = false;
  }
}

function searchMedia(): void {
  clearMediaSelection();
  void loadMediaBrowser(mediaBrowser.value?.directory ?? null, mediaSearch.value);
}

function clearMediaSelection(): void {
  selectedMediaPaths.value = new Set();
  selectedMediaEntries.value = new Map();
  allMediaSelection.value = null;
  mediaQueueFeedback.value = null;
}

function isMediaSelected(item: MediaBrowserItem): boolean {
  return (allMediaSelection.value ? !allMediaSelection.value.excluded.has(item.path) : selectedMediaPaths.value.has(item.path));
}
function toggleMediaSelection(item: MediaBrowserItem): void {
  if (mediaQueueBatchPending.value) return;
  if (allMediaSelection.value) {
    const excluded = new Set(allMediaSelection.value.excluded);
    if (excluded.has(item.path)) excluded.delete(item.path); else excluded.add(item.path);
    allMediaSelection.value = { ...allMediaSelection.value, excluded };
  } else {
    const selected = !selectedMediaPaths.value.has(item.path);
    selectedMediaPaths.value = toggleMediaPath(selectedMediaPaths.value,item.path,selected);
    const entries = new Map(selectedMediaEntries.value);
    if (selected) entries.set(item.path,item); else entries.delete(item.path);
    selectedMediaEntries.value = entries;
  }
  mediaQueueFeedback.value = null;
}
function toggleAllVisibleMedia(): void {
  if (mediaQueueBatchPending.value) return;
  const deselect = allVisibleMediaSelected.value;
  for (const item of visibleMediaFiles.value) if (isMediaSelected(item) === deselect) toggleMediaSelection(item);
}
function selectAllDirectory(total: number, files: number, version: string): void {
  if (mediaQueueBatchPending.value) return;
  clearMediaSelection();
  allMediaSelection.value = { total,files,version,excluded: new Set() };
}
async function fetchAllFilesWindow(offset: number, limit: number, version?: string, signal?: AbortSignal): Promise<MediaBrowserResponse> {
  if (!apiClient) throw new Error(t("connectSnapshot"));
  return apiClient.browseMedia(mediaBrowser.value?.directory ?? null,"",limit,{offset,version,signal,sort:mediaSort.value,details:false});
}
async function* selectedQueueEntries(explicit?: MediaBrowserItem[]) {
  if (explicit) { yield* explicit; return; }
  const selection = allMediaSelection.value;
  if (!selection) { yield* selectedMediaItems.value; return; }
  let offset: number | null = 0;
  while (offset !== null) {
    const listing = await fetchAllFilesWindow(offset,200,selection.version);
    for (const item of listing.items) if (!selection.excluded.has(item.path)) yield item;
    offset = listing.next_offset ?? null;
  }
}

function releaseMediaPreviewUrl(): void {
  if (mediaPreviewUrl.value) URL.revokeObjectURL(mediaPreviewUrl.value);
  mediaPreviewUrl.value = null;
}

function closeMediaPreview(): void {
  mediaPreviewGeneration += 1;
  releaseMediaPreviewUrl();
  mediaPreviewItem.value = null;
  mediaPreviewLoading.value = false;
  mediaPreviewError.value = null;
}

async function selectMediaItem(item: MediaBrowserItem): Promise<void> {
  if (item.is_directory) {
    mediaSearch.value = "";
    void loadMediaBrowser(item.path, "");
    return;
  }
  const generation = ++mediaPreviewGeneration;
  releaseMediaPreviewUrl();
  mediaPreviewItem.value = item;
  mediaPreviewError.value = null;
  mediaPreviewLoading.value = true;
  if (!apiClient) {
    mediaPreviewLoading.value = false;
    return;
  }
  const metadataPromise = apiClient.getMediaMetadata(item.path);
  const previewPromise = apiClient.getMediaPreview(item.path);
  try {
    const metadata = await metadataPromise;
    if (
      generation === mediaPreviewGeneration &&
      mediaPreviewItem.value?.path === item.path
    ) {
      mediaPreviewItem.value = {
        ...mediaPreviewItem.value,
        preview_kind: metadata.preview_kind,
        has_preview: metadata.has_preview,
        presentation: metadata.presentation,
      };
    }
  } catch {
    // The listing's bounded metadata remains usable when the detail refresh races.
  }
  try {
    const blob = await previewPromise;
    if (
      generation !== mediaPreviewGeneration ||
      mediaPreviewItem.value?.path !== item.path ||
      activePage.value !== "Library"
    ) {
      return;
    }
    mediaPreviewUrl.value = URL.createObjectURL(blob);
  } catch (error) {
    if (generation === mediaPreviewGeneration && mediaPreviewItem.value?.path === item.path) {
      mediaPreviewError.value =
        error instanceof ApiError ? localizedApiError(error) : "mediaPreviewUnavailable";
    }
  } finally {
    if (generation === mediaPreviewGeneration) mediaPreviewLoading.value = false;
  }
}

function openMediaBreadcrumb(path: string | null): void {
  mediaSearch.value = "";
  void loadMediaBrowser(path, "");
}

function openMediaDirectory(item: MediaBrowserItem): void {
  if (item.is_directory) openMediaBreadcrumb(item.path);
}

async function playMediaItem(item: MediaBrowserItem): Promise<void> {
  if (item.is_directory) {
    openMediaDirectory(item);
    return;
  }
  await issuePlayerCommand(async (commands) => {
    const loaded = await commands.load({ path: item.path });
    if (!loaded.accepted) throw new Error("The player did not accept the selected media.");
    return commands.play();
  });
}

function openMediaParent(): void {
  const parent = mediaBrowser.value?.parent_directory;
  openMediaBreadcrumb(parent !== null && parent !== undefined ? parent : null);
}

function formatMediaSize(size: number | null): string {
  if (size === null || !Number.isFinite(size)) return t("mediaMetadataUnavailable");
  if (size < 1024) return `${size} B`;
  const units = ["KiB", "MiB", "GiB"];
  let value = size;
  let unit = "B";
  for (const nextUnit of units) {
    value /= 1024;
    unit = nextUnit;
    if (value < 1024 || nextUnit === units.at(-1)) break;
  }
  const rounded = value >= 10 ? value.toFixed(0) : value.toFixed(1);
  return `${rounded} ${unit}`;
}

function formatMediaDate(value: string | null): string {
  if (!value) return t("mediaMetadataUnavailable");
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return t("mediaMetadataUnavailable");
  return new Intl.DateTimeFormat(locale.value, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function mediaKindLabel(item: MediaBrowserItem): string {
  if (item.is_directory) return t("mediaFolder");
  return item.media_kind === "audio" ? t("mediaAudio") : t("mediaVideo");
}

function mediaPresentationLabel(item: MediaBrowserItem): string {
  return presentationSummary(item.presentation, locale.value);
}

function mediaPreviewSourceLabel(item: MediaBrowserItem): string {
  const keyByKind: Record<MediaBrowserItem["preview_kind"], MessageKey> = {
    embedded_art: "mediaPreviewEmbeddedArt",
    sibling_art: "mediaPreviewSiblingArt",
    video_frame: "mediaPreviewVideoFrame",
    placeholder: "mediaPreviewPlaceholder",
    none: "mediaPreviewUnavailable",
  };
  return t(keyByKind[item.preview_kind] ?? "mediaPreviewUnavailable");
}

function isAmbiguousQueueAddError(error: unknown): boolean {
  // A transport exception or a 5xx response does not prove that the server
  // rejected the append. Never replay either kind of request automatically.
  return !(error instanceof ApiError) || error.status >= 500;
}

function acceptQueueRefresh(incoming: QueueState, api: typeof apiClient): QueueState {
  if (api !== apiClient || !canControl.value) throw new Error("control scope changed");
  applyQueueResponse(queue, incoming);
  return queue.value!;
}

async function addMediaItemsToQueue(items?: MediaBrowserItem[]): Promise<void> {
  const api = apiClient;
  if (mediaQueueBatchPending.value || !api || !canControl.value || (items ? !items.length : !selectedMediaCount.value)) return;
  mediaQueueBatchPending.value = true;
  mediaQueueFeedback.value = null;
  const outcome = emptyMediaQueueBatchOutcome();
  const addedPaths: string[] = [];
  try {
    let currentQueue: QueueState;
    try {
      currentQueue = await api.getQueue();
      currentQueue = acceptQueueRefresh(currentQueue, api);
    } catch (error) {
      const message = error instanceof ApiError ? localizedApiError(error) : "mediaQueueRequestFailed";
      outcome.failed = (items ?? selectedMediaItems.value).map((item) => ({ name: item.name, message }));
      outcome.stopped = true;
      outcome.stoppedReason = "refresh";
      return;
    }
    for await (const item of selectedQueueEntries(items)) {
      if (api !== apiClient || !canControl.value) { outcome.stopped = true; outcome.stoppedReason = "refresh"; break; }
      if (item.is_directory) { outcome.failed.push({name:item.name,message:"mediaFolderQueueUnavailable"}); continue; }
      const queueBeforeAttempt = currentQueue;
      try {
        await api.addQueueItem(item.path, currentQueue.items.length, currentQueue.revision);
        outcome.added.push(item.name);
        addedPaths.push(item.path);
      } catch (error) {
        const message =
          error instanceof ApiError ? localizedApiError(error) : "mediaQueueRequestFailed";
        if (isAmbiguousQueueAddError(error)) {
          try {
            const observedQueue = await api.getQueue();
            acceptQueueRefresh(observedQueue, api);
            if (queueAddWasObserved(queueBeforeAttempt, observedQueue, item.path)) {
              outcome.reconciled.push(item.name);
              addedPaths.push(item.path);
            } else {
              recordMediaQueueUncertainty(outcome, item.name, "mediaQueueUncertainItem");
            }
          } catch {
            recordMediaQueueUncertainty(outcome, item.name, "mediaQueueUncertainItem");
          }
          outcome.stopped = true;
          outcome.stoppedReason = "uncertain";
          break;
        }
        if (recordMediaQueueFailure(outcome, item.name, message, error instanceof ApiError ? error.code : undefined)) {
          try {
            currentQueue = await api.getQueue();
            currentQueue = acceptQueueRefresh(currentQueue, api);
          } catch {
            outcome.stoppedReason = "refresh";
          }
          break;
        }
        continue;
      }
      try {
        currentQueue = await api.getQueue();
        currentQueue = acceptQueueRefresh(currentQueue, api);
      } catch {
        // The add was accepted, but without a fresh revision it is unsafe to
        // continue issuing optimistic batch mutations.
        outcome.stopped = true;
        outcome.stoppedReason = "refresh";
        break;
      }
    }
  } catch (error) {
    outcome.stopped = true; outcome.stoppedReason = "refresh";
    outcome.failed.push({name: "", message: error instanceof ApiError ? localizedApiError(error) : "mediaQueueRequestFailed"});
  } finally {
    if (allMediaSelection.value) allMediaSelection.value = {...allMediaSelection.value, excluded: new Set([...allMediaSelection.value.excluded,...addedPaths])};
    selectedMediaPaths.value = new Set(
      [...selectedMediaPaths.value].filter((path) => !addedPaths.includes(path)),
    );
    mediaQueueFeedback.value = outcome;
    mediaQueueBatchPending.value = false;
  }
}

async function loadRemoteQueue(): Promise<void> {
  const api = apiClient;
  if (!api || !canControl.value) return;
  const generation = ++pageLoadGeneration;
  const controlGeneration = commandScope.generation;
  const trackGeneration = ++infoTrackRefreshGeneration;
  secondaryLoading.value = true;
  secondaryError.value = null;
  try {
    const [nextQueue, nextTracks, nextChapters] = await Promise.all([api.getQueue(), api.getTracks(), api.getChapters()]);
    if (generation !== pageLoadGeneration || activePage.value !== "Remote" || controlGeneration !== commandScope.generation || !queueResponseIsCurrent(queue.value, nextQueue)) return;
    queue.value = nextQueue;
    if (trackGeneration === infoTrackRefreshGeneration) {
      tracks.value = nextTracks;
      chapters.value = nextChapters;
    }
  } catch (error) {
    if (generation === pageLoadGeneration) {
      queue.value = null;
      secondaryError.value =
        error instanceof ApiError ? localizedApiError(error) : "sectionLoadError";
    }
  } finally {
    if (generation === pageLoadGeneration) secondaryLoading.value = false;
  }
}

async function selectTrackById(
  trackId: number,
  type: "audio" | "sub" | "video",
): Promise<void> {
  const result = await issuePlayerCommand((commands) => commands.selectTrack({ trackId, type }));
  if (!result || !apiClient) return;

  const api = apiClient;
  const generation = ++pageLoadGeneration;
  const trackGeneration = ++infoTrackRefreshGeneration;
  const deadline = Date.now() + 3000;
  secondaryLoading.value = true;
  secondaryError.value = null;
  try {
    while (Date.now() < deadline) {
      const nextTracks = await api.getTracks();
      if (generation !== pageLoadGeneration || activePage.value !== "Remote") return;
      if (trackGeneration !== infoTrackRefreshGeneration || api !== apiClient) return;
      tracks.value = nextTracks;
      if (
        nextTracks.some(
          (track) => track.id === trackId && track.type === type && track.selected,
        )
      ) {
        return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 100));
    }
    secondaryError.value = "trackSelectionUnconfirmed";
  } catch (error) {
    if (generation === pageLoadGeneration) {
      secondaryError.value =
        error instanceof ApiError ? localizedApiError(error) : "sectionLoadError";
    }
  } finally {
    if (generation === pageLoadGeneration) secondaryLoading.value = false;
  }
}

async function selectTrack(track: TrackViewModel): Promise<void> {
  if (!track.selectableType) return;
  await selectTrackById(track.id, track.selectableType);
}

function seekToChapter(chapter: ChapterViewModel): void {
  const duration = playbackView.value?.controls.durationSeconds ?? null;
  void issuePlayerCommand((commands) => seekTo(commands, chapter.timeSeconds, duration));
}

async function moveQueueItem(item: QueueItemViewModel, offset: -1 | 1): Promise<void> {
  const currentQueue = queue.value;
  if (!currentQueue) return;
  const ordinal = item.ordinal + offset;
  if (ordinal < 0 || ordinal >= currentQueue.items.length) return;
  const updated = await issueCommand((api) =>
    api.moveQueueItem(item.id, ordinal, currentQueue.revision),
  );
  if (updated) applyQueueResponse(queue, updated);
}

async function removeQueueItem(item: QueueItemViewModel): Promise<void> {
  const currentQueue = queue.value;
  if (!currentQueue) return;
  const updated = await issueCommand((api) =>
    api.removeQueueItem(item.id, currentQueue.revision),
  );
  if (updated) applyQueueResponse(queue, updated);
}

async function activateQueueItem(item: QueueItemViewModel): Promise<void> {
  const currentQueue = queue.value;
  if (!currentQueue) return;
  const updated = await issueCommand((api) =>
    playQueueItem(api, item.id, currentQueue.revision),
  );
  if (updated) applyQueueResponse(queue, updated);
}

async function clearQueue(): Promise<void> {
  const currentQueue = queue.value;
  if (!currentQueue) return;
  const updated = await issueCommand((api) => api.clearQueue(currentQueue.revision));
  if (updated) applyQueueResponse(queue, updated);
}

async function navigateQueue(direction: QueueDirection): Promise<void> {
  const updated = await issueCommand(api => navigateQueueOnce(api, direction));
  if (updated) applyQueueResponse(queue, updated);
}

function selectAudio(event: Event): void {
  if (!canControl.value || commandPending.value) return;
  audioSelectionRevision += 1;
  audioApplyResult.value = null;
  const id = (event.target as HTMLSelectElement).value;
  selectedProfileIds.value = selectedProfileIds.value.filter(key => profiles.value.find(profile => profile.id === key)?.category !== "audio");
  if (id) selectedProfileIds.value.push(id);
  commandError.value = null;
  audioApplyFailed.value = false;
}

function audioOptionLabel(profile: Profile): string {
  const label = localizedProfileName(profile);
  const duplicate = audioProfiles.value.filter(item => localizedProfileName(item) === label).length > 1;
  return `${label}${duplicate && profile.route ? ` · ${profile.route}` : ""}${profile.enabled ? "" : ` · ${t("audioUnavailableShort")}`}`;
}

function toggleProfile(profile: { id: string; category: string }, event: Event) {
  const checked = (event.target as HTMLInputElement).checked;
  selectedProfileIds.value = selectedProfileIds.value.filter(id =>
    id !== profile.id && (!checked || profiles.value.find(item => item.id === id)?.category !== profile.category));
  if (checked) selectedProfileIds.value.push(profile.id);
}

async function applySelectedProfiles(category: "audio" | "video" | "other"): Promise<void> {
  if (!canControl.value || commandPending.value) return;
  const selected = [...new Set(selectedProfileIds.value.filter(id =>
    category === "other" ? !["audio","display"].includes(profiles.value.find(profile => profile.id === id)?.category ?? "") : profiles.value.find(profile => profile.id === id)?.category === (category === "audio" ? "audio" : "display")))];
  if (!selected.length || selected.length > 5 || selected.some(id => !profiles.value.find(profile => profile.id === id)?.enabled)) return;
  const connection = apiClient;
  const generation = commandScope.generation;
  const selectionRevision = category === "video" ? videoSelectionRevision : audioSelectionRevision;
  const operation = ++audioApplyOperation;
  if (category === "audio") {
    audioApplyFailed.value = false;
    audioApplyPending.value = true;
    audioRequestRouteRevision = audioRouteRevision.value;
  }
  if (category === "video") { videoApplyPending.value = true; videoApplyFailed.value = false; }
  const result = await issueCommand((api) => api.applyProfiles(selected));
  if (connection !== apiClient || generation !== commandScope.generation || operation !== audioApplyOperation) return;
  if (category === "audio") {
    if (selectionRevision !== audioSelectionRevision || selectedAudioId.value !== selected[0]) return;
    audioApplyPending.value = false;
    audioApplyFailed.value = !result;
    if (result) audioApplyResult.value = result;
  } else if (category === "video") {
    videoApplyPending.value = false;
    if (selectionRevision !== videoSelectionRevision || selectedVideoId.value !== selected[0]) return;
    videoApplyFailed.value = !result;
    if (result) videoApplyResult.value = result;
  } else if (result) profileApplyResult.value = result;
}

onBeforeUnmount(() => { window.removeEventListener("resize", resizeMediaPage); coarsePointer.removeEventListener("change", resizeMediaPage); window.removeEventListener("popstate", modalHistoryChange); disconnect(); });
</script>

<template>
  <main
    class="app-shell"
    :class="{ 'app-shell-with-transport': playbackView }"
  >
    <header class="top-bar">
      <div>
        <p class="eyebrow">{{ t("localPlayer") }}</p>
        <h1>mpv <span>remote</span></h1>
      </div>
      <div class="top-bar-actions">
        <label class="visually-hidden" for="ui-language">{{ t("language") }}</label>
        <select id="ui-language" v-model="locale" class="language-select" :aria-label="t('language')">
          <option value="en">{{ t("english") }}</option>
          <option value="zh-CN">{{ t("simplifiedChinese") }}</option>
        </select>
        <div class="header-connection-row">
        <div
          class="connection-indicator"
          :class="[`connection-${state.connection}`, { 'connection-degraded': playbackView?.player.status === 'error' }]"
          aria-live="polite"
        >
          <span class="connection-dot" aria-hidden="true"></span>
          {{ localizedConnectionLabel }}
        </div>
        <button v-if="state.connection === 'open'" class="header-disconnect" type="button" @click="disconnect">{{ t("disconnect") }}</button>
        </div>
      </div>
    </header>

    <section
      v-if="state.connection !== 'open'"
      class="connection-panel"
      aria-labelledby="connection-title"
    >
      <div class="panel-heading">
        <div>
          <p class="eyebrow">{{ t("secureSession") }}</p>
          <h2 id="connection-title">{{ t("connectPlayer") }}</h2>
        </div>
      </div>
      <form class="connection-form" @submit.prevent="connect">
        <label for="access-token">{{ t("provisionedToken") }}</label>
        <div class="token-row">
          <input
            id="access-token"
            v-model="accessToken"
            type="password"
            name="access-token"
            autocomplete="off"
            autocapitalize="off"
            spellcheck="false"
            :placeholder="t('tokenPlaceholder')"
            :disabled="state.connection === 'connecting'"
          />
          <button
            class="button button-primary"
            type="submit"
            :disabled="state.connection === 'connecting'"
          >
            {{ t("connect") }}
          </button>
        </div>
        <p class="helper-text">{{ t("tokenMemoryNotice") }}</p>
        <p v-if="state.error" class="error-text" role="status">{{ localizedConnectionError }}</p>
      </form>
    </section>

    <nav class="primary-nav" :aria-label="t('primaryNavigation')">
      <button
        v-for="page in pages"
        :key="page"
        type="button"
        :aria-current="activePage === page ? 'page' : undefined"
        :class="{ active: activePage === page }"
        @click="activePage = page"
      >
        {{ pageLabel(page) }}
      </button>
    </nav>

    <section
      class="content-panel"
      :class="{ 'content-panel-with-transport': playbackView }"
      :aria-labelledby="`page-${activePage.toLowerCase()}`"
    >
      <template v-if="activePage === 'Remote'">
        <RemoteView
          v-if="playbackView"
          :locale="locale"
          :playback="playbackView"
          :output-status="remoteOutputStatus"
          :player-title="playerTitle"
          :status-label="localizedPlayerStatus(playbackView.player.status)"
        >
          <template #details>
            <p v-if="secondaryLoading" class="helper-text" role="status">{{ t("loadingQueue") }}</p>
            <p v-if="secondaryError" class="error-text" role="alert">{{ t(secondaryError) }}</p>

          </template>
        </RemoteView>
        <div v-else class="empty-state">
          <span class="empty-icon" aria-hidden="true">▶</span>
          <p>{{ t("connectSnapshot") }}</p>
        </div>
        <section class="secondary-section" aria-labelledby="tracks-heading">
          <h3 id="tracks-heading">{{ t("tracks") }}</h3>
          <div class="track-selectors">
            <section class="track-selector-section" aria-labelledby="audio-selector-heading">
              <h4 id="audio-selector-heading">{{ localizedTrackType("audio") }}</h4>
              <AudioSelector
              :locale="locale"
                v-if="audioTrackViews.length"
                :tracks="audioTrackViews"
                :disabled="!canControl || secondaryLoading || commandPending"
                :label="t('audioTrackSelection')"
                :choose-label="t('chooseAudioTrack')"
                :untitled-label="t('untitledTrack')"
                :codec-label="t('codecLabel')"
                :selected-label="t('selected')"
                @select="selectTrackById($event, 'audio')"
              />
              <p v-else-if="!secondaryLoading && canControl" class="helper-text">{{ t("noAudioTracks") }}</p>
            </section>
            <section class="track-selector-section" aria-labelledby="subtitle-selector-heading">
              <h4 id="subtitle-selector-heading">{{ localizedTrackType("sub") }}</h4>
              <SubtitleSelector
              :locale="locale"
                v-if="subtitleTrackViews.length"
                :tracks="subtitleTrackViews"
                :disabled="!canControl || secondaryLoading || commandPending"
                :label="t('subtitleTrackSelection')"
                :choose-label="t('chooseSubtitleTrack')"
                :untitled-label="t('untitledTrack')"
                :codec-label="t('codecLabel')"
                :selected-label="t('selected')"
                @select="selectTrackById($event, 'sub')"
              />
              <p v-else-if="!secondaryLoading && canControl" class="helper-text">{{ t("noSubtitleTracks") }}</p>
            </section>
          </div>
          <section v-if="otherTrackViews.length" class="track-selector-section other-tracks" aria-labelledby="other-tracks-heading">
            <h4 id="other-tracks-heading">{{ t("otherTracks") }}</h4>
            <ul class="track-list">
            <li v-for="track in otherTrackViews" :key="`${track.type}-${track.id}`" class="track-row">
              <div class="track-description">
                <span class="track-kind">{{ localizedTrackType(track.type) }} · #{{ track.id }}</span>
                <span class="track-title">{{ track.title || t("untitledTrack") }}</span>
                <span v-if="track.language" class="track-language">{{ track.language }}</span>
                <span class="track-language">{{ t("codecLabel") }}: {{ trackCodecLabel(track.codec, locale) }}</span>
              </div>
              <button
                class="button button-secondary row-action"
                type="button"
                :disabled="!canControl || secondaryLoading || commandPending || track.selected || !track.selectableType"
                :aria-label="track.selected ? t('trackSelected', { id: track.id }) : t('selectTrack', { type: localizedTrackType(track.type), id: track.id })"
                @click="selectTrack(track)"
              >
                {{ track.selected ? t("selected") : t("select") }}
              </button>
            </li>
            </ul>
          </section>
        </section>

        <section class="secondary-section" aria-labelledby="chapters-heading">
          <h3 id="chapters-heading">{{ t("chapters") }}</h3>
          <ul v-if="chapterViews.length" class="chapter-list">
            <li v-for="chapter in chapterViews" :key="chapter.id">
              <button
                class="chapter-button"
                type="button"
                :disabled="!canControl || secondaryLoading || commandPending"
                @click="seekToChapter(chapter)"
              >
                <span>{{ chapter.title || t("chapterFallback", { number: chapter.id + 1 }) }}</span>
                <span class="chapter-time">{{ formatPlaybackTime(chapter.timeSeconds) }}</span>
              </button>
            </li>
          </ul>
          <p v-else-if="!secondaryLoading && canControl" class="helper-text">{{ t("noChapters") }}</p>
        </section>
        <p v-if="commandError" class="error-text" role="alert">{{ t(commandError) }}</p>
      </template>
      <template v-else-if="activePage === 'Library'">
        <div class="panel-heading">
          <div>
            <p class="eyebrow">{{ t("mediaDetails") }}</p>
            <h2 id="page-library">{{ t("mediaLibraryTitle") }}</h2>
          </div>
          <button
            class="button button-secondary section-refresh"
            type="button"
            :disabled="!canControl || secondaryLoading || commandPending"
            @click="loadPageData('Library')"
          >
            {{ t("refresh") }}
          </button>
        </div>
        <p v-if="!canControl" class="helper-text">{{ t("connectMedia") }}</p>
        <p v-if="secondaryLoading" class="helper-text" role="status">{{ t("loadingMedia") }}</p>
        <p v-if="secondaryError" class="error-text" role="alert">{{ t(secondaryError) }}</p>

        <section class="secondary-section media-browser" aria-labelledby="media-browser-heading">
          <div class="media-browser-title-row">
            <div>
              <h3 id="media-browser-heading">{{ t("mediaBrowser") }}</h3>
              <p class="helper-text media-directory-label" aria-live="polite">{{ mediaDirectoryLabel }}</p>
            </div>
            <button
              v-if="mediaBrowser"
              class="button button-secondary section-refresh"
              type="button"
              :disabled="!canControl || mediaBrowserLoading || mediaQueueBatchPending || commandPending"
              :aria-label="t('mediaBack')"
              @click="openMediaParent"
            >
              {{ t("mediaBack") }}
            </button>
          </div>
          <nav class="media-breadcrumbs" :aria-label="t('mediaNavigation')">
            <span v-for="(crumb, index) in mediaBreadcrumbs" :key="crumb.path ?? 'roots'" class="media-breadcrumb">
              <span v-if="index" class="media-breadcrumb-separator" aria-hidden="true">/</span>
              <button
                class="media-breadcrumb-button"
                type="button"
                :disabled="!canControl || mediaBrowserLoading || mediaQueueBatchPending"
                :aria-current="index === mediaBreadcrumbs.length - 1 ? 'page' : undefined"
                @click="openMediaBreadcrumb(crumb.path)"
              >
                {{ crumb.label }}
              </button>
            </span>
          </nav>
          <form class="media-search-form" role="search" @submit.prevent="searchMedia">
            <label class="visually-hidden" for="media-search">{{ t("searchMedia") }}</label>
            <input
              id="media-search"
              v-model="mediaSearch"
              type="search"
              maxlength="128"
              :placeholder="t('searchMediaPlaceholder')"
              :disabled="!canControl || mediaBrowserLoading || mediaQueueBatchPending || commandPending"
            />
            <button
              class="button button-secondary"
              type="submit"
              :disabled="!canControl || mediaBrowserLoading || mediaQueueBatchPending || commandPending"
            >
              {{ t("search") }}
            </button>
<label class="media-sort-label"><span class="visually-hidden">{{ t("mediaSort") }}</span><select v-model="mediaSort" :aria-label="t('mediaSort')" :disabled="!canControl || mediaBrowserLoading || mediaQueueBatchPending" @change="loadMediaBrowser()"><option value="name">{{ t("mediaNameAscending") }}</option><option value="name_desc">{{ t("mediaNameDescending") }}</option><option value="modified">{{ t("mediaNewest") }}</option></select></label>
          </form>
          <BrowserPager :locale="locale" :offset="mediaOffset" :page-size="mediaRows" :total="mediaBrowser?.total ?? 0" :next-offset="mediaBrowser?.next_offset ?? null" :busy="!canControl || !mediaBrowser || mediaBrowserLoading || mediaQueueBatchPending" @page="loadMediaBrowser(mediaBrowser?.directory ?? null, mediaSearch, $event)" @all="openAllFiles" />
          <div v-if="visibleMediaFiles.length || selectedMediaCount" class="media-browser-toolbar">
            <div class="media-selection-summary" role="status" aria-live="polite">
              <span>{{ t("mediaSelectionCount", { count: selectedMediaCount }) }}</span>
              <button
                class="button button-secondary media-compact-action"
                type="button"
                :disabled="!canControl || mediaBrowserLoading || mediaQueueBatchPending"
                @click="toggleAllVisibleMedia"
              >
                {{ allVisibleMediaSelected ? t("clearMediaSelection") : t("selectAllMedia") }}
              </button>
              <button
                v-if="selectedMediaCount"
                class="button button-secondary media-compact-action"
                type="button"
                :disabled="!canControl || mediaBrowserLoading || mediaQueueBatchPending"
                @click="clearMediaSelection"
              >
                {{ t("clearMediaSelection") }}
              </button>
            </div>
            <button
              class="button media-queue-action"
              type="button"
              :disabled="!canControl || !selectedMediaCount || mediaQueueBatchPending || commandPending"
              @click="addMediaItemsToQueue()"
            >
              {{ mediaQueueBatchPending ? t("addingToQueue") : t("addSelectedToQueue") }}
            </button>
          </div>
          <p v-if="mediaBrowserLoading" class="helper-text" role="status">{{ t("loadingMediaLibrary") }}</p>
          <p v-if="mediaBrowserError" class="error-text" role="alert">{{ t(mediaBrowserError) }}</p>
          <ul v-if="mediaBrowser?.items.length" class="media-browser-list">
            <li
              v-for="item in mediaBrowser.items"
              :key="item.path"
              class="media-browser-row"
              :data-media-path="item.path"
              :class="{ 'media-browser-row-selected': !item.is_directory && isMediaSelected(item) }"
            >
              <label v-if="!item.is_directory" class="media-select-control">
                <input
                  type="checkbox"
                  :checked="isMediaSelected(item)"
                  :disabled="!canControl || mediaBrowserLoading || mediaQueueBatchPending || commandPending"
                  :aria-label="t('selectMediaItem', { name: item.name })"
                  @click.stop
                  @change="toggleMediaSelection(item)"
                />
              </label>
              <span v-else class="media-select-placeholder" aria-hidden="true"></span>
              <div
                v-if="item.is_directory"
                class="media-thumbnail media-thumbnail-folder"
                aria-hidden="true"
              >
                ▸
              </div>
              <div
                v-else
                class="media-thumbnail"
                :class="`media-thumbnail-${mediaThumbnailState(item).status}`"
                :data-media-thumbnail-path="item.path"
                :data-preview-kind="item.preview_kind"
                :data-preview-status="mediaThumbnailState(item).status"
                role="img"
                :aria-label="mediaThumbnailAriaLabel(item, mediaThumbnailState(item))"
                :ref="canLoadMediaThumbnail(item) ? (element) => setMediaThumbnailTarget(item.path, element) : undefined"
              >
                <img
                  v-if="mediaThumbnailState(item).url"
                  :src="mediaThumbnailState(item).url ?? undefined"
                  alt=""
                  @error="handleMediaThumbnailError(item.path)"
                />
                <span v-else aria-hidden="true">
                  {{ mediaThumbnailPlaceholder(item, mediaThumbnailState(item)) }}
                </span>
              </div>
              <button
                class="media-item-main"
                type="button"
                :disabled="!canControl || mediaBrowserLoading || mediaQueueBatchPending || commandPending"
                :aria-label="item.is_directory ? t('viewMediaFolder', { name: item.name }) : t('viewMediaDetails', { name: item.name })"
                @click="selectMediaItem(item)"
              >
                <span class="media-item-copy">
                  <span class="media-browser-name" :title="item.name">{{ item.name }}</span>
                  <span class="media-item-meta">
                    <span>{{ mediaKindLabel(item) }}</span>
                    <span v-if="!item.is_directory">{{ formatMediaSize(item.size_bytes) }}</span>
                    <span v-if="!item.is_directory">{{ formatMediaDate(item.modified_at) }}</span>
                    <span v-if="!item.is_directory">{{ mediaPresentationLabel(item) }}</span>
                  </span>
                </span>
              </button>
              <div class="media-browser-actions">
                <button
                  v-if="item.is_directory"
                  class="button button-secondary media-browser-action"
                  type="button"
                  :disabled="!canControl || mediaBrowserLoading || mediaQueueBatchPending || commandPending"
                  :aria-label="t('openMediaFolder', { name: item.name })"
                  @click="openMediaDirectory(item)"
                >
                  {{ t("openFolder") }}
                </button>
                <template v-else>
                  <button
                    class="button button-secondary media-browser-action"
                    type="button"
                    :disabled="!canControl || mediaBrowserLoading || mediaQueueBatchPending || commandPending"
                    :aria-label="t('addMediaToQueue', { name: item.name })"
                    @click="addMediaItemsToQueue([item])"
                  >
                    {{ t("addToQueue") }}
                  </button>
                  <button
                    class="button button-secondary media-browser-action"
                    type="button"
                    :disabled="!canControl || mediaBrowserLoading || mediaQueueBatchPending || commandPending"
                    :aria-label="t('playMediaItem', { name: item.name })"
                    @click="playMediaItem(item)"
                  >
                    {{ t("playNow") }}
                  </button>
                </template>
              </div>
            </li>
          </ul>
          <p
            v-else-if="mediaBrowser && !mediaBrowserLoading && !mediaBrowserError"
            class="helper-text"
          >
            {{ t("mediaBrowserEmpty") }}
          </p>

          <BrowserPager :locale="locale" :offset="mediaOffset" :page-size="mediaRows" :total="mediaBrowser?.total ?? 0" :next-offset="mediaBrowser?.next_offset ?? null" :busy="!canControl || !mediaBrowser || mediaBrowserLoading || mediaQueueBatchPending" @page="loadMediaBrowser(mediaBrowser?.directory ?? null, mediaSearch, $event)" @all="openAllFiles" />
          <aside v-if="mediaPreviewItem" class="media-preview-panel" aria-labelledby="media-preview-heading">
            <div class="media-preview-art" aria-hidden="true">
              <img
                v-if="mediaPreviewUrl"
                :src="mediaPreviewUrl"
                :alt="t('mediaPreviewAlt', { name: mediaPreviewItem.name })"
              />
              <span v-else class="media-preview-placeholder">{{ mediaPreviewItem.is_directory ? "▸" : "♪" }}</span>
            </div>
            <div class="media-preview-copy">
              <div class="media-preview-heading-row">
                <div>
                  <p class="eyebrow">{{ t("mediaPreview") }}</p>
                  <h4 id="media-preview-heading">{{ mediaPreviewItem.name }}</h4>
                </div>
                <button
                  class="button button-secondary media-compact-action"
                  type="button"
                  :aria-label="t('closeMediaPreview')"
                  @click="closeMediaPreview"
                >
                  ×
                </button>
              </div>
              <p v-if="mediaPreviewLoading" class="helper-text" role="status">{{ t("mediaPreviewLoading") }}</p>
              <p v-if="mediaPreviewError" class="error-text" role="alert">{{ t(mediaPreviewError) }}</p>
              <dl class="media-metadata-list">
                <div><dt>{{ t("mediaType") }}</dt><dd>{{ mediaKindLabel(mediaPreviewItem) }}</dd></div>
                <div><dt>{{ t("mediaPresentation") }}</dt><dd>{{ mediaPresentationLabel(mediaPreviewItem) }}</dd></div>
                <div><dt>{{ t("mediaPreviewSource") }}</dt><dd>{{ mediaPreviewSourceLabel(mediaPreviewItem) }}</dd></div>
                <div><dt>{{ t("mediaFileSize") }}</dt><dd>{{ formatMediaSize(mediaPreviewItem.size_bytes) }}</dd></div>
                <div><dt>{{ t("mediaModified") }}</dt><dd>{{ formatMediaDate(mediaPreviewItem.modified_at) }}</dd></div>
                <div><dt>{{ t("mediaPath") }}</dt><dd><code>{{ mediaPreviewItem.path }}</code></dd></div>
              </dl>
              <div class="media-preview-actions">
                <button
                  class="button button-secondary"
                  type="button"
                  :disabled="!canControl || mediaQueueBatchPending || commandPending"
                  @click="addMediaItemsToQueue([mediaPreviewItem])"
                >
                  {{ t("addToQueue") }}
                </button>
                <button
                  class="button button-secondary"
                  type="button"
                  :disabled="!canControl || mediaQueueBatchPending || commandPending"
                  @click="playMediaItem(mediaPreviewItem)"
                >
                  {{ t("playNow") }}
                </button>
              </div>
            </div>
          </aside>
          <div v-if="mediaQueueFeedback" class="media-queue-feedback" role="status" aria-live="polite">
            <p v-if="mediaQueueFeedback.added.length">
              {{ t("mediaQueueAdded", { count: mediaQueueFeedback.added.length, names: mediaQueueFeedback.added.join(", ") }) }}
            </p>
            <p v-if="mediaQueueFeedback.reconciled.length">
              {{ t("mediaQueueReconciled", { count: mediaQueueFeedback.reconciled.length, names: mediaQueueFeedback.reconciled.join(", ") }) }}
            </p>
            <p v-if="mediaQueueFeedback.failed.length">{{ t("mediaQueueFailed", { count: mediaQueueFeedback.failed.length }) }}</p>
            <ul v-if="mediaQueueFeedback.failed.length" class="media-queue-failures">
              <li v-for="failure in mediaQueueFeedback.failed" :key="failure.name">{{ failure.name || t("mediaLibraryTitle") }} — {{ queueErrorMessage(failure.message, locale) }}</li>
            </ul>
            <p v-if="mediaQueueFeedback.uncertain.length">{{ t("mediaQueueUncertain", { count: mediaQueueFeedback.uncertain.length }) }}</p>
            <ul v-if="mediaQueueFeedback.uncertain.length" class="media-queue-failures">
              <li v-for="failure in mediaQueueFeedback.uncertain" :key="failure.name">{{ failure.name || t("mediaLibraryTitle") }} — {{ queueErrorMessage(failure.message, locale) }}</li>
            </ul>
            <p v-if="mediaQueueFeedback.stopped && mediaQueueFeedback.stoppedReason === 'conflict'">{{ t("mediaQueueStopped") }}</p>
            <p v-else-if="mediaQueueFeedback.stopped && mediaQueueFeedback.stoppedReason === 'uncertain'">{{ t("mediaQueueStoppedUncertain") }}</p>
            <p v-else-if="mediaQueueFeedback.stopped && mediaQueueFeedback.stoppedReason === 'refresh'">{{ t("mediaQueueStoppedRefresh") }}</p>
          </div>
        </section>

      </template>

      <template v-else-if="activePage === 'Queue'">
        <div class="panel-heading">
          <div>
            <p class="eyebrow">{{ t("playNext") }}</p>
            <h2 id="page-queue">{{ pageLabel("Queue") }}</h2>
          </div>
          <button
            class="button button-secondary section-refresh"
            type="button"
            :disabled="!canControl || secondaryLoading || commandPending"
            @click="loadPageData('Queue')"
          >
            {{ t("refresh") }}
          </button>
        </div>
        <p v-if="!canControl" class="helper-text">{{ t("connectQueue") }}</p>
        <p v-if="secondaryLoading" class="helper-text" role="status">{{ t("loadingQueue") }}</p>
        <p v-if="secondaryError" class="error-text" role="alert">{{ t(secondaryError) }}</p>

        <div class="queue-summary">
          <span v-if="queueView">{{ t("queueSummary", { count: queueView.items.length, revision: queueView.revision }) }}</span>
          <button
            class="text-button danger-action"
            type="button"
              :disabled="!canControl || secondaryLoading || commandPending || !queueView?.items.length"
            @click="clearQueue"
          >
            {{ t("clearQueue") }}
          </button>
        </div>
        <ul v-if="orderedQueueItems.length" class="queue-list">
          <li v-for="(item, index) in orderedQueueItems" :key="item.id" class="queue-row">
            <div class="queue-item-copy">
              <span class="queue-item-title">{{ index + 1 }}. {{ mediaTitle(item.mediaPath, locale) }}</span>
              <span class="queue-item-path">{{ item.mediaPath }}</span>
              <span v-if="item.active" class="queue-active">{{ t("nowPlayingItem") }}</span>
              <span v-else-if="queue?.playback?.item_id === item.id && ['prepared','submitted'].includes(queue.playback.phase)">{{ t("queuePlaybackPending") }}</span>
              <span v-else-if="queue?.playback?.item_id === item.id && queue.playback.phase === 'failed'">{{ t("queuePlaybackFailed") }}</span>
              <span v-else-if="queue?.playback?.item_id === item.id && queue.playback.phase === 'uncertain'">{{ t("queuePlaybackUncertain") }}</span>
            </div>
            <div class="queue-actions">
              <button
                class="icon-button"
                type="button"
                :aria-label="t('moveQueueItemUp', { number: index + 1 })"
                :disabled="!canControl || secondaryLoading || commandPending || index === 0"
                @click="moveQueueItem(item, -1)"
              >
                ↑
              </button>
              <button
                class="icon-button"
                type="button"
                :aria-label="t('moveQueueItemDown', { number: index + 1 })"
                :disabled="!canControl || secondaryLoading || commandPending || index === orderedQueueItems.length - 1"
                @click="moveQueueItem(item, 1)"
              >
                ↓
              </button>
              <button
                class="text-button"
                type="button"
                :disabled="!canControl || secondaryLoading || commandPending"
                :aria-label="t('playQueueItem', { number: index + 1, title: mediaTitle(item.mediaPath, locale) })"
                @click="activateQueueItem(item)"
              >
                {{ t("play") }}
              </button>
              <button
                class="text-button danger-action"
                type="button"
                :disabled="!canControl || secondaryLoading || commandPending"
                :aria-label="t('removeQueueItem', { number: index + 1, title: mediaTitle(item.mediaPath, locale) })"
                @click="removeQueueItem(item)"
              >
                {{ t("remove") }}
              </button>
            </div>
          </li>
        </ul>
        <p v-else-if="!secondaryLoading && canControl" class="empty-queue">{{ t("queueEmpty") }}</p>
        <p v-if="commandError" class="error-text" role="alert">{{ t(commandError) }}</p>
      </template>

      <template v-else>
        <div class="panel-heading">
          <div>
            <p class="eyebrow">{{ t("playerSettings") }}</p>
            <h2 id="page-info">{{ t("info") }}</h2>
          </div>
          <button
            class="button button-secondary section-refresh"
            type="button"
            :disabled="!canControl || secondaryLoading || commandPending"
            @click="loadPageData('Info')"
          >
            {{ t("refresh") }}
          </button>
        </div>
        <p v-if="!canControl" class="helper-text">{{ t("connectProfiles") }}</p>
        <p v-if="secondaryLoading" class="helper-text" role="status">{{ t("loadingProfiles") }}</p>
        <p v-if="secondaryError" class="error-text" role="alert">{{ t(secondaryError) }}</p>

        <section class="secondary-section audio-selection" aria-labelledby="outputs-heading">
          <h3 id="outputs-heading">{{ t("audioOutput") }}</h3>
          <p class="applied-audio" data-testid="applied-audio"><strong>{{ t("audioAppliedRoute") }}</strong> {{ appliedAudioLabel }}</p>
          <label for="audio-device">{{ t("audioDeviceSelection") }}</label>
          <div class="audio-select-row">
            <select id="audio-device" :value="selectedAudioId" :disabled="!canControl || secondaryLoading || commandPending" @change="selectAudio">
              <option value="">{{ t("audioChoose") }}</option>
              <optgroup v-for="group in [
                { kind: 'device', title: t('audioPhysicalDevices') },
                { kind: 'backend', title: t('audioBackendDefaults') },
              ]" :key="group.kind" :label="group.title">
                <option v-for="profile in audioProfiles.filter(profile => isPhysicalAudioDevice(profile) === (group.kind === 'device'))" :key="profile.id" :value="profile.id" :disabled="!profile.enabled">{{ audioOptionLabel(profile) }}</option>
              </optgroup>
            </select>
            <button class="button button-primary" type="button" :disabled="!canControl || secondaryLoading || commandPending || !selectedAudioProfile?.enabled" @click="applySelectedProfiles('audio')">{{ t("applyAudio") }}</button>
          </div>
          <p v-if="selectedAudioProfile?.route && isPhysicalAudioDevice(selectedAudioProfile)" class="output-route">{{ t("audioSelectedRoute") }} {{ selectedAudioProfile.route }}</p>
          <p class="helper-text" role="status" data-testid="audio-selection-state">{{ t(audioApplyState) }}</p>
          <p v-if="selectedAudioProfile && isPhysicalAudioDevice(selectedAudioProfile) && !selectedAudioProfile.enabled" class="helper-text">{{ t(selectedAudioProfile.device_reported === false ? "outputDeviceMissing" : "outputRouteUnavailable") }}</p>
          <p v-if="outputInventory?.unavailable.length" role="status" class="helper-text">{{ t("outputDiscoveryUnavailable", { methods: outputInventory.unavailable.join(", ") }) }}</p>
        </section>

        <section class="secondary-section" aria-labelledby="profiles-heading">
          <h3 id="profiles-heading">{{ t("displayAndDecoder") }}</h3>
          <div class="video-selection">
            <p><strong>{{ t("videoAppliedRoute") }}</strong> {{ canControl ? sinkLabel(state.snapshot?.output_status?.display_sink, state.snapshot?.output_status?.display_route, locale) : t('audioDisconnected') }}</p>
            <label for="video-device">{{ t("videoDeviceSelection") }}</label>
            <div class="audio-select-row">
              <select id="video-device" :value="selectedVideoId" :disabled="!canControl || secondaryLoading || commandPending" @change="selectVideo">
                <option value="">{{ t("videoChoose") }}</option>
                <optgroup v-for="group in [{kind:'device',title:t('audioPhysicalDevices')},{kind:'backend',title:t('audioBackendDefaults')}]" :key="group.kind" :label="group.title">
                  <option v-for="profile in videoProfiles.filter(profile => (profile.kind === 'device') === (group.kind === 'device'))" :key="profile.id" :value="profile.id" :disabled="!profile.enabled">{{ videoOptionLabel(profile) }}</option>
                </optgroup>
              </select>
              <button class="button button-primary" type="button" :disabled="!canControl || secondaryLoading || commandPending || !selectedVideoProfile?.enabled" @click="applySelectedProfiles('video')">{{ t("videoApply") }}</button>
            </div>
            <p v-if="selectedVideoProfile?.route" class="output-route">{{ selectedVideoProfile.route }}</p>
            <p role="status" data-testid="video-selection-state">{{ t(videoApplyState) }}</p>
          </div>
          <template v-for="group in [
                    { key: 'video', title: t('decoderSettings'), entries: profileViews.filter(profile => profile.category !== 'audio' && profile.category !== 'display') },
          ]" :key="group.key">
            <h4 v-if="group.entries.length">{{ group.title }}</h4>
            <ul v-if="group.entries.length" class="profile-list">
              <li v-for="profile in group.entries" :key="profile.id">
                <label class="profile-option" :class="{ disabled: !profile.enabled }">
                  <input :checked="selectedProfileIds.includes(profile.id)" @change="toggleProfile(profile, $event)" type="checkbox" :value="profile.id" :disabled="!canControl || secondaryLoading || commandPending || (!profile.enabled && !selectedProfileIds.includes(profile.id))" />
                  <span class="profile-copy">
                    <span class="profile-name">{{ localizedProfileName(profile) }}</span>
                    <span v-if="profile.route" class="output-route">{{ profile.route }}</span>
                    <span v-if="profile.kind === 'device' && !profile.enabled" class="profile-category">{{ t(profile.device_reported === false ? "outputDeviceMissing" : "outputRouteUnavailable") }}</span>
                    <span class="profile-category">{{ profileRole(profile) }}</span>
                  </span>
                </label>
              </li>
            </ul>
          </template>
          <button class="button button-primary apply-profiles" type="button" :disabled="!canControl || secondaryLoading || commandPending || !selectedProfileIds.some(id => !['audio','display'].includes(profiles.find(profile => profile.id === id)?.category ?? '')) || selectedProfileIds.filter(id => !['audio','display'].includes(profiles.find(profile => profile.id === id)?.category ?? '')).some(id => !profiles.find(profile => profile.id === id)?.enabled)" @click="applySelectedProfiles('other')">{{ t("applyDisplayDecoder") }}</button>
          <div v-if="profileApplySummary" class="profile-result" role="status">
            <strong>{{ profileApplySummary.applied ? t("profileSettingsApplied") : t("profileSelectionAccepted") }}</strong>
            <span>{{ t("observedPending", { observed: profileApplySummary.observedOptionCount, pending: profileApplySummary.pendingRestartOptionCount }) }}</span>
          </div>
        </section>

        <p v-if="commandError" class="error-text" role="alert">{{ t(commandError) }}</p>
      </template>
    </section>
    <div v-if="playbackView" class="controller-clearance" aria-hidden="true"></div>
    <AllFilesDialog v-if="allFilesOpen && mediaBrowser" :key="mediaBrowser.directory ?? 'roots'" :directory="mediaBrowser.directory" :fetch-window="fetchAllFilesWindow" :is-selected="isMediaSelected" :selected-count="selectedMediaCount" :busy="mediaQueueBatchPending" :labels="{title:t('mediaAllTitle'),close:t('closeAllFiles'),all:t('selectAllMedia'),clear:t('clearMediaSelection'),queue:t('addSelectedToQueue'),loading:t('loadingMediaLibrary'),selected:t('mediaSelected'),files:t('mediaQueueableFiles'),roots:t('mediaRoots'),retry:t('retry'),loadError:t('sectionLoadError'),folder:t('mediaFolder'),audio:t('trackAudio'),video:t('trackVideo'),other:t('otherTracks')}" @close="closeAllFiles" @toggle="toggleMediaSelection" @all="selectAllDirectory" @clear="clearMediaSelection" @queue="addMediaItemsToQueue()" @invalidated="clearMediaSelection" @directory="closeAllFiles(); loadMediaBrowser($event, '')" />
    <TransportBar
      v-if="playbackView"
      :playback="playbackView"
      :media-title="playerTitle"
      :can-control="canControl"
      :command-pending="commandPending"
      :has-previous="Boolean(previousQueueItem)"
      :has-next="Boolean(nextQueueItem)"
      :labels="transportLabels"
      @previous="navigateQueue('previous')"
      @skip-backward="onSkip('backward')"
      @toggle="onPlaybackToggle"
      @skip-forward="onSkip('forward')"
      @next="navigateQueue('next')"
>
      <template #controls>
            <section class="playback-controls" :aria-label="t('playbackControls')">
            <SeekBar
              :key="`${playbackView.player.sessionId ?? 'player'}:${playbackView.media.path ?? ''}`"
              :position-seconds="playbackView.controls.positionSeconds"
              :duration-seconds="playbackView.controls.durationSeconds"
              :disabled="!canControl || commandPending || state.snapshot?.seekable === false"
              :label="t('position')"
              :slider-label="t('seekPosition')"
              @seek="onTimelineSeek"
            />

            <div class="control-grid">
              <div class="range-control">
                <label for="player-volume">
                  <span>{{ t("volume") }}</span>
                  <span>{{ playbackView.controls.volumePercent === null ? "--" : `${Math.round(playbackView.controls.volumePercent)}%` }}</span>
                </label>
                <div class="volume-row">
                  <button
                    class="button button-secondary mute-button"
                    type="button"
                    :aria-label="playbackView.controls.volumePercent === 0 ? t('unmute') : t('mute')"
                    :aria-pressed="playbackView.controls.volumePercent === 0"
                    :disabled="!canControl || commandPending || playbackView.controls.volumePercent === null"
                    @click="onMuteToggle"
                  >
                    {{ playbackView.controls.volumePercent === 0 ? t("unmute") : t("mute") }}
                  </button>
                  <input
                    id="player-volume"
                    type="range"
                    min="0"
                    max="100"
                    step="1"
                    :value="playbackView.controls.volumePercent ?? 0"
                    :disabled="!canControl || commandPending || playbackView.controls.volumePercent === null"
                    :aria-label="t('volume')"
                    @change="onVolumeChange"
                  />
                </div>
              </div>
              <div class="speed-control">
                <label for="player-speed">{{ t("speed") }}</label>
                <select
                  id="player-speed"
                  :value="displayedSpeed"
                  :disabled="!canControl || commandPending || playbackView.controls.speedMultiplier === null"
                  :aria-label="t('playbackSpeed')"
                  @change="onSpeedChange"
                >
                  <option v-for="speed in speedOptions" :key="speed" :value="String(speed)">
                    {{ speed }}×
                  </option>
                </select>
              </div>
            </div>
            <p v-if="commandError" class="error-text command-error" role="alert">{{ t(commandError) }}</p>
            </section>
      </template>
    </TransportBar>
  </main>
</template>

<style scoped>
.audio-select-row { display: flex; flex-wrap: wrap; align-items: center; gap: .75rem; margin-top: .5rem; }
.audio-select-row select { flex: 1 1 18rem; min-width: 0; max-width: 100%; padding: .75rem; }
.applied-audio { overflow-wrap: anywhere; }
.output-route { overflow-wrap: anywhere; font-size: .8rem; color: var(--text-muted, #94a3b8); }
</style>

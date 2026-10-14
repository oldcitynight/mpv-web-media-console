<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue';
import type { MediaBrowserResponse, MediaBrowserItem } from './api-client.ts';
const props = defineProps<{
  directory: string | null;
  fetchWindow: (offset: number, limit: number, version?: string, signal?: AbortSignal) => Promise<MediaBrowserResponse>;
  isSelected: (item: MediaBrowserItem) => boolean;
  selectedCount: number;
  busy: boolean;
  labels: { title: string; close: string; all: string; clear: string; queue: string; loading: string; selected: string; files: string; roots: string; retry: string; loadError: string; folder: string; audio: string; video: string; other: string };
}>();
const emit = defineEmits<{ close: []; toggle: [item: MediaBrowserItem]; all: [total: number, files: number, version: string]; clear: []; queue: []; directory: [path: string]; invalidated: [] }>();
const dialog = ref<HTMLDialogElement>();
const scroller = ref<HTMLElement>();
const items = ref<MediaBrowserItem[]>([]);
const total = ref(0), files = ref(0), offset = ref(0), loadedOffset = ref(-1);
const version = ref<string>();
const loading = ref(false), error = ref(false);
const retryOffset = ref(0);
const rowHeight = 84;
let generation = 0;
let requestedOffset: number | null = null;
let windowAbort: AbortController | null = null;
const height = computed(() => total.value * rowHeight);
async function load(start: number, conflictRetry = false) {
  if (start === requestedOffset) return;
  const request = ++generation;
  windowAbort?.abort();
  if (start === loadedOffset.value) {
    requestedOffset = null; loading.value = false;
    return;
  }
  const abort = new AbortController(); windowAbort = abort;
  requestedOffset = start;
  loading.value = true; error.value = false;
  try {
    const page = await props.fetchWindow(start, 100, version.value, abort.signal);
    if (request !== generation) return;
    total.value = page.total ?? page.items.length;
    files.value = page.total_files ?? page.items.filter(item => !item.is_directory).length;
    version.value = page.version;
    offset.value = start; loadedOffset.value = start; items.value = page.items;
  } catch (e) {
    if (request !== generation) return;
    retryOffset.value = start;
    error.value = true;
    items.value = []; loadedOffset.value = -1;
    if (typeof e === 'object' && e && 'status' in e && e.status === 409 && !conflictRetry) {
      emit('invalidated'); version.value = undefined;
      if (scroller.value) scroller.value.scrollTop = 0;
      requestedOffset = null;
      await load(0, true);
    }
  } finally { if (request === generation) { requestedOffset = null; loading.value = false; } }
}
function scroll() {
  const start = Math.floor(Math.max(0, (scroller.value?.scrollTop ?? 0) / rowHeight - 5) / 50) * 50;
  if (start !== loadedOffset.value || (requestedOffset !== null && start !== requestedOffset)) void load(start);
}
onMounted(() => { dialog.value?.showModal(); void load(0); });
onBeforeUnmount(() => { generation++; windowAbort?.abort(); dialog.value?.close(); });
</script>
<template>
  <dialog ref="dialog" class="all-files-dialog" aria-labelledby="all-files-title" @cancel.prevent="!busy && emit('close')">
    <header><h3 id="all-files-title">{{ labels.title }}</h3><button class="button button-secondary" :disabled="busy" @click="emit('close')">{{ labels.close }}</button></header>
    <p class="all-files-directory">{{ directory ?? labels.roots }}</p>
    <div class="all-files-actions">
      <span role="status">{{ selectedCount }} {{ labels.selected }} / {{ total }} · {{ files }} {{ labels.files }}</span>
      <button class="button button-secondary" :disabled="busy || loading || !total" @click="emit('all', total, files, version ?? '')">{{ labels.all }}</button>
      <button class="button button-secondary" :disabled="busy || !selectedCount" @click="emit('clear')">{{ labels.clear }}</button>
      <button class="button" :disabled="busy || !selectedCount" @click="emit('queue')">{{ labels.queue }}</button>
    </div>
    <p v-if="loading" role="status">{{ labels.loading }}</p><p v-if="error" role="alert">{{ labels.loadError }} <button @click="load(retryOffset)">{{ labels.retry }}</button></p>
    <div ref="scroller" class="all-files-scroll" tabindex="0" @scroll="scroll">
      <div :style="{ height: `${height}px`, position: 'relative' }" role="list" :aria-label="labels.title">
        <div v-for="(item, index) in items" :key="item.path" class="all-files-row" role="listitem" :aria-posinset="offset + index + 1" :aria-setsize="total" :data-file-index="offset + index" :style="{ top: `${(offset + index) * rowHeight}px`, height: `${rowHeight}px` }">
          <label :title="item.name"><input type="checkbox" :checked="isSelected(item)" :disabled="busy" @change="emit('toggle', item)" /> <span>{{ item.name }}</span></label>
          <button v-if="item.is_directory" class="all-files-folder" :disabled="busy" @click="emit('directory', item.path)">▸ {{ item.name }}</button>
          <small>{{ item.is_directory ? labels.folder : item.media_kind === 'audio' ? labels.audio : item.media_kind === 'video' ? labels.video : labels.other }} · {{ item.size_bytes ?? '—' }}</small>
        </div>
      </div>
    </div>
  </dialog>
</template>

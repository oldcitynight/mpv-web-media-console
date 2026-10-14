<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from "vue";
import { formatPlaybackTime } from "./player-controls.ts";
import type { PlaybackViewModel } from "./view-models.ts";

export interface TransportBarLabels {
  controls: string;
  previous: string;
  previousShort: string;
  skipBackward: string;
  skipBackwardShort: string;
  play: string;
  pause: string;
  skipForward: string;
  skipForwardShort: string;
  next: string;
  nextShort: string;
  sending: string;
}

defineProps<{
  playback: PlaybackViewModel;
  mediaTitle: string;
  canControl: boolean;
  commandPending: boolean;
  hasPrevious: boolean;
  hasNext: boolean;
  labels: TransportBarLabels;
}>();

const dock = ref<HTMLElement>();
let observer: ResizeObserver | undefined;
onMounted(() => {
  observer = new ResizeObserver(() => document.documentElement.style.setProperty('--controller-height', `${dock.value?.getBoundingClientRect().height ?? 285}px`));
  if (dock.value) observer.observe(dock.value);
});
onBeforeUnmount(() => { observer?.disconnect(); document.documentElement.style.removeProperty('--controller-height'); });
const emit = defineEmits<{
  previous: [];
  skipBackward: [];
  toggle: [];
  skipForward: [];
  next: [];
}>();
</script>

<template>
  <section ref="dock" class="transport-dock" data-testid="remote-transport" :aria-label="labels.controls">
    <div class="transport-summary">
      <p class="transport-media" :title="mediaTitle">{{ mediaTitle }}</p>
      <p class="transport-time" aria-live="off">
        <span>{{ formatPlaybackTime(playback.controls.positionSeconds) }}</span>
        <span class="time-divider" aria-hidden="true">/</span>
        <span>{{ formatPlaybackTime(playback.controls.durationSeconds) }}</span>
      </p>
    </div>
    <slot name="controls" />
    <div class="transport-actions">
      <button
        class="transport-button"
        type="button"
        :aria-label="labels.previous"
        :disabled="!canControl || commandPending || !hasPrevious"
        @click="emit('previous')"
      >
        <span class="transport-icon" aria-hidden="true">⏮</span>
        <span class="transport-caption">{{ labels.previousShort }}</span>
      </button>
      <button
        class="transport-button"
        type="button"
        :aria-label="labels.skipBackward"
        :disabled="!canControl || commandPending || playback.controls.positionSeconds === null"
        @click="emit('skipBackward')"
      >
        <span class="transport-icon transport-skip" aria-hidden="true">{{ labels.skipBackwardShort }}</span>
      </button>
      <button
        class="transport-button transport-button-primary"
        type="button"
        :aria-label="playback.controls.paused ? labels.play : labels.pause"
        :aria-pressed="playback.controls.paused"
        :disabled="!canControl || commandPending"
        @click="emit('toggle')"
      >
        <span class="transport-icon" aria-hidden="true">{{ playback.controls.paused ? "▶" : "Ⅱ" }}</span>
        <span class="transport-caption">{{ playback.controls.paused ? labels.play : labels.pause }}</span>
      </button>
      <button
        class="transport-button"
        type="button"
        :aria-label="labels.skipForward"
        :disabled="!canControl || commandPending || playback.controls.positionSeconds === null"
        @click="emit('skipForward')"
      >
        <span class="transport-icon transport-skip" aria-hidden="true">{{ labels.skipForwardShort }}</span>
      </button>
      <button
        class="transport-button"
        type="button"
        :aria-label="labels.next"
        :disabled="!canControl || commandPending || !hasNext"
        @click="emit('next')"
      >
        <span class="transport-icon" aria-hidden="true">⏭</span>
        <span class="transport-caption">{{ labels.nextShort }}</span>
      </button>
    </div>
    <p v-if="commandPending" class="transport-pending" role="status" aria-live="polite">
      {{ labels.sending }}
    </p>
  </section>
</template>

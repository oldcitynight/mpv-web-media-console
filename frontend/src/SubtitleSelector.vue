<script setup lang="ts">
import { computed } from "vue";
import type { TrackViewModel } from "./view-models.ts";
import type { AppLocale } from "./i18n.ts";
import { trackCodecLabel } from "./media-presentation.ts";

const props = defineProps<{
  tracks: readonly TrackViewModel[];
  locale?: AppLocale;
  disabled: boolean;
  label: string;
  chooseLabel: string;
  untitledLabel: string;
  codecLabel: string;
  selectedLabel: string;
}>();

const emit = defineEmits<{ select: [trackId: number] }>();
const selectedTrack = computed(() => props.tracks.find((track) => track.selected) ?? null);
const selectedTrackId = computed(() =>
  selectedTrack.value === null ? "" : String(selectedTrack.value.id),
);

function optionLabel(track: TrackViewModel): string {
  return [
    `#${track.id}`,
    track.title || props.untitledLabel,
    track.language,
    `${props.codecLabel}: ${trackCodecLabel(track.codec, props.locale)}`,
  ]
    .filter((part): part is string => Boolean(part))
    .join(" · ");
}

function onChange(event: Event): void {
  const input = event.target;
  if (!(input instanceof HTMLSelectElement) || input.value === "") return;
  const trackId = Number(input.value);
  if (!Number.isSafeInteger(trackId)) return;
  if (props.tracks.some((track) => track.id === trackId && track.selectableType === "sub")) {
    emit("select", trackId);
  }
}
</script>

<template>
  <div class="track-selector">
    <label for="subtitle-track-select">{{ label }}</label>
    <select
      id="subtitle-track-select"
      :aria-label="label"
      :disabled="disabled"
      :value="selectedTrackId"
      @change="onChange"
    >
      <option value="" disabled>{{ chooseLabel }}</option>
      <option v-for="track in tracks" :key="track.id" :value="String(track.id)">
        {{ optionLabel(track) }}
      </option>
    </select>
    <div v-if="selectedTrack" class="track-current-selection" role="status">
      <span class="track-title">{{ selectedTrack.title || untitledLabel }}</span>
      <span v-if="selectedTrack.language" class="track-language">{{ selectedTrack.language }}</span>
      <span class="track-language">
        {{ codecLabel }}: {{ trackCodecLabel(selectedTrack.codec) }}
      </span>
      <span class="track-kind">{{ selectedLabel }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { translate, type AppLocale, type MessageKey } from "./i18n.ts";
import { layoutLabel } from "./status-presentation.ts";
import { trackCodecLabel } from "./media-presentation.ts";
import type { RemoteOutputStatusViewModel, TrackViewModel } from "./view-models.ts";

const props = defineProps<{
  locale: AppLocale;
  status: RemoteOutputStatusViewModel | null;
  tracks: readonly TrackViewModel[];
}>();

function t(key: MessageKey): string {
  return translate(props.locale, key);
}

function reported(value: string | number | null | undefined): string {
  return value === null || value === undefined ? t("notReported") : String(value);
}

function trackTypeLabel(type: string): string {
  const keys: Record<string, MessageKey> = {
    audio: "trackAudio",
    video: "trackVideo",
    sub: "trackSubtitle",
  };
  const key = keys[type.toLowerCase()];
  return key ? t(key) : t("trackOther");
}

const selectedTracks = computed(() => props.tracks.filter((track) => track.selected));
const selectedAudioTrack = computed(() =>
  selectedTracks.value.find((track) => track.type === "audio") ?? null,
);
const selectedVideoTrack = computed(() =>
  selectedTracks.value.find((track) => track.type === "video") ?? null,
);

const videoParameters = computed(() => {
  const status = props.status;
  if (status === null) return null;
  const dimensions = status.source_width !== null && status.source_height !== null
    ? `${status.source_width} × ${status.source_height}`
    : null;
  const frameRate = status.source_frame_rate === null
    ? null
    : `${status.source_frame_rate} ${t("framesPerSecond")}`;
  return [dimensions, frameRate].filter((part): part is string => part !== null).join(" · ") || null;
});

const fields = computed(() => {
  const status = props.status;
  return [
    { key: "video-codec", label: t("sourceVideoCodec"), value: selectedVideoTrack.value ? trackCodecLabel(selectedVideoTrack.value.codec, props.locale) : null },
    { key: "audio-codec", label: t("sourceAudioCodec"), value: selectedAudioTrack.value ? trackCodecLabel(selectedAudioTrack.value.codec, props.locale) : null },
    { key: "video-parameters", label: t("videoParameters"), value: videoParameters.value },
    { key: "hdr-classification", label: t("hdrClassification"), value: status?.source_dynamic_range ?? null },
    { key: "display-path", label: t("displayDrmPath"), value: status?.display_sink ?? null },
    { key: "audio-device", label: t("activeAudioDevice"), value: status?.audio_sink ?? null },
    { key: "audio-layout", label: t("audioOutputLayout"), value: status?.audio_layout ? layoutLabel(status.audio_layout, props.locale) : null },
  ];
});
</script>

<template>
  <section
    class="secondary-section info-status-section"
    aria-labelledby="info-output-heading"
    data-testid="player-info-summary"
  >
    <h3 id="info-output-heading">{{ t("playerInformation") }}</h3>
    <dl class="info-status-grid">
      <div v-for="field in fields" :key="field.key" :data-testid="`info-field-${field.key}`">
        <dt>{{ field.label }}</dt>
        <dd>{{ reported(field.value) }}</dd>
      </div>
    </dl>

    <section class="info-selected-tracks" aria-labelledby="selected-tracks-heading">
      <h4 id="selected-tracks-heading">{{ t("selectedTracks") }}</h4>
      <ul v-if="selectedTracks.length" data-testid="info-selected-tracks">
        <li
          v-for="track in selectedTracks"
          :key="`${track.type}-${track.id}`"
          :data-testid="`info-selected-track-${track.id}`"
        >
          <span>{{ trackTypeLabel(track.type) }} · #{{ track.id }} · {{ track.title ?? t("untitledTrack") }}</span>
          <span>{{ [track.language, `${t("codecLabel")}: ${trackCodecLabel(track.codec, props.locale)}`].filter(Boolean).join(" · ") }}</span>
        </li>
      </ul>
      <p v-else class="helper-text" data-testid="info-no-selected-tracks">{{ t("noSelectedTracksReported") }}</p>
    </section>

  </section>
</template>

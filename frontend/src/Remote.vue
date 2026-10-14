<script setup lang="ts">
import { translate, type AppLocale } from "./i18n.ts";
import OutputStatusBadges from "./OutputStatusBadges.vue";
import type { PlaybackViewModel, RemoteOutputStatusViewModel } from "./view-models.ts";

const props = defineProps<{
  locale: AppLocale;
  playback: PlaybackViewModel;
  outputStatus: RemoteOutputStatusViewModel | null;
  playerTitle: string;
  statusLabel: string;
}>();

function t(key: Parameters<typeof translate>[1]): string {
  return translate(props.locale, key);
}

</script>

<template>
  <div class="remote-view">
    <div class="panel-heading">
      <div>
        <p class="eyebrow">{{ t("nowPlaying") }}</p>
        <h2 id="page-remote">{{ t("remote") }}</h2>
      </div>
      <span class="status-pill">{{ statusLabel }}</span>
    </div>
    <div class="player-summary">
      <p class="media-path">{{ playerTitle }}</p>
      <OutputStatusBadges :locale="locale" :status="outputStatus" />
      <slot name="details" />
    </div>
  </div>
</template>

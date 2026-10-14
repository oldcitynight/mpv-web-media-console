<script setup lang="ts">
import { computed } from 'vue';
import { translate, type AppLocale, type MessageKey } from './i18n.ts';
import type { RemoteOutputStatusViewModel } from './view-models.ts';
import { audioFormatLabel, displaySignalPresentation, layoutLabel, rendererTargetLabel, sinkLabel, sourceRangeLabel, videoSpecification } from './status-presentation.ts';
const props = defineProps<{ locale: AppLocale; status: RemoteOutputStatusViewModel | null }>();
function t(key: MessageKey): string { return translate(props.locale, key); }
const signal = computed(() => displaySignalPresentation(props.status, props.locale));
const badges = computed(() => {
  const s = props.status;
  const rows = [
    {key:'dynamic-range',label:t('dynamicRange'),value:sourceRangeLabel(s?.source_dynamic_range, props.locale)},
    {key:'display-signal',label:t('displaySignal'),value:signal.value.label},
    {key:'resolution',label:t('resolutionFrameRate'),value:videoSpecification(s?.source_width,s?.source_height,s?.source_frame_rate, props.locale) ?? t('unknownStatus')},
    {key:'display-sink',label:t('displaySink'),value:sinkLabel(s?.display_sink,s?.display_route,props.locale)},
    {key:'audio-sink',label:t('audioSink'),value:sinkLabel(s?.audio_sink,s?.audio_route,props.locale)},
    {key:'audio-layout',label:t('audioOutputLayout'),value:layoutLabel(s?.audio_layout,props.locale)},
    {key:'audio-format',label:t('sourceAudioFormat'),value:audioFormatLabel(s?.source_audio_format,null,null) ?? t('unknownStatus')},
  ];
  rows.push({key:'renderer-target',label:t('rendererTarget'),value:rendererTargetLabel(s?.target_dynamic_range,props.locale)});
  rows.push({key:'scanout-depth',label:t('scanoutFormat'),value:signal.value.scanout ?? t('unknownStatus')});
  return rows;
});
</script>
<template>
  <section class="output-status" :aria-label="t('outputStatus')" data-testid="output-status">
    <div v-for="badge in badges" :key="badge.key" class="output-status-badge" :data-testid="`output-status-${badge.key}`">
      <span class="output-status-label">{{ badge.label }}</span><strong class="output-status-value">{{ badge.value }}</strong>
    </div>
  </section>
  <p class="helper-text output-signal-reason" :class="{'output-signal-reason-empty': !signal.reason}" :aria-hidden="!signal.reason" role="status">{{ signal.reason ?? '\u00a0' }}</p>
</template>

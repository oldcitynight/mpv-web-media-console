<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue';
import { formatPlaybackTime } from './player-controls.ts';
import { beginSeekDraft, cancelSeekDraft, commitSeekDraft, displayedSeekPosition, idleSeekDraft, updateSeekDraft } from './seek-bar-state.ts';
const props = defineProps<{ positionSeconds: number | null; durationSeconds: number | null; disabled: boolean; label: string; sliderLabel: string }>();
const emit = defineEmits<{ seek: [seconds: number] }>();
const input = ref<HTMLInputElement>();
const draft = ref(idleSeekDraft());
const pending = ref<number | null>(null);
let pointer: number | null = null;
let timer: ReturnType<typeof setTimeout> | undefined;
const usableDuration = computed(() => props.durationSeconds !== null && Number.isFinite(props.durationSeconds) && props.durationSeconds > 0);
const shownPosition = computed(() => displayedSeekPosition(draft.value, pending.value ?? props.positionSeconds) ?? 0);
function cancel() {
  const id = pointer; pointer = null;
  if (id !== null && input.value?.hasPointerCapture(id)) input.value.releasePointerCapture(id);
  draft.value = cancelSeekDraft();
}
watch(() => props.disabled || !usableDuration.value, disabled => { if (disabled) cancel(); });
watch(() => props.durationSeconds, () => { cancel(); pending.value = null; });
watch(() => props.positionSeconds, value => {
  if (pending.value !== null && value !== null && Math.abs(value - pending.value) < 2) { pending.value = null; clearTimeout(timer); }
});
function begin() {
  if (props.disabled || !usableDuration.value || props.durationSeconds === null) return;
  if (!draft.value.active) draft.value = beginSeekDraft(props.positionSeconds, props.durationSeconds);
}
function updateFromPointer(event: PointerEvent) {
  if (props.durationSeconds === null || !input.value) return;
  const bounds = input.value.getBoundingClientRect();
  // Match the native thumb centre at each end of the track.
  const fraction = Math.max(0,Math.min(1,(event.clientX - bounds.left - 8) / Math.max(1,bounds.width - 16)));
  draft.value = updateSeekDraft(draft.value,fraction * props.durationSeconds,props.durationSeconds);
}
function down(event: PointerEvent) {
  if (props.disabled || !usableDuration.value || pointer !== null || !event.isPrimary || (event.pointerType === 'mouse' && event.button !== 0)) return;
  begin(); pointer = event.pointerId; input.value?.focus(); input.value?.setPointerCapture(event.pointerId); updateFromPointer(event);
}
function move(event: PointerEvent) { if (pointer === event.pointerId) updateFromPointer(event); }
function commit() {
  const result = commitSeekDraft(draft.value); draft.value = result.state;
  if (result.target !== null) {
    pending.value = result.target; clearTimeout(timer);
    timer = setTimeout(() => { pending.value = null; },3000);
    emit('seek',result.target);
  }
}
function up(event: PointerEvent) {
  if (pointer !== event.pointerId || props.disabled) return;
  updateFromPointer(event); const id = pointer; pointer = null;
  if (input.value?.hasPointerCapture(id)) input.value.releasePointerCapture(id);
  commit();
}
function keyboardInput(event: Event) {
  if (pointer !== null || props.disabled || props.durationSeconds === null) return;
  begin(); draft.value = updateSeekDraft(draft.value,Number((event.target as HTMLInputElement).value),props.durationSeconds);
}
function keyboardChange(event: Event) { if (pointer === null && !props.disabled) { keyboardInput(event); commit(); } }
onBeforeUnmount(() => { cancel(); clearTimeout(timer); });
</script>
<template>
  <div class="range-control timeline-control">
    <label for="seek-position"><span>{{ label }}</span><span>{{ formatPlaybackTime(shownPosition) }}</span></label>
    <input ref="input" id="seek-position" type="range" min="0" :max="usableDuration ? durationSeconds ?? 0 : 0" step="1" :value="shownPosition" :disabled="disabled || !usableDuration" :aria-label="sliderLabel"
      @pointerdown.prevent="down" @pointermove.prevent="move" @pointerup.prevent="up" @pointercancel="cancel" @lostpointercapture="pointer !== null && cancel()"
      @keydown="begin" @input="keyboardInput" @change="keyboardChange" />
  </div>
</template>

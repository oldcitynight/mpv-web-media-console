<script setup lang="ts">
import { translate, type AppLocale } from "./i18n.ts";
const props = defineProps<{ locale: AppLocale; offset: number; pageSize: number; total: number; nextOffset: number | null; busy: boolean }>();
const emit = defineEmits<{ page: [offset: number]; all: [] }>();
</script>
<template>
  <nav class="media-page-controls" :aria-label="translate(locale, 'mediaNavigation')" data-testid="browser-pager">
    <button type="button" class="button button-secondary" :disabled="busy || offset <= 0" @click="emit('page', Math.max(0, offset - pageSize))">{{ translate(locale, 'mediaPreviousPage') }}</button>
    <span class="media-page-number" role="status" :title="`${total}`">{{ translate(locale, 'mediaPageCompact', {page: Math.floor(offset / pageSize) + 1, pages: Math.max(1, Math.ceil(total / pageSize))}) }}</span>
    <button type="button" class="button button-secondary" :disabled="busy || nextOffset === null" @click="nextOffset !== null && emit('page', nextOffset)">{{ translate(locale, 'mediaNextPage') }}</button>
    <button type="button" class="button button-secondary" :disabled="busy" :aria-label="translate(locale, 'mediaAllDescription')" @click="emit('all')">{{ translate(locale, 'showAllFiles') }}</button>
  </nav>
</template>

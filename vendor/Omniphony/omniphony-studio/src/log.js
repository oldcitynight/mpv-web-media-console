/**
 * Logging system — log panel, entries, level control.
 */

import { t, tf, i18nState } from './i18n.js';

function getLogOverlayEl() { return document.getElementById('logOverlay'); }
function getLogSummaryEl() { return document.getElementById('logSummary'); }
function getLogEntriesEl() { return document.getElementById('logEntries'); }
function getLogEmptyStateEl() { return document.getElementById('logEmptyState'); }
function getLogToggleBtnEl() { return document.getElementById('logToggleBtn'); }
function getLogLevelSelectEl() { return document.getElementById('logLevelSelect'); }
function getLogFilterInputEl() { return document.getElementById('logFilterInput'); }

const LOG_ENTRY_LIMIT = 120;
export const LOG_LEVEL_VALUES = ['off', 'error', 'warn', 'info', 'debug', 'trace'];

export const logState = {
  expanded: false,
  entries: [],
  backendLogLevel: 'info',
  filterText: ''
};

function formatLogTime(date) {
  return new Intl.DateTimeFormat(i18nState.locale, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  }).format(date);
}

function formatLogTimestampForExport(date) {
  const pad = (value) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function normalizeLogMessage(message) {
  return typeof message === 'string' ? message : String(message ?? '');
}

function normalizeLogTarget(target) {
  return typeof target === 'string' ? target.trim() : '';
}

function normalizeFilterText(value) {
  return typeof value === 'string' ? value : String(value ?? '');
}

function buildRenderedMessage(entry) {
  const message = normalizeLogMessage(entry.message);
  if (message.startsWith('[')) {
    return message;
  }
  return entry.target ? `[${entry.target}] ${message}` : message;
}

function buildSearchableText(entry) {
  return [
    buildRenderedMessage(entry),
    entry.target,
    String(entry.level || 'info')
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
}

function getFilteredEntries() {
  const filter = normalizeFilterText(logState.filterText).trim().toLowerCase();
  if (!filter) return logState.entries;
  return logState.entries.filter((entry) => buildSearchableText(entry).includes(filter));
}

export function serializeLogsForClipboard(entries = getFilteredEntries()) {
  return entries
    .map((entry) => `[${formatLogTimestampForExport(entry.timestamp)}] ${String(entry.level || 'info').toUpperCase()} ${buildRenderedMessage(entry)}`)
    .join('\n');
}

export function normalizeLogError(error) {
  if (error instanceof Error) {
    return error.message || error.name || String(error);
  }
  if (typeof error === 'string') return error;
  try {
    return JSON.stringify(error);
  } catch (_e) {
    return String(error);
  }
}

export function normalizeLogLevel(value) {
  const normalized = String(value || '').trim().toLowerCase();
  return LOG_LEVEL_VALUES.includes(normalized) ? normalized : 'info';
}

export function pushLog(level, message, target = '') {
  logState.entries.push({
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    level: ['warn', 'error', 'debug', 'trace'].includes(level) ? level : 'info',
    target: normalizeLogTarget(target),
    message: normalizeLogMessage(message),
    timestamp: new Date()
  });
  if (logState.entries.length > LOG_ENTRY_LIMIT) {
    logState.entries.splice(0, logState.entries.length - LOG_ENTRY_LIMIT);
  }
  renderLogPanel();
}

export function setLogFilterText(value) {
  logState.filterText = normalizeFilterText(value);
  renderLogPanel();
}

export function renderLogLevelControl() {
  const logLevelSelectEl = getLogLevelSelectEl();
  if (!logLevelSelectEl) return;
  const value = normalizeLogLevel(logState.backendLogLevel);
  if (logLevelSelectEl.value !== value) {
    logLevelSelectEl.value = value;
  }
}

function renderLogFilterControl() {
  const logFilterInputEl = getLogFilterInputEl();
  if (!logFilterInputEl) return;
  if (logFilterInputEl.value !== logState.filterText) {
    logFilterInputEl.value = logState.filterText;
  }
}

export function renderLogPanel() {
  const logOverlayEl = getLogOverlayEl();
  const logSummaryEl = getLogSummaryEl();
  const logEntriesEl = getLogEntriesEl();
  const logEmptyStateEl = getLogEmptyStateEl();
  const logToggleBtnEl = getLogToggleBtnEl();
  if (!logOverlayEl || !logSummaryEl || !logEntriesEl || !logEmptyStateEl || !logToggleBtnEl) return;

  const filterActive = logState.filterText.trim().length > 0;
  const visibleEntries = getFilteredEntries();
  const latestVisible = visibleEntries[visibleEntries.length - 1];
  const latestOverall = logState.entries[logState.entries.length - 1];
  const summaryEntry = filterActive ? latestVisible : latestOverall;

  logOverlayEl.classList.toggle('expanded', logState.expanded);
  logOverlayEl.classList.toggle('collapsed', !logState.expanded);
  logSummaryEl.textContent = summaryEntry ? buildRenderedMessage(summaryEntry) : t('log.empty');
  logToggleBtnEl.textContent = logState.expanded ? '▴' : '▾';
  logToggleBtnEl.title = t(logState.expanded ? 'log.collapse' : 'log.expand');
  logEmptyStateEl.style.display = visibleEntries.length > 0 ? 'none' : 'block';
  logEmptyStateEl.textContent = logState.entries.length === 0
    ? t('log.empty')
    : (filterActive ? t('log.noMatch') : t('log.empty'));
  logEntriesEl.innerHTML = '';
  renderLogFilterControl();
  if (visibleEntries.length === 0) return;

  const fragment = document.createDocumentFragment();
  visibleEntries.slice().reverse().forEach((entry) => {
    const row = document.createElement('div');
    row.className = 'log-entry';

    const timeEl = document.createElement('div');
    timeEl.className = 'log-entry-time';
    timeEl.textContent = formatLogTime(entry.timestamp);

    const levelEl = document.createElement('div');
    levelEl.className = `log-entry-level ${['warn', 'error', 'debug', 'trace'].includes(entry.level) ? entry.level : 'info'}`;
    levelEl.textContent = t(`log.level.${entry.level}`);

    const msgEl = document.createElement('div');
    msgEl.className = 'log-entry-message';
    msgEl.textContent = buildRenderedMessage(entry);

    row.appendChild(timeEl);
    row.appendChild(levelEl);
    row.appendChild(msgEl);
    fragment.appendChild(row);
  });
  logEntriesEl.appendChild(fragment);
}

export function setLogExpanded(next) {
  logState.expanded = Boolean(next);
  renderLogPanel();
}

export async function copyLogsToClipboard() {
  const visibleEntries = getFilteredEntries();
  if (!visibleEntries.length) {
    pushLog('warn', t('log.copyEmpty'));
    return;
  }
  const text = serializeLogsForClipboard(visibleEntries);
  try {
    if (navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.setAttribute('readonly', 'true');
      textarea.style.position = 'fixed';
      textarea.style.left = '-9999px';
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand('copy');
      document.body.removeChild(textarea);
    }
    pushLog('info', tf('log.copySuccess', { count: visibleEntries.length }));
  } catch (error) {
    pushLog('error', tf('log.copyFailed', { error: normalizeLogError(error) }));
  }
}

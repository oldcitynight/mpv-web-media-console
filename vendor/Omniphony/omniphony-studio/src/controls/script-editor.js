/**
 * Local editor for renderer-owned backend files (e.g. the scriptable backend's
 * `.lua`). The file's content lives on the renderer, so the editor LOADS it with
 * `backend_file_get` and SAVES it with `backend_file_put` over OSC — it never
 * reads or writes a cross-host path. That is what makes editing work when orender
 * runs on another machine: bytes travel, not a meaningless local path.
 *
 * Selection: a managed-file picker (populated from `backend_file_list`) plus, only
 * when the renderer is local, the native Browse dialog. Saving under a name writes
 * the renderer's managed store and makes it the active file.
 */

import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { EditorView, basicSetup } from 'codemirror';
import { Compartment, EditorState } from '@codemirror/state';
import { StreamLanguage } from '@codemirror/language';
import { lua } from '@codemirror/legacy-modes/mode/lua';
import { oneDark } from '@codemirror/theme-one-dark';
import {
  amy, ayuLight, cobalt, coolGlow, dracula, espresso,
  rosePineDawn, solarizedLight, tomorrow,
} from 'thememirror';
import { t } from '../i18n.js';

// Selectable editor colour themes. `ext: null` keeps CodeMirror's built-in light
// theme. Dark themes come first since the Studio panel is dark; the default is
// One Dark. Each theme extension carries both the editor chrome and the syntax
// highlight colours, so applying one is a single extension.
const THEMES = [
  { id: 'one-dark', label: 'One Dark', ext: oneDark },
  { id: 'dracula', label: 'Dracula', ext: dracula },
  { id: 'cobalt', label: 'Cobalt', ext: cobalt },
  { id: 'cool-glow', label: 'Cool Glow', ext: coolGlow },
  { id: 'espresso', label: 'Espresso', ext: espresso },
  { id: 'amy', label: 'Amy', ext: amy },
  { id: 'tomorrow', label: 'Tomorrow (light)', ext: tomorrow },
  { id: 'solarized-light', label: 'Solarized Light', ext: solarizedLight },
  { id: 'ayu-light', label: 'Ayu Light', ext: ayuLight },
  { id: 'rose-pine-dawn', label: 'Rosé Pine Dawn', ext: rosePineDawn },
  { id: 'default', label: 'Default (light)', ext: null },
];
const THEME_KEY = 'omniphony.scriptEditor.theme';
const DEFAULT_THEME = 'one-dark';
const themeCompartment = new Compartment();

function selectedThemeId() {
  const id = localStorage.getItem(THEME_KEY);
  return THEMES.some((entry) => entry.id === id) ? id : DEFAULT_THEME;
}

function themeExtensionFor(id) {
  const entry = THEMES.find((e) => e.id === id);
  return entry && entry.ext ? entry.ext : [];
}

let modalEl = null;
let view = null;
let filenameEl = null;
let pickerEl = null;
let themeEl = null;
let browseBtn = null;
let statusEl = null;
// The file currently open: { backend, key, language, extensions, rendererIsLocal }.
let session = null;
let listenersBound = false;

function setStatus(text, isError) {
  if (!statusEl) return;
  statusEl.textContent = text || '';
  statusEl.style.color = isError ? '#ff9c9c' : 'rgba(217,236,255,0.65)';
}

function langExtensions(language) {
  return language === 'lua' ? [StreamLanguage.define(lua)] : [];
}

// Replace the whole document, keeping the language + theme for the active session.
// Used on load and New so the editor stays consistent.
function resetEditor(text) {
  if (!view) return;
  view.setState(EditorState.create({
    doc: text || '',
    extensions: [
      basicSetup,
      ...langExtensions(session && session.language),
      themeCompartment.of(themeExtensionFor(selectedThemeId())),
    ],
  }));
}

function editorContent() {
  return view ? view.state.doc.toString() : '';
}

function suggestedNewName(extensions) {
  const ext = Array.isArray(extensions) && extensions.length ? extensions[0] : 'txt';
  return `untitled.${ext}`;
}

function matchesSession(payload) {
  return session && payload
    && String(payload.backend) === String(session.backend)
    && (payload.key == null || String(payload.key) === String(session.key));
}

function bindGlobalListeners() {
  if (listenersBound) return;
  listenersBound = true;
  listen('backend-file:content', ({ payload }) => {
    if (!matchesSession(payload)) return;
    resetEditor(payload.content || '');
    if (payload.name && filenameEl) filenameEl.value = String(payload.name);
    setStatus(t('backend.file.editor.loaded'));
  });
  listen('backend-file:list', ({ payload }) => {
    if (!session || !payload || String(payload.backend) !== String(session.backend)) return;
    populatePicker(Array.isArray(payload.names) ? payload.names : []);
  });
  listen('backend-file:error', ({ payload }) => {
    if (!matchesSession(payload)) return;
    setStatus(payload.message || 'error', true);
  });
}

function populatePicker(names) {
  if (!pickerEl) return;
  pickerEl.replaceChildren();
  const def = document.createElement('option');
  def.value = '';
  def.textContent = t('backend.file.editor.pickerDefault');
  pickerEl.appendChild(def);
  for (const name of names) {
    const opt = document.createElement('option');
    opt.value = String(name);
    opt.textContent = String(name);
    pickerEl.appendChild(opt);
  }
}

function button(label) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'mini-btn';
  b.textContent = label;
  return b;
}

function buildModal() {
  modalEl = document.createElement('div');
  modalEl.className = 'script-editor-modal';
  modalEl.style.cssText = 'position:fixed;inset:0;z-index:1200;display:none;'
    + 'align-items:center;justify-content:center;background:rgba(0,0,0,0.55)';

  const panel = document.createElement('div');
  panel.style.cssText = 'width:min(840px,92vw);height:min(660px,88vh);display:flex;'
    + 'flex-direction:column;gap:0.5rem;background:#10151c;color:#d9ecff;'
    + 'border:1px solid rgba(255,255,255,0.12);border-radius:10px;padding:0.8rem 0.9rem;'
    + 'box-shadow:0 12px 40px rgba(0,0,0,0.5)';

  // Header: title + close.
  const header = document.createElement('div');
  header.style.cssText = 'display:flex;align-items:center;justify-content:space-between';
  const title = document.createElement('strong');
  title.textContent = t('backend.file.editor.title');
  // Colour-theme picker (persisted): the Studio panel is dark, so a dark editor
  // theme reads far better than CodeMirror's default light one.
  themeEl = document.createElement('select');
  themeEl.title = t('backend.file.editor.theme');
  themeEl.style.fontSize = '12px';
  for (const entry of THEMES) {
    const opt = document.createElement('option');
    opt.value = entry.id;
    opt.textContent = entry.label;
    themeEl.appendChild(opt);
  }
  themeEl.value = selectedThemeId();
  themeEl.addEventListener('change', () => {
    localStorage.setItem(THEME_KEY, themeEl.value);
    if (view) {
      view.dispatch({ effects: themeCompartment.reconfigure(themeExtensionFor(themeEl.value)) });
    }
  });

  const closeBtn = button(t('backend.file.editor.close'));
  closeBtn.addEventListener('click', closeEditor);
  const headerRight = document.createElement('div');
  headerRight.style.cssText = 'display:flex;gap:0.4rem;align-items:center';
  headerRight.append(themeEl, closeBtn);
  header.append(title, headerRight);

  // Toolbar: managed-file picker, filename, Browse, New, Reload, Save.
  const toolbar = document.createElement('div');
  toolbar.style.cssText = 'display:flex;gap:0.35rem;align-items:center;flex-wrap:wrap';

  pickerEl = document.createElement('select');
  pickerEl.style.minWidth = '9rem';
  pickerEl.addEventListener('change', () => {
    const name = pickerEl.value;
    if (!name) return;
    filenameEl.value = name;
    requestContent(name);
  });

  filenameEl = document.createElement('input');
  filenameEl.type = 'text';
  filenameEl.className = 'delay-input';
  filenameEl.style.minWidth = '10rem';
  filenameEl.placeholder = t('backend.file.editor.filenamePlaceholder');

  browseBtn = button(t('backend.file.browse'));
  browseBtn.addEventListener('click', async () => {
    const exts = (session && session.extensions) || [];
    const picked = await invoke('pick_backend_file_path', { extensions: exts }).catch(() => null);
    if (picked) {
      filenameEl.value = picked;
      requestContent(picked);
    }
  });

  const newBtn = button(t('backend.file.editor.new'));
  newBtn.addEventListener('click', () => {
    filenameEl.value = suggestedNewName(session && session.extensions);
    resetEditor('');
    setStatus(t('backend.file.editor.new'));
    view && view.focus();
  });

  const reloadBtn = button(t('backend.file.editor.reload'));
  reloadBtn.addEventListener('click', () => requestContent(filenameEl.value.trim() || null));

  const saveBtn = button(t('backend.file.editor.save'));
  saveBtn.addEventListener('click', save);

  toolbar.append(pickerEl, filenameEl, browseBtn, newBtn, reloadBtn, saveBtn);

  // Editor host.
  const host = document.createElement('div');
  host.style.cssText = 'flex:1;min-height:0;overflow:auto;border:1px solid '
    + 'rgba(255,255,255,0.1);border-radius:6px';

  statusEl = document.createElement('div');
  statusEl.style.cssText = 'font-size:12px;min-height:1.2em';

  panel.append(header, toolbar, host, statusEl);
  modalEl.appendChild(panel);
  // Click on the dimmed backdrop (not the panel) closes.
  modalEl.addEventListener('mousedown', (e) => { if (e.target === modalEl) closeEditor(); });
  document.body.appendChild(modalEl);

  view = new EditorView({
    parent: host,
    doc: '',
    extensions: [basicSetup, themeCompartment.of(themeExtensionFor(selectedThemeId()))],
  });
}

// Ask the renderer for content: an explicit `name` previews a managed file;
// `null` reads the param's current handle.
function requestContent(name) {
  if (!session) return;
  setStatus(t('backend.file.editor.loading'));
  const args = { backend: session.backend, key: session.key };
  if (name) args.name = name;
  invoke('backend_file_get', args).catch((e) => setStatus(String(e), true));
}

function save() {
  if (!session) return;
  const name = filenameEl.value.trim();
  if (!name) {
    setStatus(t('backend.file.editor.nameRequired'), true);
    return;
  }
  setStatus(t('backend.file.editor.saving'));
  invoke('backend_file_put', {
    backend: session.backend,
    key: session.key,
    name,
    content: editorContent(),
  }).catch((e) => setStatus(String(e), true));
}

function closeEditor() {
  if (modalEl) modalEl.style.display = 'none';
  session = null;
}

/**
 * Open the editor for a backend file param. `opts` = { backend, key, language,
 * extensions, rendererIsLocal }. Loads the param's current content and the list
 * of managed files; Browse is shown only when the renderer is local.
 */
export function openBackendFileEditor(opts) {
  if (!opts || !opts.backend || !opts.key) return;
  if (!modalEl) buildModal();
  bindGlobalListeners();
  session = {
    backend: String(opts.backend),
    key: String(opts.key),
    language: opts.language || null,
    extensions: Array.isArray(opts.extensions) ? opts.extensions : [],
    rendererIsLocal: opts.rendererIsLocal !== false,
  };
  if (browseBtn) browseBtn.style.display = session.rendererIsLocal ? '' : 'none';
  populatePicker([]);
  resetEditor('');
  modalEl.style.display = 'flex';
  // Fetch the managed-file list and the current content.
  invoke('backend_file_list', { backend: session.backend }).catch(() => {});
  requestContent(null);
}

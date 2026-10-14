// SOFA HRTF database browser: navigates the Apache index of
// sofacoustics.org/data via the `sofa_browse` Tauri command, downloads a
// chosen .sofa with `sofa_download`, then activates it through the existing
// `control_hrir_source` command (`sofa:<local path>`).
//
// State is one percent-encoded relative path ("" = root). Starts in
// "database/", where the per-subject HRTF sets live.

import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { open as openFileDialog } from '@tauri-apps/plugin-dialog';

const el = (id) => document.getElementById(id);

let currentPath = 'database/';
let busy = false;
// Remote path (currentPath + href) of the currently active .sofa, so the
// entry stays visibly marked while scrolling / navigating / reopening.
let activeRemotePath = '';
// Browser mode: 'local' (default — already-downloaded files, no network)
// or 'remote' (sofacoustics.org). Going online asks for confirmation once
// per session.
let mode = 'local';
let onlineConsent = false;
// Active local .sofa path, fed from the renderer state broadcast
// (binaural.hrtfSofaPath) so the highlight survives Studio restarts.
let activeSofaPath = '';

export function setActiveSofaPath(path) {
  activeSofaPath = typeof path === 'string' ? path : '';
}

const fmtMB = (b) => `${(b / (1024 * 1024)).toFixed(1)} MB`;

// Compact label + severity for a GLOBAL:License attribute value. SOFA files
// embed anything from a short identifier to the full license paragraph; we
// classify the common cases and truncate the rest (full text in tooltip).
function licenseLabel(raw) {
  const t = (raw || '').trim();
  if (!t) return { label: 'no license — ask the author', warn: true, full: '' };
  const l = t.toLowerCase();
  const has = (...xs) => xs.every((x) => l.includes(x));
  if (has('cc0') || has('public domain')) return { label: 'CC0 / public domain', warn: false, full: t };
  if (has('by-nc-sa') || has('nc-sa')) return { label: 'CC BY-NC-SA (non-commercial)', warn: true, full: t };
  if (has('by-nc') || has('non-commercial')) return { label: 'CC BY-NC (non-commercial)', warn: true, full: t };
  if (has('by-sa')) return { label: 'CC BY-SA', warn: false, full: t };
  if (has('creativecommons') && has('/by/')) return { label: 'CC BY', warn: false, full: t };
  if (l.startsWith('cc by') || l === 'cc-by') return { label: t.length > 30 ? 'CC BY' : t, warn: false, full: t };
  if (has('mit license') || l === 'mit') return { label: 'MIT', warn: false, full: t };
  if (has('no license')) return { label: 'no license — ask the author', warn: true, full: t };
  const short = t.length > 60 ? `${t.slice(0, 57)}…` : t;
  return { label: short, warn: false, full: t };
}

function licenseLine(meta) {
  const lic = licenseLabel(meta && meta.license);
  const div = document.createElement('div');
  div.style.cssText = `font-size:0.62rem;${lic.warn ? 'color:#e8c46a;' : 'color:#7d92a6;'}word-break:break-word;`;
  const parts = [`⚖ ${lic.label}`];
  if (meta && meta.organization) parts.push(meta.organization);
  div.textContent = parts.join(' — ');
  const tip = [lic.full, meta && meta.author ? `Contact: ${meta.author}` : '']
    .filter(Boolean).join('\n');
  if (tip) div.title = tip;
  return div;
}

function setDownloadUiVisible(visible) {
  const row = el('sofaDlRow');
  if (row) row.style.display = visible ? 'flex' : 'none';
  if (visible) {
    const bar = el('sofaDlBar');
    const txt = el('sofaDlText');
    if (bar) bar.style.width = '0%';
    if (txt) txt.textContent = '';
  }
}

function onDownloadProgress({ bytes, total }) {
  const bar = el('sofaDlBar');
  const txt = el('sofaDlText');
  if (total) {
    const pct = Math.min(100, (bytes / total) * 100);
    if (bar) bar.style.width = `${pct.toFixed(1)}%`;
    if (txt) txt.textContent = `${fmtMB(bytes)} / ${fmtMB(total)}`;
  } else {
    // Unknown size: full-width pulse + byte counter.
    if (bar) { bar.style.width = '100%'; bar.style.opacity = '0.35'; }
    if (txt) txt.textContent = fmtMB(bytes);
  }
}

function setStatus(text, isError = false) {
  const n = el('sofaBrowserStatus');
  if (!n) return;
  n.textContent = text || '';
  n.style.color = isError ? '#ff7a7a' : '#c9d6e2';
}

function renderCrumbs() {
  const n = el('sofaBrowserCrumbs');
  if (!n) return;
  n.textContent = '';
  const mk = (label, path) => {
    const a = document.createElement('a');
    a.textContent = label;
    a.href = '#';
    a.style.color = '#7fb3e8';
    a.addEventListener('click', (e) => {
      e.preventDefault();
      navigate(path);
    });
    return a;
  };
  n.appendChild(mk('data', ''));
  let acc = '';
  for (const seg of currentPath.split('/').filter(Boolean)) {
    acc += `${seg}/`;
    n.appendChild(document.createTextNode(' / '));
    n.appendChild(mk(decodeURIComponent(seg), acc));
  }
}

function renderList(entries) {
  const list = el('sofaBrowserList');
  if (!list) return;
  list.textContent = '';
  if (currentPath) {
    const up = document.createElement('div');
    up.textContent = '⬑ ..';
    up.style.cssText = 'cursor:pointer;padding:0.15rem 0.3rem;color:#8fa6bd;';
    up.addEventListener('click', () => {
      const parts = currentPath.split('/').filter(Boolean);
      parts.pop();
      navigate(parts.length ? `${parts.join('/')}/` : '');
    });
    list.appendChild(up);
  }
  if (!entries.length) {
    const empty = document.createElement('div');
    empty.textContent = '(no subfolders or .sofa files here)';
    empty.style.cssText = 'padding:0.15rem 0.3rem;color:#777;';
    list.appendChild(empty);
    return;
  }
  let activeRow = null;
  for (const entry of entries) {
    const row = document.createElement('div');
    const isActive = !entry.dir && currentPath + entry.href === activeRemotePath;
    row.dataset.sofaPath = entry.dir ? '' : currentPath + entry.href;
    row.style.cssText =
      'display:flex;justify-content:space-between;gap:0.5rem;cursor:pointer;' +
      'padding:0.15rem 0.3rem;border-radius:4px;';
    if (isActive) {
      row.style.background = 'rgba(86,156,255,0.22)';
      row.style.boxShadow = 'inset 2px 0 0 #569cff';
      activeRow = row;
    }
    row.addEventListener('mouseenter', () => {
      if (row.dataset.sofaPath !== activeRemotePath || !row.dataset.sofaPath) {
        row.style.background = 'rgba(255,255,255,0.06)';
      }
    });
    row.addEventListener('mouseleave', () => {
      row.style.background =
        row.dataset.sofaPath && row.dataset.sofaPath === activeRemotePath
          ? 'rgba(86,156,255,0.22)'
          : '';
    });
    const name = document.createElement('span');
    name.textContent = entry.dir
      ? `📁 ${entry.name}/`
      : `🎧 ${entry.name}${isActive ? '  ✓ active' : ''}`;
    name.style.wordBreak = 'break-all';
    if (isActive) name.style.color = '#9cc4ff';
    const size = document.createElement('span');
    size.textContent = entry.dir ? '' : entry.size;
    size.style.cssText = 'color:#8fa6bd;white-space:nowrap;';
    row.append(name, size);
    row.addEventListener('click', () => {
      if (entry.dir) {
        navigate(currentPath + entry.href);
      } else {
        downloadAndActivate(entry);
      }
    });
    list.appendChild(row);
  }
  // Bring the active file back into view when (re)entering its folder.
  if (activeRow) activeRow.scrollIntoView({ block: 'nearest' });
}

function setChrome() {
  const title = el('sofaBrowserTitle');
  const toggle = el('sofaSourceToggleBtn');
  const crumbs = el('sofaBrowserCrumbs');
  if (mode === 'local') {
    if (title) title.textContent = 'SOFA HRTF files — downloaded';
    if (toggle) { toggle.textContent = 'Online database…'; toggle.style.display = ''; }
    if (crumbs) crumbs.textContent = '';
  } else {
    if (title) title.textContent = 'SOFA HRTF database — sofacoustics.org';
    if (toggle) { toggle.textContent = '← Local files'; toggle.style.display = ''; }
  }
  const note = el('sofaLicenseNote');
  if (note) note.style.display = mode === 'remote' ? '' : 'none';
}

async function showLocal() {
  mode = 'local';
  setChrome();
  setStatus('');
  const list = el('sofaBrowserList');
  if (!list) return;
  list.textContent = 'Loading…';
  let files = [];
  try {
    files = await invoke('sofa_list_local', {});
  } catch (e) {
    setStatus(String(e), true);
    return;
  }
  list.textContent = '';

  // Action header: import from this machine, wipe the cache.
  const actions = document.createElement('div');
  actions.style.cssText =
    'display:flex;gap:0.4rem;justify-content:flex-end;padding:0.15rem 0.3rem 0.3rem;' +
    'border-bottom:1px solid rgba(255,255,255,0.06);margin-bottom:0.2rem;';
  const importBtn = document.createElement('button');
  importBtn.type = 'button';
  importBtn.className = 'toggle-btn';
  importBtn.textContent = '＋ Import…';
  importBtn.title = 'Copy .sofa files from this machine into the cache';
  importBtn.addEventListener('click', () => importFromDisk());
  actions.appendChild(importBtn);
  if (files.length) {
    const delAll = document.createElement('button');
    delAll.type = 'button';
    delAll.className = 'toggle-btn';
    delAll.textContent = '🗑 Delete all';
    delAll.addEventListener('click', () => confirmDeleteAll(files));
    actions.appendChild(delAll);
  }
  list.appendChild(actions);

  if (!files.length) {
    const empty = document.createElement('div');
    empty.textContent = 'No downloaded SOFA files yet — use “Online database…” or “＋ Import…”.';
    empty.style.cssText = 'padding:0.3rem;color:#8fa6bd;';
    list.appendChild(empty);
    return;
  }
  let activeRow = null;
  for (const f of files) {
    const row = document.createElement('div');
    const isActive = f.path === activeSofaPath;
    row.style.cssText =
      'display:flex;justify-content:space-between;gap:0.5rem;cursor:pointer;' +
      'padding:0.15rem 0.3rem;border-radius:4px;';
    if (isActive) {
      row.style.background = 'rgba(86,156,255,0.22)';
      row.style.boxShadow = 'inset 2px 0 0 #569cff';
      activeRow = row;
    }
    const left = document.createElement('div');
    left.style.cssText = 'display:grid;gap:0.05rem;min-width:0;';
    const name = document.createElement('span');
    name.textContent = `🎧 ${f.name}${isActive ? '  ✓ active' : ''}`;
    name.style.wordBreak = 'break-all';
    if (isActive) name.style.color = '#9cc4ff';
    left.append(name, licenseLine(f.meta));
    const right = document.createElement('div');
    right.style.cssText = 'display:flex;align-items:center;gap:0.35rem;white-space:nowrap;';
    const size = document.createElement('span');
    size.textContent = f.size;
    size.style.cssText = 'color:#8fa6bd;';
    const upload = document.createElement('button');
    upload.type = 'button';
    upload.className = 'toggle-btn';
    upload.textContent = '⇪';
    upload.title = 'Send to the renderer over OSC and activate there (for a renderer on another machine)';
    upload.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (busy) return;
      busy = true;
      setStatus(`Uploading ${f.name} to the renderer…`);
      try {
        const chunks = await invoke('sofa_upload_to_renderer', { path: f.path });
        setStatus(`✓ Sent ${f.name} (${chunks} chunks) — the renderer stores and activates it (see the HRTF file line).`);
      } catch (err) {
        setStatus(String(err), true);
      } finally {
        busy = false;
      }
    });
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'toggle-btn';
    del.textContent = '🗑';
    del.title = 'Delete this cached file';
    del.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (busy) return;
      busy = true;
      try {
        await invoke('sofa_delete_local', { paths: [f.path] });
        setStatus(`Deleted ${f.name}.`);
      } catch (err) {
        setStatus(String(err), true);
      } finally {
        busy = false;
      }
      showLocal();
    });
    right.append(size, upload, del);
    row.append(left, right);
    row.addEventListener('click', async () => {
      if (busy) return;
      busy = true;
      setStatus('Activating…');
      try {
        await invoke('control_hrir_source', { value: `sofa:${f.path}` });
        const src = el('binauralHrirSource');
        if (src) src.value = 'sofa';
        activeSofaPath = f.path;
        setStatus(`✓ Active: ${f.name}`);
        await showLocal(); // re-render to move the highlight
      } catch (e) {
        setStatus(String(e), true);
      } finally {
        busy = false;
      }
    });
    list.appendChild(row);
  }
  if (activeRow) activeRow.scrollIntoView({ block: 'nearest' });
}

async function importFromDisk() {
  if (busy) return;
  let picked;
  try {
    picked = await openFileDialog({
      multiple: true,
      filters: [{ name: 'SOFA', extensions: ['sofa'] }],
      title: 'Import SOFA files',
    });
  } catch (e) {
    setStatus(String(e), true);
    return;
  }
  if (!picked) return;
  const paths = Array.isArray(picked) ? picked : [picked];
  busy = true;
  let done = 0;
  try {
    for (const src of paths) {
      setStatus(`Importing ${src.split('/').pop()}…`);
      await invoke('sofa_import_local', { src });
      done += 1;
    }
    setStatus(`✓ Imported ${done} file${done > 1 ? 's' : ''}.`);
  } catch (e) {
    setStatus(String(e), true);
  } finally {
    busy = false;
  }
  showLocal();
}

function confirmDeleteAll(files) {
  const list = el('sofaBrowserList');
  if (!list) return;
  list.textContent = '';
  const pane = document.createElement('div');
  pane.style.cssText = 'display:grid;gap:0.5rem;padding:0.5rem;';
  const msg = document.createElement('div');
  msg.style.cssText = 'font-size:0.78rem;color:#c9d6e2;';
  msg.textContent = `Delete all ${files.length} cached SOFA files? The currently active HRTF stays loaded in the renderer until you switch.`;
  const actions = document.createElement('div');
  actions.style.cssText = 'display:flex;gap:0.5rem;justify-content:flex-end;';
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'ui-btn';
  cancel.textContent = 'Cancel';
  cancel.addEventListener('click', () => showLocal());
  const go = document.createElement('button');
  go.type = 'button';
  go.className = 'ui-btn';
  go.style.cssText = 'border-color:rgba(255,120,120,0.6);color:#ff9a9a;';
  go.textContent = 'Delete all';
  go.addEventListener('click', async () => {
    try {
      await invoke('sofa_delete_local', { paths: files.map((f) => f.path) });
      setStatus(`Deleted ${files.length} files.`);
    } catch (e) {
      setStatus(String(e), true);
    }
    showLocal();
  });
  actions.append(cancel, go);
  pane.append(msg, actions);
  list.appendChild(pane);
}

function showOnlineConfirm() {
  setChrome();
  const toggle = el('sofaSourceToggleBtn');
  if (toggle) toggle.style.display = 'none';
  const list = el('sofaBrowserList');
  if (!list) return;
  list.textContent = '';
  const pane = document.createElement('div');
  pane.style.cssText = 'display:grid;gap:0.5rem;padding:0.5rem;';
  const msg = document.createElement('div');
  msg.style.cssText = 'font-size:0.78rem;color:#c9d6e2;line-height:1.4;';
  msg.textContent =
    'This will connect to sofacoustics.org (the public SOFA conventions '
    + 'database) to browse and download HRTF files over the internet. '
    + 'Nothing is sent besides the directory and file requests.';
  const actions = document.createElement('div');
  actions.style.cssText = 'display:flex;gap:0.5rem;justify-content:flex-end;';
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'ui-btn';
  cancel.textContent = 'Cancel';
  cancel.addEventListener('click', () => showLocal());
  const go = document.createElement('button');
  go.type = 'button';
  go.className = 'ui-btn ui-btn-primary';
  go.textContent = 'Connect';
  go.addEventListener('click', () => {
    onlineConsent = true;
    mode = 'remote';
    setChrome();
    navigate(currentPath);
  });
  actions.append(cancel, go);
  pane.append(msg, actions);
  list.appendChild(pane);
}

async function navigate(path) {
  if (busy) return;
  busy = true;
  currentPath = path;
  renderCrumbs();
  setStatus('Loading…');
  try {
    const entries = await invoke('sofa_browse', { path });
    renderList(entries);
    setStatus('');
  } catch (e) {
    setStatus(String(e), true);
  } finally {
    busy = false;
  }
}

async function downloadAndActivate(entry) {
  if (busy) return;
  busy = true;
  setStatus(`Downloading ${entry.name} (${entry.size})…`);
  setDownloadUiVisible(true);
  try {
    const remotePath = currentPath + entry.href;
    const localPath = await invoke('sofa_download', { path: remotePath });
    setStatus('Activating…');
    await invoke('control_hrir_source', { value: `sofa:${localPath}` });
    // Reflect the selection locally; the state broadcast will confirm it.
    const src = el('binauralHrirSource');
    if (src) src.value = 'sofa';
    activeRemotePath = remotePath;
    activeSofaPath = localPath;
    try {
      const meta = await invoke('sofa_file_meta', { path: localPath });
      const lic = licenseLabel(meta && meta.license);
      setStatus(`✓ Active: ${entry.name} — ⚖ ${lic.label}`, lic.warn);
    } catch {
      setStatus(`✓ Active: ${entry.name}`);
    }
    // Re-render so the previous highlight moves to the new file.
    const listEl = el('sofaBrowserList');
    if (listEl) {
      for (const row of listEl.children) {
        const p = row.dataset ? row.dataset.sofaPath : '';
        const on = p && p === activeRemotePath;
        row.style.background = on ? 'rgba(86,156,255,0.22)' : '';
        row.style.boxShadow = on ? 'inset 2px 0 0 #569cff' : '';
        const nameEl = row.firstChild;
        if (nameEl && nameEl.textContent && nameEl.textContent.startsWith('🎧')) {
          nameEl.textContent = nameEl.textContent.replace('  ✓ active', '');
          if (on) nameEl.textContent += '  ✓ active';
          nameEl.style.color = on ? '#9cc4ff' : '';
        }
      }
    }
  } catch (e) {
    const msg = String(e);
    setStatus(msg.includes('cancelled') ? 'Download cancelled.' : msg, !msg.includes('cancelled'));
  } finally {
    setDownloadUiVisible(false);
    busy = false;
  }
}

export function initSofaBrowser() {
  const btn = el('sofaBrowseBtn');
  const modal = el('sofaBrowserModal');
  const close = el('sofaBrowserClose');
  if (!btn || !modal) return;
  btn.addEventListener('click', () => {
    modal.classList.add('open');
    showLocal();
  });
  const sourceToggle = el('sofaSourceToggleBtn');
  if (sourceToggle) {
    sourceToggle.addEventListener('click', () => {
      if (busy) return;
      if (mode === 'remote') {
        showLocal();
      } else if (onlineConsent) {
        mode = 'remote';
        setChrome();
        navigate(currentPath);
      } else {
        showOnlineConfirm();
      }
    });
  }
  if (close) {
    close.addEventListener('click', () => modal.classList.remove('open'));
  }
  modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.classList.remove('open');
  });
  const cancel = el('sofaDlCancel');
  if (cancel) {
    // Deliberately not gated by `busy`: cancelling is only meaningful while
    // a download is in flight.
    cancel.addEventListener('click', () => {
      invoke('sofa_download_cancel').catch((e) => console.error('[sofa] cancel', e));
    });
  }
  listen('sofa:download_progress', ({ payload }) => {
    if (payload && typeof payload === 'object') onDownloadProgress(payload);
  });
}

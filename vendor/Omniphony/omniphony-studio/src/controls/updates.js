// Opt-in update check. When enabled (off by default), Studio queries the GitHub
// releases API at most once per day, at startup, and shows a blue banner with a
// link to the newer release if one is available.
//
// Frontend-only by design: the webview runs with `csp: null` and GitHub's REST
// API answers with `Access-Control-Allow-Origin: *`, so a plain `fetch()` works
// without any Rust command or new dependency. The toggle state and the
// once-per-day throttle persist in localStorage, mirroring the pattern used by
// diag-plot.js / room-geometry.js.

import { t, tf } from '../i18n.js';
import { pushLog, normalizeLogError } from '../log.js';

const RELEASES_URL = 'https://api.github.com/repos/mgth/Omniphony/releases';
const RELEASES_PAGE = 'https://github.com/mgth/Omniphony/releases';
const CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000; // 1 day
// Only official `vMAJOR.MINOR.PATCH` tags count: excludes the `v0.x.y.nnn`
// dev/polish tags and the `liborender-v*` component tags.
const OFFICIAL_TAG_RE = /^v(\d+)\.(\d+)\.(\d+)$/;

const ENABLED_KEY = 'omniphony.updateCheck.enabled';
const LAST_CHECK_KEY = 'omniphony.updateCheck.lastCheck';
const RESULT_KEY = 'omniphony.updateCheck.result';

let toggleEl = null;
let bannerEl = null;
let bannerTextEl = null;
let bannerLinkEl = null;
let currentVersion = '';

function isEnabled() {
  return localStorage.getItem(ENABLED_KEY) === '1';
}

function setEnabled(enabled) {
  localStorage.setItem(ENABLED_KEY, enabled ? '1' : '0');
}

function loadCachedResult() {
  try {
    const raw = localStorage.getItem(RESULT_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function storeResult(result) {
  if (result) localStorage.setItem(RESULT_KEY, JSON.stringify(result));
  else localStorage.removeItem(RESULT_KEY);
}

// Parse `vX.Y.Z` (or `X.Y.Z`) into [major, minor, patch]; null if not official.
function parseVersion(tag) {
  if (typeof tag !== 'string') return null;
  const m = tag.trim().match(OFFICIAL_TAG_RE);
  if (!m) return null;
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

// Returns >0 if a is newer than b, <0 if older, 0 if equal.
function compareVersions(a, b) {
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return 0;
}

function hideBanner() {
  if (bannerEl) bannerEl.style.display = 'none';
}

function renderBanner(result) {
  if (!bannerEl || !result || !result.tag) {
    hideBanner();
    return;
  }
  const cur = parseVersion(currentVersion);
  const next = parseVersion(result.tag);
  // Only show if we can confirm the cached/fetched release is strictly newer.
  if (!cur || !next || compareVersions(next, cur) <= 0) {
    hideBanner();
    return;
  }
  if (bannerTextEl) bannerTextEl.textContent = tf('updates.available', { version: result.tag });
  if (bannerLinkEl) {
    bannerLinkEl.textContent = t('updates.linkText');
    bannerLinkEl.href = result.htmlUrl || RELEASES_PAGE;
  }
  bannerEl.style.display = '';
}

// Pick the highest official, non-draft, non-prerelease release from the API list.
function pickLatestOfficial(releases) {
  if (!Array.isArray(releases)) return null;
  let best = null;
  let bestVer = null;
  for (const rel of releases) {
    if (!rel || rel.draft || rel.prerelease) continue;
    const ver = parseVersion(rel.tag_name);
    if (!ver) continue;
    if (!bestVer || compareVersions(ver, bestVer) > 0) {
      bestVer = ver;
      best = { tag: rel.tag_name, htmlUrl: rel.html_url || RELEASES_PAGE };
    }
  }
  return best;
}

async function fetchAndRender() {
  try {
    const resp = await fetch(RELEASES_URL, { headers: { Accept: 'application/vnd.github+json' } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const releases = await resp.json();
    const result = pickLatestOfficial(releases);
    localStorage.setItem(LAST_CHECK_KEY, String(Date.now()));
    storeResult(result);
    renderBanner(result);
  } catch (e) {
    // Network/parse failures are non-fatal: log and show nothing.
    pushLog('warn', tf('updates.checkFailed', { error: normalizeLogError(e) }));
  }
}

// Run a check if enabled, respecting the once-per-day throttle. Within the day,
// render from the cached result so the banner survives restarts without refetch.
function maybeCheck(version) {
  if (typeof version === 'string') currentVersion = version;
  if (!isEnabled()) {
    hideBanner();
    return;
  }
  const last = Number(localStorage.getItem(LAST_CHECK_KEY) || 0);
  if (Number.isFinite(last) && Date.now() - last < CHECK_INTERVAL_MS) {
    renderBanner(loadCachedResult());
    return;
  }
  fetchAndRender();
}

// Bind the toggle and banner DOM. Called once at boot, before the version is
// known; `maybeCheck(version)` is invoked later from the get_about_info handler.
export function initUpdateCheck() {
  toggleEl = document.getElementById('updateCheckToggle');
  bannerEl = document.getElementById('updateAvailableBanner');
  bannerTextEl = document.getElementById('updateAvailableText');
  bannerLinkEl = document.getElementById('updateAvailableLink');

  if (toggleEl) {
    toggleEl.checked = isEnabled();
    toggleEl.addEventListener('change', () => {
      setEnabled(toggleEl.checked);
      if (toggleEl.checked) maybeCheck(currentVersion);
      else hideBanner();
    });
  }
  hideBanner();
}

export { maybeCheck };

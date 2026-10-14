import type { ProfileApplyResponse } from './api-client.ts';
const DISPLAY_OPTIONS = new Set([
  'vo','drm-device','drm-connector','drm-mode','gpu-context','gpu-api',
  'drm-format','target-colorspace-hint','target-colorspace-hint-mode','target-trc','target-prim',
]);
// Exact render-option readback verifies configuration, not physical HDR output or signaling.
export function videoReceiptState(receipt: ProfileApplyResponse, profileId: string, route?: string | null): 'audioApplied' | 'audioPending' | 'audioDraft' {
  if (!receipt.accepted || !receipt.profile_ids.includes(profileId)) return 'audioDraft';
  const expected = Object.entries(receipt.effective_options).filter(([key]) => DISPLAY_OPTIONS.has(key));
  if (!expected.length) return 'audioDraft';
  if (route) {
    const match = /^(\/dev\/dri\/card\d+) \/ ([A-Za-z]+(?:-[A-Za-z]+)?-\d+)$/.exec(route);
    if (!match || receipt.effective_options['drm-device'] !== match[1] || receipt.effective_options['drm-connector'] !== match[2]) return 'audioPending';
  }
  return expected.every(([key,value]) => receipt.observed_options[key] === value && !(key in receipt.pending_restart_options)) ? 'audioApplied' : 'audioPending';
}

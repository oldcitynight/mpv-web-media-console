import { displaySignalReadbackIsConsistent, type DisplaySignalStatus } from './api-client.ts';
import { translate, type AppLocale, type MessageKey } from './i18n.ts';

const signalReasonMessages: Partial<Record<NonNullable<DisplaySignalStatus['display_signal_reason']>, MessageKey>> = {
  no_current_frame: 'signalNoFrame',
  unsupported_renderer: 'signalUnsupportedRenderer',
  unbound_route: 'signalUnboundRoute',
  permission_denied: 'signalReadbackUnavailable',
  unavailable: 'signalReadbackUnavailable',
  invalid_readback: 'signalInvalidReadback',
  connector_disconnected: 'signalDisconnected',
  inactive_scanout: 'signalInactiveScanout',
  missing_connector_properties: 'signalMissingProperties',
  malformed_metadata: 'signalInvalidReadback',
  unknown_scanout_format: 'signalUnknownScanout',
  conflicting_evidence: 'signalConflictingEvidence',
  stale_observation: 'signalRefreshing',
  epoch_invalidated: 'signalRefreshing',
  connector_changed: 'signalRefreshing',
};

/** Describes current kernel observations, never what the TV has received. */
export function displaySignalPresentation(status: DisplaySignalStatus | null | undefined, locale: AppLocale): {
  label: string; reason: string | null; scanout: string | null;
} {
  if (status && displaySignalReadbackIsConsistent(status)) {
    const label = status.display_signal_state === 'hdr10' ? 'signalObservedHdr10'
      : status.display_signal_state === 'hlg' ? 'signalObservedHlg' : 'signalObservedSdr';
    return {label: translate(locale, label), reason: null,
      scanout: translate(locale, 'scanoutDepth', {depth: status.scanout_bit_depth!})};
  }
  const positiveClaim = status?.display_signal_state != null && status.display_signal_state !== 'unknown';
  const verifiedClaim = status?.display_signal_reason?.startsWith('verified_') === true;
  const reason = positiveClaim || verifiedClaim ? 'signalConflictingEvidence'
    : status?.display_signal_reason && Object.hasOwn(signalReasonMessages, status.display_signal_reason)
      ? signalReasonMessages[status.display_signal_reason] : null;
  return {label: translate(locale, 'signalUnknown'), reason: reason ? translate(locale, reason) : null, scanout: null};
}

export function resolutionLabel(width: number | null | undefined, height: number | null | undefined): string | null {
  if (!width || !height || !Number.isFinite(width) || !Number.isFinite(height)) return null;
  if ((width === 3840 || width === 4096) && height === 2160) return '4K';
  if (width === 2560 && height === 1440) return '2K (1440p)';
  if (width === 1920 && height === 1080) return '1080p';
  if (width === 1280 && height === 720) return '720p';
  if (width === 7680 && height === 4320) return '8K';
  return `${width} × ${height}`;
}
export function videoSpecification(width: number | null | undefined, height: number | null | undefined, fps: number | null | undefined, locale: AppLocale = "en"): string | null {
  const size = resolutionLabel(width, height);
  const rate = fps != null && Number.isFinite(fps) && fps > 0 ? `${Number(fps.toFixed(3))} ${translate(locale, "framesPerSecond")}` : null;
  return [size, rate].filter(Boolean).join(' / ') || null;
}
export function layoutLabel(value: string | null | undefined, locale: AppLocale): string {
  const key = value?.trim().toLowerCase();
  if (['stereo', 'stereo 2.0', '2.0', 'fl-fr', 'fl,fr'].includes(key ?? '')) return translate(locale, 'layoutStereo');
  if (['mono', '1.0', 'fc'].includes(key ?? '')) return translate(locale, 'layoutMono');
  const recognized = /^(?:surround\s+)?(2\.1|4\.0|4\.1|5\.0|5\.1|6\.1|7\.1|5\.1\.2|5\.1\.4|7\.1\.2|7\.1\.4)(?:\((?:side|back)\))?$/.exec(key ?? '');
  return recognized ? translate(locale, 'layoutSurround', {layout: recognized[1]}) : translate(locale, 'layoutUnknown');
}
export function audioFormatLabel(format: string | null | undefined, profile: string | null | undefined, codec: string | null | undefined): string | null {
  const combined = [format, profile, codec].filter(Boolean).join(' ').toLowerCase();
  if (/dts[ :+-]*x\b/.test(combined)) return 'DTS:X';
  if (/atmos/.test(combined)) return 'Dolby Atmos';
  if (/auro/.test(combined)) return 'Auro-3D';
  if (/dts.*(?:hd.*ma|master)/.test(combined)) return 'DTS-HD MA';
  if (/dts.*hd/.test(combined)) return 'DTS-HD';
  if (/dts/.test(combined)) return 'DTS';
  if (/truehd/.test(combined)) return 'Dolby TrueHD';
  if (/e-?ac-?3|digital plus/.test(combined)) return 'Dolby Digital Plus';
  if (/ac-?3|dolby digital/.test(combined)) return 'Dolby Digital';
  for (const name of ['AAC','FLAC','Opus','Vorbis','MP3','PCM']) if (combined.includes(name.toLowerCase())) return name;
  return null;
}
export function sourceRangeLabel(value: string | null | undefined, locale: AppLocale): string {
  const ranges = ['SDR', 'HDR10', 'HDR10+', 'Dolby Vision', 'HLG'];
  return ranges.includes(value ?? '') ? value! : translate(locale, 'unknownStatus');
}
export function rendererTargetLabel(value: string | null | undefined, locale: AppLocale): string {
  return ['SDR', 'HDR10', 'HLG'].includes(value ?? '') ? value! : translate(locale, 'unknownStatus');
}
export function sinkLabel(name: string | null | undefined, route: string | null | undefined, locale: AppLocale): string {
  const backend = route?.match(/^(alsa|pulse|pipewire|drm|wayland|x11|gpu-next|gpu)(?:[/:]|$)/i)?.[1];
  // Legacy raw routes are not hardware identity. No dropdown draft values are used.
  const friendly = name && !/^(?:alsa|pulse|pipewire|drm|gpu)(?:[/:]|$)/i.test(name) ? name : null;
  return [friendly ?? translate(locale, 'outputIdentityUnknown'), backend?.toUpperCase()].filter(Boolean).join(' · ');
}

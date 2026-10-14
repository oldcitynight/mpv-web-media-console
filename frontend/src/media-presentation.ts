import type { MediaBrowserItem, MediaPresentation } from "./api-client.ts";
import { translate, type AppLocale } from "./i18n.ts";

export const UNKNOWN_PRESENTATION = "Unknown";

const HDR_LABELS = new Set(["Unknown", "SDR", "HLG", "HDR10", "HDR10+", "Dolby Vision"]);
const COMPATIBILITY_LABELS = new Set(["HDR10"]);
const AUDIO_LABELS = new Set([
  "Unknown",
  "AAC",
  "FLAC",
  "Opus",
  "Vorbis",
  "MP3",
  "MPEG Audio",
  "PCM",
  "Dolby Digital",
  "Dolby Digital Plus",
  "Dolby TrueHD",
  "Dolby Atmos",
  "DTS",
  "DTS-HD",
  "DTS-HD MA",
  "DTS:X",
]);
const VIDEO_LABELS = new Set([
  "Unknown",
  "H.264",
  "HEVC",
  "AV1",
  "VP9",
  "VP8",
  "MPEG-2",
  "MPEG-4",
  "VC-1",
]);
const SUBTITLE_LABELS = new Set(["SRT", "ASS", "SSA", "WebVTT", "Timed text", "DVD subtitles", "PGS"]);

function canonicalLabel(value: unknown, allowed: ReadonlySet<string>): string {
  return typeof value === "string" && allowed.has(value) ? value : UNKNOWN_PRESENTATION;
}

export function normalizePresentation(value: Partial<MediaPresentation> | null | undefined): MediaPresentation {
  const audio = Array.isArray(value?.audio_formats)
    ? [...new Set(value.audio_formats.map((item) => canonicalLabel(item, AUDIO_LABELS)))]
    : [];
  const compatibility = Array.isArray(value?.hdr_compatibility)
    ? [...new Set(value.hdr_compatibility.map((item) => canonicalLabel(item, COMPATIBILITY_LABELS)))]
        .filter((item) => item !== UNKNOWN_PRESENTATION)
    : [];
  return {
    hdr_format: canonicalLabel(value?.hdr_format, HDR_LABELS),
    hdr_compatibility: compatibility,
    audio_formats: audio.length ? audio : [UNKNOWN_PRESENTATION],
    video_codec: canonicalLabel(value?.video_codec, VIDEO_LABELS),
  };
}

export function trackCodecLabel(codec: string | null | undefined, locale: AppLocale = "en"): string {
  if (typeof codec !== "string" || !codec.trim() || codec === UNKNOWN_PRESENTATION) return translate(locale, "unknownStatus");
  if (codec === "Timed text") return translate(locale, "subtitleTimedText");
  if (codec === "DVD subtitles") return translate(locale, "subtitleDvd");
  if (AUDIO_LABELS.has(codec) || VIDEO_LABELS.has(codec) || SUBTITLE_LABELS.has(codec)) return codec;
  const components = codec.split(" + ");
  return components.length > 1 && components.every((component) => AUDIO_LABELS.has(component))
    ? components.join(" + ")
    : translate(locale, "unknownStatus");
}

export function presentationSummary(value: Partial<MediaPresentation> | null | undefined, locale: AppLocale = "en"): string {
  const presentation = normalizePresentation(value);
  const parts = [presentation.video_codec];
  if (presentation.hdr_format !== UNKNOWN_PRESENTATION) parts.push(presentation.hdr_format);
  parts.push(...presentation.hdr_compatibility);
  parts.push(...presentation.audio_formats);
  const visible = parts.filter((part, index) => part !== UNKNOWN_PRESENTATION || index === parts.length - 1);
  return [...new Set(visible)].map(part => part === UNKNOWN_PRESENTATION ? translate(locale, "unknownStatus") : part).join(" · ") || translate(locale, "unknownStatus");
}

export function previewKindLabel(item: Pick<MediaBrowserItem, "preview_kind">, locale: AppLocale = "en"): string {
  switch (item.preview_kind) {
    case "embedded_art":
      return translate(locale, "mediaPreviewEmbeddedArt");
    case "sibling_art":
      return translate(locale, "mediaPreviewSiblingArt");
    case "video_frame":
      return translate(locale, "mediaPreviewVideoFrame");
    case "placeholder":
      return translate(locale, "mediaPreviewPlaceholder");
    default:
      return translate(locale, "unknownStatus");
  }
}

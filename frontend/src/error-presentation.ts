import type { ApiError } from "./api-client.ts";
import { translate, type AppLocale, type MessageKey } from "./i18n.ts";

export function apiErrorMessageKey(error: Pick<ApiError, "status" | "code">): MessageKey {
  if (error.status === 401) return "connectionTokenError";
  const keys: Record<string, MessageKey> = {
    invalid_request: "apiInvalidRequest",
    forbidden_media: "apiForbiddenMedia",
    not_found: "apiNotFound",
    conflict: "apiConflict",
    player_unavailable: "apiPlayerUnavailable",
    internal_error: "apiInternalError",
  };
  return Object.hasOwn(keys, error.code) ? keys[error.code] : "apiInternalError";
}

export function connectionErrorMessage(error: string | null, locale: AppLocale): string | null {
  if (error === null) return null;
  const keys: Record<string, MessageKey> = {
    token_whitespace: "tokenWhitespaceError",
    connection_start_failed: "connectionStartError",
    websocket_unavailable: "connectionLostError",
    websocket_transport_error: "connectionLostError",
    websocket_authentication_failed: "connectionTokenError",
    invalid_event: "connectionRefreshing",
    snapshot_required: "connectionRefreshing",
    event_sequence_gap: "connectionRefreshing",
    event_state_missing: "connectionRefreshing",
  };
  return translate(locale, Object.hasOwn(keys, error) ? keys[error] : "connectionStartError");
}

export function queueErrorMessage(key: string, locale: AppLocale): string {
  const keys: ReadonlySet<MessageKey> = new Set([
    "apiInvalidRequest", "apiForbiddenMedia", "apiNotFound", "apiConflict",
    "apiPlayerUnavailable", "apiInternalError", "connectionTokenError",
    "mediaQueueRequestFailed", "mediaFolderQueueUnavailable", "mediaQueueUncertainItem",
  ]);
  return translate(locale, keys.has(key as MessageKey) ? key as MessageKey : "mediaQueueRequestFailed");
}

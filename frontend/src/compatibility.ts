export interface FrontendBuildInfo {
  schema_version: 1;
  client_protocol_version: number;
  build_id: string;
}

export type BootstrapOutcome = "mounted" | "reloading" | "blocked";

export interface BootstrapDependencies {
  expectedProtocolVersion: number;
  fetcher: typeof fetch;
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem">;
  reload: () => void;
  mount: () => void;
  showBlocked: () => void;
}

const RELOAD_GUARD_KEY = "mpv-web-suite:protocol-reload";
const BUILD_ID_PATTERN = /^[a-f0-9]{64}$/;

function isBuildInfo(value: unknown): value is FrontendBuildInfo {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  return (
    candidate.schema_version === 1 &&
    Number.isSafeInteger(candidate.client_protocol_version) &&
    (candidate.client_protocol_version as number) > 0 &&
    typeof candidate.build_id === "string" &&
    BUILD_ID_PATTERN.test(candidate.build_id)
  );
}

export async function bootstrapCompatibleClient(
  dependencies: BootstrapDependencies,
): Promise<BootstrapOutcome> {
  const {
    expectedProtocolVersion,
    fetcher,
    storage,
    reload,
    mount,
    showBlocked,
  } = dependencies;

  const block = (): BootstrapOutcome => {
    showBlocked();
    return "blocked";
  };

  try {
    if (!Number.isSafeInteger(expectedProtocolVersion) || expectedProtocolVersion < 1) {
      return block();
    }
    const response = await fetcher("/_ui/version", {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
    });
    if (!response.ok) return block();

    const value: unknown = await response.json();
    if (!isBuildInfo(value)) return block();

    if (value.client_protocol_version === expectedProtocolVersion) {
      storage.removeItem(RELOAD_GUARD_KEY);
      mount();
      return "mounted";
    }

    const mismatch = `${expectedProtocolVersion}:${value.client_protocol_version}`;
    if (storage.getItem(RELOAD_GUARD_KEY) === mismatch) return block();

    storage.setItem(RELOAD_GUARD_KEY, mismatch);
    reload();
    return "reloading";
  } catch {
    return block();
  }
}

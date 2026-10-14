import { createApp } from "vue";
import App from "./App.vue";
import { bootstrapCompatibleClient } from "./compatibility";
import { resolveLocale, translate } from "./i18n";
import "./style.css";

function renderCompatibilityBlocked(): void {
  const root = document.querySelector("#app");
  if (!root) return;

  let savedLocale: string | null = null;
  try {
    savedLocale = window.localStorage.getItem("mpv-web-locale");
  } catch {
    // Language selection also works when browser storage is disabled.
  }
  const browserLocales = navigator.languages?.length ? navigator.languages : [navigator.language];
  const locale = resolveLocale(savedLocale, browserLocales);
  document.documentElement.lang = locale;
  document.querySelector('meta[name="description"]')?.setAttribute("content", translate(locale, "appDescription"));
  const view = document.createElement("main");
  view.className = "compatibility-gate";
  view.setAttribute("role", "alert");
  view.setAttribute("aria-live", "assertive");

  const heading = document.createElement("h1");
  heading.textContent = translate(locale, "compatibilityBlockedTitle");
  const message = document.createElement("p");
  message.textContent = translate(locale, "compatibilityBlockedMessage");
  const retry = document.createElement("button");
  retry.type = "button";
  retry.textContent = translate(locale, "compatibilityRetry");
  retry.addEventListener("click", () => window.location.reload());

  view.append(heading, message, retry);
  root.replaceChildren(view);
}

let browserSessionStorage: Storage | undefined;
try {
  browserSessionStorage = window.sessionStorage;
} catch {
  renderCompatibilityBlocked();
}

if (browserSessionStorage) {
  void bootstrapCompatibleClient({
    expectedProtocolVersion: __MPV_CLIENT_PROTOCOL_VERSION__,
    fetcher: globalThis.fetch.bind(globalThis),
    storage: browserSessionStorage,
    reload: () => window.location.reload(),
    mount: () => createApp(App).mount("#app"),
    showBlocked: renderCompatibilityBlocked,
  });
}

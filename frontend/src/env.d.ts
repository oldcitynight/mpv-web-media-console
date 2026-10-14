/// <reference types="vite/client" />

declare const __MPV_CLIENT_PROTOCOL_VERSION__: number;

declare module "*.vue" {
  import type { DefineComponent } from "vue";

  const component: DefineComponent<object, object, unknown>;
  export default component;
}

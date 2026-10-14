import { createHash } from "node:crypto";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import type { Plugin } from "vite";
import vue from "@vitejs/plugin-vue";

const protocolFile = fileURLToPath(new URL("./src/client-protocol.json", import.meta.url));
const protocolConfig: unknown = JSON.parse(readFileSync(protocolFile, "utf-8"));
const clientProtocolVersion =
  typeof protocolConfig === "object" && protocolConfig !== null
    ? (protocolConfig as Record<string, unknown>).version
    : undefined;
if (!Number.isSafeInteger(clientProtocolVersion) || (clientProtocolVersion as number) < 1) {
  throw new Error("frontend client protocol version must be a positive safe integer");
}

const apiProxy = {
  "/api/v1": {
    target: process.env.MPV_API_ORIGIN ?? "http://127.0.0.1:8765",
    ws: true,
  },
};

function deterministicBuildMetadata(): Plugin {
  return {
    name: "mpv-web-suite-build-metadata",
    apply: "build",
    writeBundle(outputOptions) {
      const configRoot = dirname(fileURLToPath(import.meta.url));
      const outputRoot = resolve(configRoot, outputOptions.dir ?? "dist");
      const files: string[] = [];
      const walk = (directory: string): void => {
        for (const entry of readdirSync(directory, { withFileTypes: true })) {
          const path = join(directory, entry.name);
          if (entry.isDirectory()) walk(path);
          else if (entry.isFile()) {
            const fileName = relative(outputRoot, path).split("\\").join("/");
            if (fileName !== "assets/build-info.json") files.push(fileName);
          }
        }
      };
      walk(outputRoot);
      files.sort((left, right) => (left < right ? -1 : left > right ? 1 : 0));

      const digest = createHash("sha256");
      for (const fileName of files) {
        const contentHash = createHash("sha256")
          .update(readFileSync(join(outputRoot, fileName)))
          .digest("hex");
        digest.update(fileName, "utf-8");
        digest.update("\0", "utf-8");
        digest.update(contentHash, "ascii");
        digest.update("\n", "utf-8");
      }

      writeFileSync(
        join(outputRoot, "assets", "build-info.json"),
        `${JSON.stringify(
          {
            schema_version: 1,
            client_protocol_version: clientProtocolVersion,
            build_id: digest.digest("hex"),
          },
          null,
          2,
        )}\n`,
        "utf-8",
      );
    },
  };
}

export default defineConfig({
  define: {
    __MPV_CLIENT_PROTOCOL_VERSION__: JSON.stringify(clientProtocolVersion),
  },
  plugins: [vue(), deterministicBuildMetadata()],
  server: {
    host: "127.0.0.1",
    proxy: {
      ...apiProxy,
      "/_ui/version": {
        target: process.env.MPV_API_ORIGIN ?? "http://127.0.0.1:8765",
      },
    },
  },
  preview: {
    host: "127.0.0.1",
    proxy: {
      ...apiProxy,
      "/_ui/version": {
        target: process.env.MPV_API_ORIGIN ?? "http://127.0.0.1:8765",
      },
    },
  },
});

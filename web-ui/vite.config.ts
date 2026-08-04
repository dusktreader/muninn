import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const apiProxy = {
  "/v1": {
    target: process.env.MUNINN_API_PROXY_TARGET ?? "http://127.0.0.1:5000",
    timeout: 300_000,
    proxyTimeout: 300_000,
  },
  "/healthz": process.env.MUNINN_API_PROXY_TARGET ?? "http://127.0.0.1:5000",
  "/readyz": process.env.MUNINN_API_PROXY_TARGET ?? "http://127.0.0.1:5000",
};

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  resolve: {
    alias: mode === "test" ? [{
      find: "./auth-session",
      replacement: fileURLToPath(new URL("./src/test-auth-session.ts", import.meta.url)),
    }] : [],
  },
  server: {
    proxy: apiProxy,
  },
  preview: {
    proxy: apiProxy,
  },
}));

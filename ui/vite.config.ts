import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The build goes into the Python package, which serves it (nanio_orchestrator/web/spa.py).
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../nanio_orchestrator/web/dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    // `make run` (or the development stack) serves the API on 8080
    proxy: { "/api": "http://localhost:8080" },
  },
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});

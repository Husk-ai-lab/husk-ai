import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { defineConfig, loadEnv } from "vite";

// In dev the studio talks to the local Husk backend. The backend's default
// port is 7654 but `husk-studio start` will fall back to the next free port if 7654
// is taken — so we let the dev shell override it via VITE_BACKEND_PORT.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backendPort = Number(env.VITE_BACKEND_PORT || "7654");
  const backendUrl = `http://localhost:${backendPort}`;
  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        "@": path.resolve(import.meta.dirname, "client", "src"),
      },
    },
    root: path.resolve(import.meta.dirname, "client"),
    build: {
      outDir: path.resolve(import.meta.dirname, "dist"),
      emptyOutDir: true,
    },
    server: {
      port: 5174,
      strictPort: false,
      host: true,
      proxy: {
        "/api": backendUrl,
        "/ws": {
          target: `ws://localhost:${backendPort}`,
          ws: true,
          changeOrigin: true,
        },
      },
    },
  };
});

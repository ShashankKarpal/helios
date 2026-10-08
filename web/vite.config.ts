import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev proxy points same-origin /api calls at the local heliosd backend, which
// serves https only on 8420 (a self-signed local certificate, so the proxy
// does not verify it). The dev server has no shell token, so every API call
// still answers 401 until a token header is added by hand.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "https://localhost:8420",
        changeOrigin: true,
        secure: false,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    // A brand build must not leave superseded hashed bundles in dist: the
    // service worker can otherwise continue serving retired palette values.
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks: {
          echarts: ["echarts", "echarts-for-react"],
          react: ["react", "react-dom"],
        },
      },
    },
  },
});

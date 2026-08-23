import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev proxy points same-origin /api calls at the local heliosd backend.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8420",
        changeOrigin: true,
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

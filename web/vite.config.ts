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
    // The dev sandbox mount forbids unlinking synced files, so let Vite
    // overwrite in place rather than clearing the directory first. On a normal
    // filesystem this is harmless.
    emptyOutDir: false,
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

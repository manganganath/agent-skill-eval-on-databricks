import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output goes to the FastAPI static folder, so one process serves the UI and the API.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../backend/static", emptyOutDir: true },
  server: { proxy: { "/api": "http://localhost:8000" } },
  test: { environment: "jsdom", setupFiles: ["./src/test-setup.ts"], globals: true },
});

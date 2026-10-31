import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development, run the API on :8080 (uvicorn --factory vrixel_sdlc.api.app:create_app --port 8080)
// and `npm run dev`; /api is proxied so cookies stay same-origin.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": { target: "http://localhost:8080", changeOrigin: false } } },
  build: { outDir: "dist", sourcemap: false },
});

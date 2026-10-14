/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Production assets are served by FastAPI from src/reva/api/static.
// In development, /v1 and /health are proxied to the local API (uv run reva api).
const apiTarget = process.env.REVA_API_URL ?? 'http://127.0.0.1:8000';

export default defineConfig({
  base: '/',
  plugins: [react()],
  build: {
    outDir: '../src/reva/api/static',
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 1600,
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/v1': { target: apiTarget, changeOrigin: false },
      '/health': { target: apiTarget, changeOrigin: false },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    css: false,
  },
});

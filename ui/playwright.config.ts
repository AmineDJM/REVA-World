import { defineConfig, devices } from '@playwright/test';

/**
 * End-to-end smoke tests against a running REVA API that serves the built UI.
 * Skipped unless REVA_E2E_BASE_URL is set, e.g.
 *   REVA_E2E_BASE_URL=http://localhost:8000 REVA_E2E_USERNAME=admin REVA_E2E_PASSWORD=... \
 *   PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers npm run e2e
 * Browsers are not downloaded by this project; use a preinstalled Chromium.
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.REVA_E2E_BASE_URL,
    trace: 'off',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});

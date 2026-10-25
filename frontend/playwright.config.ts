import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  use: { baseURL: 'http://127.0.0.1:18742', channel: process.env.CI ? undefined : 'chrome', headless: true },
  webServer: { command: '../.venv/bin/python ../scripts/preview.py', url: 'http://127.0.0.1:18742/api/health', timeout: 30000, reuseExistingServer: !process.env.CI },
});

import { defineConfig, devices } from '@playwright/test'

/**
 * TracePilot 浏览器验收测试配置。
 *
 * 使用系统已安装的 Microsoft Edge（channel: 'msedge'），
 * 无需下载 Playwright 自带 Chromium。
 *
 * 前置条件：后端 (127.0.0.1:8000) 与前端 (localhost:5173) 已在运行。
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['json', { outputFile: 'e2e-results.json' }]],
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'off',
    screenshot: 'only-on-failure',
    video: 'off',
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
  },
  projects: [
    {
      name: 'desktop-edge',
      use: {
        ...devices['Desktop Chrome'],
        channel: 'msedge',
        viewport: { width: 1440, height: 900 },
      },
    },
  ],
})

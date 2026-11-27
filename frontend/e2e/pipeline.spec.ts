import { expect, test, type Page } from '@playwright/test'

/**
 * 第 52 步：Automated Evaluation Pipeline 浏览器验收。
 *
 * 覆盖：创建 → 启动 → 轮询进度 → 阶段流程图交互 → 阶段详情/跳过原因 →
 * 最终报告（可复现性/关联工件/证据入口）→ 质量门禁集成 → 历史流水线 →
 * 真实 API 未授权拒绝。
 *
 * 全部使用 Mock 执行模式（确定性、零成本、无付费 API）。
 */

function collectErrors(page: Page): string[] {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console.error: ${m.text()}`)
  })
  return errors
}

function serious(errors: string[]): string[] {
  return errors.filter(
    (e) => !e.includes('favicon') && !e.includes('net::ERR_') && !e.includes('status of 404'),
  )
}

async function openPipelinePage(page: Page) {
  await page.goto('/pipelines', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: /Evaluation Pipeline/ })).toBeVisible({
    timeout: 30_000,
  })
  const datasetSelect = page.locator('#pl-ds')
  await expect(datasetSelect).toBeVisible({ timeout: 30_000 })
  await expect
    .poll(async () => await datasetSelect.locator('option').count(), { timeout: 30_000 })
    .toBeGreaterThan(0)
  return datasetSelect
}

async function createAndRun(
  page: Page,
  options: { dataset: string; maxCases?: string; baseline?: string; judge?: 'none' | 'mock' },
) {
  const datasetSelect = await openPipelinePage(page)
  await datasetSelect.selectOption(options.dataset)
  if (options.maxCases) await page.locator('#pl-mc').fill(options.maxCases)
  if (options.judge === 'mock') await page.locator('#pl-judge').selectOption('mock')
  if (options.baseline) await page.locator('#pl-baseline').selectOption(options.baseline)

  await page.getByRole('button', { name: '创建流水线' }).click()
  // 创建成功：出现阶段流程图（PENDING 状态）
  await expect(page.locator('.pl-stage').first()).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText('PENDING', { exact: true }).first()).toBeVisible()
  await page.getByRole('button', { name: '启动流水线' }).click()

  // 等待最终报告出现（不把未完成运行报告为成功）
  await expect(page.getByText('可复现性')).toBeVisible({ timeout: 180_000 })
  await expect(page.locator('.pl-report-grid')).toBeVisible()
  return page.locator('.pl-stage')
}

test('流水线：创建 → 启动 → 阶段流程图 → 最终报告（Mock，无基线）', async ({ page }) => {
  const errors = collectErrors(page)
  const stages = await createAndRun(page, {
    dataset: 'tracepilot-eval-v1',
    maxCases: '3',
    judge: 'mock',
  })

  // 11 个阶段（含可选阶段，SKIPPED 不等于成功）
  await expect(stages).toHaveCount(11)
  const skipped = page.locator('.pl-stage.partial')
  await expect(skipped.first()).toBeVisible()

  // 点击「版本对比」阶段 → 右侧显示明确跳过原因
  await page.locator('.pl-stage', { hasText: '版本对比' }).click()
  await expect(page.locator('.pl-stage-detail')).toContainText('跳过原因')
  await expect(page.locator('.pl-stage-detail')).toContainText('未提供基线版本')

  // 最终报告：可复现性字段 + 评测摘要 + 质量门禁未执行说明
  await expect(page.getByText('pipeline_run_id').first()).toBeVisible()
  await expect(page.getByText('agent_version')).toBeVisible()
  // 可复现性来自真实配置指纹与执行模式（不是伪造的固定字段）
  await expect(page.locator('.pl-report-grid')).toContainText('execution_mode')
  await expect(page.locator('.pl-report-grid')).toContainText('random_seed')
  await expect(page.locator('.pl-report-grid')).toContainText('config_fingerprint')
  await expect(page.getByText('评测摘要（确定性）')).toBeVisible()
  await expect(page.getByText('质量门禁未执行（缺少基线或版本对比）')).toBeVisible()

  // 历史流水线列表出现本次运行
  await expect(page.locator('.pl-history tbody tr').first()).toBeVisible({ timeout: 30_000 })
  expect(serious(errors)).toEqual([])
})

test('流水线：提供基线 → 版本对比/回归/质量门禁全部执行', async ({ page }) => {
  // 先跑一条流水线，产生可供选择的基线评测运行
  await createAndRun(page, { dataset: 'tracepilot-eval-v1', maxCases: '3' })
  await page.reload({ waitUntil: 'domcontentloaded' })

  const datasetSelect = await openPipelinePage(page)
  await datasetSelect.selectOption('tracepilot-eval-v1')
  await page.locator('#pl-mc').fill('3')
  const baselineSelect = page.locator('#pl-baseline')
  await expect
    .poll(async () => await baselineSelect.locator('option').count(), { timeout: 30_000 })
    .toBeGreaterThan(1)
  const baselineValue = await baselineSelect
    .locator('option')
    .last()
    .getAttribute('value')
  expect(baselineValue).toBeTruthy()
  await baselineSelect.selectOption(baselineValue!)

  await page.getByRole('button', { name: '创建流水线' }).click()
  await expect(page.locator('.pl-stage').first()).toBeVisible({ timeout: 30_000 })
  await page.getByRole('button', { name: '启动流水线' }).click()
  await expect(page.getByText('可复现性')).toBeVisible({ timeout: 180_000 })

  // 质量门禁面板：PASS + 退出码 0
  const gatePanel = page.locator('.pl-panel', { hasText: '质量门禁' })
  await expect(gatePanel).toContainText('PASS')
  await expect(gatePanel).toContainText('退出码 0')

  // 阶段详情：质量门禁 SUCCEEDED，版本对比 SUCCEEDED
  await page.locator('.pl-stage', { hasText: '质量门禁' }).click()
  await expect(page.locator('.pl-stage-detail')).toContainText('SUCCEEDED')
  await page.locator('.pl-stage', { hasText: '版本对比' }).click()
  await expect(page.locator('.pl-stage-detail')).toContainText('SUCCEEDED')
  await expect(page.getByText('版本对比', { exact: true }).first()).toBeVisible()
})

test('流水线：real_api 未授权 → 明确错误且不创建运行', async ({ page }) => {
  const datasetSelect = await openPipelinePage(page)
  await datasetSelect.selectOption('tracepilot-eval-v1')
  await page.locator('#pl-mode').selectOption('real_api')
  await expect(page.getByText(/真实付费 API 调用/)).toBeVisible()
  await page.locator('.pl-check.pl-warn input[type="checkbox"]').check()
  await page.getByRole('button', { name: '创建流水线' }).click()
  await expect(page.locator('.errorbox')).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('.errorbox')).toContainText('未获授权')
  // 未创建流水线：没有阶段流程图
  await expect(page.locator('.pl-stage')).toHaveCount(0)
})

test('流水线：页面刷新后通过历史列表恢复运行状态', async ({ page }) => {
  await createAndRun(page, { dataset: 'tracepilot-eval-v1', maxCases: '2' })
  const pipelineId = await page
    .locator('.pl-progress .muted.small')
    .innerText()
    .then((text) => text.match(/Pipeline (pipe-[a-z0-9]+)/)?.[1] ?? '')
  expect(pipelineId).toBeTruthy()

  await page.reload({ waitUntil: 'domcontentloaded' })
  await openPipelinePage(page)
  const row = page.locator('.pl-history tbody tr', { hasText: pipelineId })
  await expect(row).toBeVisible({ timeout: 30_000 })
  await expect(row).toContainText('SUCCEEDED')
  await row.getByRole('button', { name: '查看' }).click()
  await expect(page.getByText('可复现性')).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('.pl-stage').first()).toBeVisible()
})

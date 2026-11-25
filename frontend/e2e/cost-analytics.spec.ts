import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

/**
 * 第 53 步：Token & Cost Analytics 浏览器验收。
 *
 * 覆盖：核心 Token/成本/延迟指标、成本口径声明（非实际账单）、模型分布、
 * 版本成本对比、Cost Attribution Explorer（model_call / evaluation / pipeline）、
 * 单次对象查询、UNKNOWN 与缺失数据、数据来源与价格版本。
 *
 * 全部为本地确定性数据：真实 API Trace（历史已验证 1 次）用于展示
 * PROVIDER_REPORTED + PRICE_UNKNOWN；评测/流水线为 SIMULATED 0 成本。
 */

const API = 'http://127.0.0.1:8000'
const REAL_TRACE_ID = 'trace-4d3d82dc7a67496dbc2842cd3b31aa47'

// 成本分析页首次加载需本地分析约 9.7k Trace（后端有单飞缓存），串行执行避免
// 多个 worker 同时触发冷启动重算与评测/流水线重复创建。
test.describe.configure({ mode: 'serial' })

let evaluationRunId = ''
let pipelineRunId = ''

async function waitEvaluation(request: APIRequestContext, runId: string) {
  await expect
    .poll(
      async () => {
        const r = await request.get(`${API}/api/evaluation/runs/${runId}`)
        return ((await r.json()) as { status: string }).status
      },
      { timeout: 120_000, intervals: [200, 500, 1000] },
    )
    .toBe('succeeded')
}

async function waitPipeline(request: APIRequestContext, runId: string) {
  await expect
    .poll(
      async () => {
        const r = await request.get(`${API}/api/pipelines/${runId}`)
        return ((await r.json()) as { status: string }).status
      },
      { timeout: 180_000, intervals: [250, 500, 1000] },
    )
    .toMatch(/SUCCEEDED|FAILED|BLOCKED|CANCELLED/)
}

test.beforeAll(async ({ request }) => {
  const created = await request.post(`${API}/api/evaluation/runs`, {
    data: { dataset_id: 'tracepilot-eval-v1', max_cases: 2, judge_mode: 'none', force_new: true },
  })
  expect(created.ok(), await created.text()).toBeTruthy()
  evaluationRunId = ((await created.json()) as { run_id: string }).run_id
  await waitEvaluation(request, evaluationRunId)

  const pipeline = await request.post(`${API}/api/pipelines`, {
    data: { dataset_id: 'tracepilot-eval-v1', max_cases: 2, label: 'cost-e2e' },
  })
  expect(pipeline.ok(), await pipeline.text()).toBeTruthy()
  pipelineRunId = ((await pipeline.json()) as { pipeline_run_id: string }).pipeline_run_id
  const started = await request.post(`${API}/api/pipelines/${pipelineRunId}/start`)
  expect(started.ok(), await started.text()).toBeTruthy()
  await waitPipeline(request, pipelineRunId)
})

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

async function openCosts(page: Page) {
  await page.goto('/costs', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: /Cost Analytics/ })).toBeVisible({
    timeout: 30_000,
  })
  await expect(page.locator('.ca-kpis')).toBeVisible({ timeout: 90_000 })
}

test('成本分析：核心指标 + 计费口径 + 数据来源与价格版本', async ({ page }) => {
  const errors = collectErrors(page)
  await openCosts(page)

  // 顶部指标
  await expect(page.locator('.ca-kpis .stat')).toHaveCount(5)
  await expect(page.locator('.ca-kpis').getByText('总 Token')).toBeVisible()
  await expect(page.locator('.ca-kpis').getByText('成本估算')).toBeVisible()
  await expect(page.locator('.ca-kpis').getByText('平均模型延迟')).toBeVisible()

  // 计费口径：非实际账单 + PRICE_UNKNOWN 展示
  const disclaimer = page.locator('.ca-disclaimer')
  await expect(disclaimer).toContainText('不是 Provider 实际账单')
  await expect(disclaimer).toContainText('PRICE_UNKNOWN')
  await expect(page.getByText('成本估算 ≠ 实际账单')).toBeVisible()

  // 价格版本 / 来源 / 用量来源
  await expect(page.getByText('价格版本', { exact: true })).toBeVisible()
  await expect(page.getByText('数据来源与价格版本')).toBeVisible()
  await expect(page.getByText('PROVIDER_REPORTED', { exact: false }).first()).toBeVisible()
  await expect(page.getByText('付费 API', { exact: true })).toBeVisible()

  // 模型分布与版本成本对比
  await expect(page.getByText('模型成本分布')).toBeVisible()
  await expect(page.locator('.ca-models tbody tr').first()).toBeVisible()
  await expect(page.getByText('版本成本变化（agent_version 缺失时按模型/执行模式分组）')).toBeVisible()
  await expect(page.getByText(/不能当作真实 Agent 版本对比/)).toBeVisible()

  expect(serious(errors)).toEqual([])
})

test('成本归因：model_call / evaluation / pipeline 维度', async ({ page }) => {
  await openCosts(page)

  const explorer = page.locator('.card', { hasText: 'Cost Attribution Explorer' })
  await expect(explorer.locator('.ca-attribution tbody tr').first()).toBeVisible({
    timeout: 60_000,
  })

  // 模型调用维度：默认按时间倒序（近期均为模拟评测数据）；用「用量来源」过滤出
  // Provider 报告用量的调用（真实 Trace + 测试产生的本地样本），验证状态为 PRICE_UNKNOWN。
  await page.getByLabel('用量来源').selectOption('PROVIDER_REPORTED')
  const providerRow = explorer.locator('.ca-attribution tbody tr').first()
  await expect(providerRow).toContainText('PROVIDER_REPORTED', { timeout: 60_000 })
  await expect(providerRow).toContainText('deepseek')
  await expect(providerRow).toContainText('PRICE_UNKNOWN')

  // 评测维度：包含刚创建的评测运行
  await page.locator('#ca-level').selectOption('evaluation')
  await expect(
    explorer.locator('.ca-attribution tbody tr', { hasText: evaluationRunId }),
  ).toBeVisible({ timeout: 60_000 })
  await expect(explorer.locator('.ca-attribution tbody tr').first()).toContainText('SIMULATED')

  // 流水线维度：包含刚运行的流水线
  await page.locator('#ca-level').selectOption('pipeline')
  await expect(
    explorer.locator('.ca-attribution tbody tr', { hasText: pipelineRunId }),
  ).toBeVisible({ timeout: 60_000 })
  await expect(explorer.locator('.ca-attribution tbody tr').first()).toContainText('SIMULATED')
})

test('成本分析：单次 Trace 查询 + UNKNOWN 清单', async ({ page }) => {
  await openCosts(page)

  // 单次对象查询：真实 API Trace 应有 Provider 报告用量 1852
  await page.getByLabel('查询类型').selectOption('trace')
  await page.getByLabel('对象 ID').fill(REAL_TRACE_ID)
  await page.getByRole('button', { name: '查询' }).click()
  const result = page.locator('.ca-query-result')
  await expect(result).toBeVisible({ timeout: 60_000 })
  await expect(result).toContainText(REAL_TRACE_ID)
  await expect(result).toContainText('1852')
  await expect(result).toContainText('PRICE_UNKNOWN')

  // UNKNOWN 清单：无价格调用被列出且说明原因
  await expect(page.getByText('UNKNOWN 与缺失数据（不当作 0）')).toBeVisible()
  await expect(page.getByText(/无价格 /).first()).toBeVisible()
  await expect(page.getByText(/不计为 0，不计入平均成本/)).toBeVisible()
})

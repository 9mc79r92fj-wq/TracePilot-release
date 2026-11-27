import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

/**
 * 第 55-A 步：数据治理与可信度浏览器验收。
 *
 * 覆盖：
 * 1. Overview 数据口径说明 + 全库来源分类表 + REAL_API=0 的明确提示；
 * 2. Trace Explorer 来源 / 状态 / 故障筛选（失败 Trace 可查询），REAL_API 空结果提示；
 * 3. Trace 详情的数据来源分类（MOCK，不自动升级为 REAL_API）与四类受控实验资格；
 * 4. Cost Analytics 数据来源范围筛选 + UNKNOWN/PRICE_UNKNOWN 不按 0 展示 + REAL_API 空态。
 */

const API = 'http://127.0.0.1:8000'

/**
 * 解析一条「MOCK 失败且含故障注入」的 Trace（由仓库脚本
 * `python scripts/create_mock_failure_trace.py` 生成；脚本每次生成的 trace_id 不固定，
 * 因此从 API 动态发现，避免数据重建后 spec 因硬编码 id 失效）。
 */
async function resolveFailedMockTraceId(request: APIRequestContext): Promise<string> {
  const resp = await request.get(`${API}/api/traces?source_class=MOCK&status=failed`)
  expect(resp.ok(), await resp.text()).toBeTruthy()
  const body = (await resp.json()) as {
    traces: { trace_id: string; fault_types: string[]; step_count: number }[]
  }
  const hit =
    body.traces.find((t) => t.fault_types.length > 0 && t.step_count >= 7) ?? body.traces[0]
  expect(hit, '应存在 MOCK 失败 Trace 用于来源分类/资格验收（请先运行 E2E fixture 脚本）').toBeTruthy()
  return hit.trace_id
}

// 成本分析首次分析全库 1.6 万 Trace 需要数十秒；先预热后端缓存，
// 避免页面 120 秒请求超时（该超时本身已放宽，但预热能让后续断言更稳定）。
test.beforeAll(async ({ request }) => {
  await request.get(`${API}/api/cost/overview?recent=1`, { timeout: 180_000 })
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

test('Overview：数据口径 + 全库来源分类 + REAL_API 空态提示', async ({ page, request }) => {
  const errors = collectErrors(page)
  const sources = await request.get(`${API}/api/governance/sources`)
  expect(sources.ok(), await sources.text()).toBeTruthy()
  const data = (await sources.json()) as {
    total_traces: number
    agent_traces: number
    by_source: Record<string, number>
  }

  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible({ timeout: 30_000 })

  await expect(page.getByText('数据范围与来源')).toBeVisible({
    timeout: 90_000,
  })
  await expect(page.getByText(/仅统计主 Trace 目录/)).toBeVisible()
  await expect(page.getByText(/统计全库（含评测 Trace 目录）/)).toBeVisible()
  await expect(page.getByText(String(data.agent_traces)).first()).toBeVisible()

  await expect(page.getByText('数据来源分类（全库）')).toBeVisible()
  const sourceTable = page.locator('.card', { hasText: '数据来源分类（全库）' })
  for (const cls of ['REAL_API', 'MOCK', 'SIMULATED', 'REPLAY', 'UNKNOWN']) {
    await expect(sourceTable.getByText(cls, { exact: true }).first()).toBeVisible()
  }
  if (data.by_source.REAL_API === 0) {
    await expect(page.getByText('暂无已验证的真实模型运行记录。').first()).toBeVisible()
  }

  await expect(page.getByText('失败记录概览（主 Trace 目录）')).toBeVisible()
  await expect(page.getByText(/失败 Trace 保存在主 Trace 目录/)).toBeVisible()

  expect(serious(errors)).toEqual([])
})

test('Trace Explorer：按来源/状态/故障筛选失败 Trace；REAL_API 空结果不冒充真实', async ({
  page,
}) => {
  const errors = collectErrors(page)
  await page.goto('/traces', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: 'Trace Explorer' })).toBeVisible({
    timeout: 30_000,
  })
  const rows = page.locator('table.tbl tbody tr')
  await expect(rows.first()).toBeVisible({ timeout: 90_000 })
  await expect(page.getByText('主 Trace 目录全量')).toBeVisible()

  // 来源筛选：MOCK
  await page.locator('#te-source').selectOption('MOCK')
  await expect
    .poll(async () => {
      const badges = await page.locator('table.tbl tbody tr td:nth-child(4)').allInnerTexts()
      return badges.length > 0 && badges.every((text) => text.trim() === 'MOCK')
    }, { timeout: 60_000 })
    .toBeTruthy()

  // 失败 + 仅看故障注入：全部行必须同时满足失败与包含故障类型
  await page.locator('#te-status').selectOption('failed')
  await page.getByLabel('仅看故障注入 Trace').check()
  await expect
    .poll(async () => (await rows.count()) > 0, { timeout: 60_000 })
    .toBeTruthy()
  const statuses = await page.locator('table.tbl tbody tr td:nth-child(3)').allInnerTexts()
  expect(statuses.every((text) => text.includes('失败'))).toBeTruthy()
  await expect(
    page.getByText('REAL_API 0 条：暂无已验证的真实模型运行记录').first(),
  ).toBeVisible()

  // REAL_API 空结果：明确提示，不伪造真实数据
  await page.locator('#te-source').selectOption('REAL_API')
  await expect(page.getByText(/当前筛选条件下没有 Trace/)).toBeVisible({ timeout: 60_000 })
  await expect(page.getByText(/当前筛选条件下没有 Trace/)).toBeVisible()

  expect(serious(errors)).toEqual([])
})

test('Trace 详情：来源分类 MOCK + 四类受控实验资格说明', async ({ page, request }) => {
  const errors = collectErrors(page)
  const failedMockTraceId = await resolveFailedMockTraceId(request)
  await page.goto(`/traces/${failedMockTraceId}`, { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: /Trace 详情/ })).toBeVisible({
    timeout: 30_000,
  })
  await expect(page.getByText('数据来源分类', { exact: true })).toBeVisible({
    timeout: 60_000,
  })
  const classCard = page.locator('.card', { hasText: '数据来源分类' })
  await expect(classCard.getByText('MOCK / 模拟')).toBeVisible()
  await expect(page.getByText(/data_source=mock（本地 Mock Agent 实际执行）/)).toBeVisible()

  await expect(page.getByText('受控实验资格', { exact: true })).toBeVisible()
  const eligibility = page.locator('.card', { hasText: '受控实验资格' })
  for (const label of ['只读重放', '受控重跑', '反事实实验', '修复候选生成']) {
    await expect(eligibility.getByText(label, { exact: true })).toBeVisible()
  }
  await expect(eligibility.getByText('可用', { exact: true }).first()).toBeVisible()

  expect(serious(errors)).toEqual([])
})

test('Cost Analytics：来源范围筛选 + UNKNOWN/PRICE_UNKNOWN 不按 0 + REAL_API 空态', async ({
  page,
}) => {
  const errors = collectErrors(page)
  await page.goto('/costs', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: /Cost Analytics/ })).toBeVisible({
    timeout: 30_000,
  })
  await expect(page.locator('.ca-kpis')).toBeVisible({ timeout: 120_000 })

  // 全库口径：范围提示 + 来源范围徽标
  await expect(page.getByText(/统计范围：/)).toBeVisible()
  await expect(page.locator('#ca-scope')).toHaveValue('')

  // REAL_API 范围：明确空态，不把 0 当真实成本
  await page.locator('#ca-scope').selectOption('REAL_API')
  await expect(page.getByText('暂无已验证的真实模型运行记录。').first()).toBeVisible({
    timeout: 120_000,
  })
  await expect(page.getByText('REAL_API 数据')).toBeVisible()
  const costValue = page.locator('.ca-kpis .stat', { hasText: '成本估算' }).locator('.v')
  await expect(costValue).toHaveText('UNKNOWN')

  // MOCK 范围：成本状态为 SIMULATED（0 表示未调用付费 API，不是账单）
  await page.locator('#ca-scope').selectOption('MOCK')
  await expect(page.getByText(/范围 MOCK/).first()).toBeVisible({ timeout: 120_000 })
  const mockCostBadge = page.locator('.ca-kpis .stat', { hasText: '成本估算' })
  await expect(mockCostBadge.getByText('SIMULATED', { exact: true })).toBeVisible({
    timeout: 60_000,
  })

  expect(serious(errors)).toEqual([])
})

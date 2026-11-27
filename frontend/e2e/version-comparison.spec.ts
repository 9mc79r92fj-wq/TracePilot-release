import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

/**
 * 第 49 / 50 步：Evaluation Version Comparison + Regression Detection 浏览器验收。
 *
 * 覆盖：
 * - 版本选择与真实评测运行元数据（agent_version 缺失必须显式说明，不伪造）
 * - 可比性横幅（同数据集 COMPARABLE / 跨数据集 NOT_COMPARABLE）
 * - 指标差异卡（pp 与相对变化、分子/分母、UNKNOWN 不当 0）
 * - Case-level Diff 状态迁移筛选
 * - Trace / DAG 跳转
 * - 回归风险矩阵、规则求值明细、门禁（INCONCLUSIVE 不等同 PASS）
 *
 * 数据由真实受控评测执行产生（零成本、无付费 API）。
 */

const API = 'http://127.0.0.1:8000'

let faultRunA = ''
let faultRunB = ''
let mainRun = ''

async function createRun(request: APIRequestContext, datasetId: string): Promise<string> {
  const created = await request.post(`${API}/api/evaluation/runs`, {
    data: { dataset_id: datasetId, judge_mode: 'none', force_new: true },
  })
  expect(created.ok(), await created.text()).toBeTruthy()
  const body = (await created.json()) as { run_id: string }
  const runId = body.run_id
  await expect
    .poll(
      async () => {
        const r = await request.get(`${API}/api/evaluation/runs/${runId}`)
        const d = (await r.json()) as { status: string; error?: string }
        if (d.status === 'failed') throw new Error(`评测运行失败: ${d.error}`)
        return d.status
      },
      { timeout: 180_000, intervals: [200, 500, 1000] },
    )
    .toBe('succeeded')
  return runId
}

test.beforeAll(async ({ request }) => {
  faultRunA = await createRun(request, 'tracepilot-fault-v1')
  faultRunB = await createRun(request, 'tracepilot-fault-v1')
  mainRun = await createRun(request, 'tracepilot-eval-v1')
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

async function openPage(page: Page) {
  await page.goto('/version-compare', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: /Version Comparison/ })).toBeVisible({
    timeout: 30_000,
  })
  const baseSel = page.getByLabel('基线版本（Baseline）')
  await expect(baseSel).toBeVisible({ timeout: 30_000 })
  await expect
    .poll(async () => await baseSel.locator('option').count(), { timeout: 30_000 })
    .toBeGreaterThan(1)
  return baseSel
}

async function compareFaultRuns(page: Page) {
  const baseSel = await openPage(page)
  await baseSel.selectOption(faultRunA)
  await page.getByLabel('候选版本（Candidate）').selectOption(faultRunB)
  await page.getByRole('button', { name: '开始对比' }).click()
  await expect(page.getByText(/可比性：/)).toBeVisible({ timeout: 90_000 })
}

test('版本对比：可比性 + 版本元数据 + 指标差异（分子/分母）', async ({ page }) => {
  const errors = collectErrors(page)
  await compareFaultRuns(page)

  // 同数据集、同版本 → 可比
  await expect(page.getByText('可比性：COMPARABLE')).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText('同版本（身份哈希一致）')).toBeVisible()
  await expect(page.getByText(/数据集：tracepilot-fault-v1@1\.0\.0/).first()).toBeVisible()

  // 版本元数据：缺失的 agent_version / Rubric 必须显式说明，不得伪造
  await expect(page.getByText('agent_version', { exact: true }).first()).toBeVisible()
  await expect(page.getByText(/无独立 agent_version 字段，未伪造/).first()).toBeVisible()
  await expect(page.getByText('未启用 Judge（Rubric 不适用）').first()).toBeVisible()

  // 指标差异卡：至少 5 个指标，且保留分子/分母与 pp 口径
  const metricCards = page.locator('.vc-metric')
  await expect(metricCards).toHaveCount(6)
  await expect(metricCards.filter({ hasText: '任务成功率' })).toBeVisible()
  await expect(metricCards.filter({ hasText: '安全通过率' })).toBeVisible()
  await expect(page.getByText(/分子\/分母：A/).first()).toBeVisible()
  await expect(page.getByText(/pp（百分点）/).first()).toBeVisible()
  // 平均耗时为标量指标：明确标注没有分子分母，而不是编造
  await expect(page.getByText('标量指标（无分子分母）').first()).toBeVisible()

  // Case-level Diff：共同案例 8 例
  await expect(page.locator('.vc-cases tbody tr')).toHaveCount(8)
  await expect(page.getByText(/仅对共同案例做配对比较/)).toBeVisible()
  await expect(page.getByText(/稳定失败 6/)).toBeVisible()

  expect(serious(errors)).toEqual([])
})

test('版本对比：跨数据集 → NOT_COMPARABLE 且差异不得解释为提升', async ({ page }) => {
  const baseSel = await openPage(page)
  await baseSel.selectOption(faultRunA)
  await page.getByLabel('候选版本（Candidate）').selectOption(mainRun)
  await page.getByRole('button', { name: '开始对比' }).click()

  await expect(page.getByText('可比性：NOT_COMPARABLE')).toBeVisible({ timeout: 90_000 })
  await expect(page.getByText(/DATASET_ID_MISMATCH/)).toBeVisible()
  await expect(page.getByText(/不得解释为性能提升或退化/)).toBeVisible()
  // 不可比时不做案例配对，缺失案例单独列出
  await expect(page.getByText('该迁移下没有案例')).toBeVisible()
  await expect(page.getByText(/缺失案例（不参与配对统计）/)).toBeVisible()

  // 不可比时回归检测门禁必须是 INCONCLUSIVE（不是 PASS）
  await page.getByRole('button', { name: '运行回归检测' }).click()
  await expect(page.locator('.rx-banner')).toBeVisible({ timeout: 120_000 })
  await expect(page.getByText('门禁 INCONCLUSIVE')).toBeVisible()
  await expect(page.getByText(/INCONCLUSIVE 视为无法判定，不等同 PASS/).first()).toBeVisible()
  await expect(page.getByText(/风险等级取规则严重度最大值/)).toBeVisible()
})

test('回归检测：风险矩阵 + 规则明细 + 门禁', async ({ page }) => {
  await compareFaultRuns(page)
  await page.getByRole('button', { name: '运行回归检测' }).click()

  const banner = page.locator('.rx-banner')
  await expect(banner).toBeVisible({ timeout: 120_000 })
  await expect(banner.getByText(/风险等级 (LOW|MEDIUM|HIGH|CRITICAL|UNKNOWN)/)).toBeVisible()
  await expect(banner.getByText(/门禁 (PASS|WARN|BLOCK|INCONCLUSIVE)/)).toBeVisible()
  await expect(page.getByText(/阈值来源：PROJECT_DEFAULT_NOT_INDUSTRY_STANDARD/)).toBeVisible()

  // 风险矩阵：6 个风险维度 × 4 个严重度
  await expect(page.locator('.rx-matrix')).toBeVisible()
  await expect(page.locator('.rx-matrix tbody tr')).toHaveCount(6)
  await expect(page.locator('.rx-matrix thead th')).toHaveCount(5)
  // 同版本运行：8 条规则都应有求值状态
  await expect(page.locator('.rx-rules tbody tr')).toHaveCount(8)
  await expect(page.getByText(/规则求值明细/)).toBeVisible()
  await expect(page.getByText(/阻断规则：3 条/)).toBeVisible()

  // 门禁语义声明（横幅与报告限制中各出现一次 → 取第一个）
  await expect(
    page.getByText(/INCONCLUSIVE 视为无法判定，不等同 PASS/).first(),
  ).toBeVisible()
})

test('Case-level Diff：状态迁移筛选与 Trace 跳转', async ({ page }) => {
  await compareFaultRuns(page)

  const filter = page.getByLabel('按状态迁移筛选')
  await expect(filter).toBeVisible()

  await filter.selectOption('PASS_TO_FAIL')
  await expect(page.getByText('该迁移下没有案例')).toBeVisible()

  await filter.selectOption('FAIL_TO_FAIL')
  await expect
    .poll(async () => await page.locator('.vc-cases tbody tr').count(), { timeout: 20_000 })
    .toBeGreaterThan(0)

  // 跳转候选 Trace
  await page.getByRole('button', { name: '候选 Trace' }).first().click()
  await expect(page).toHaveURL(/\/traces\/trace-/, { timeout: 30_000 })
})

test('版本对比：基线/候选相同 → 明确拒绝', async ({ page }) => {
  const baseSel = await openPage(page)
  await baseSel.selectOption(faultRunA)
  await page.getByLabel('候选版本（Candidate）').selectOption(faultRunA)
  await page.getByRole('button', { name: '开始对比' }).click()
  await expect(page.getByText('基线版本与候选版本不能相同')).toBeVisible({ timeout: 20_000 })
})

test('质量门禁：确定性检查展示阈值/实际值/触发规则', async ({ page }) => {
  await compareFaultRuns(page)
  await page.getByRole('button', { name: '运行质量门禁' }).click()

  // 受控失败数据集任务成功率为 0，低于演示阈值 0.90 → BLOCK（退出码 1）
  await expect(page.getByText('门禁 BLOCK')).toBeVisible({ timeout: 120_000 })
  await expect(page.getByText('退出码 1')).toBeVisible()
  await expect(page.getByText(/CI Run ID：/)).toBeVisible()
  await expect(
    page.getByText(/PROJECT_DEMO_DEFAULT_NOT_INDUSTRY_STANDARD/).first(),
  ).toBeVisible()
  // 门禁语义声明位于「限制说明」折叠区（当前 UI），展开后验证 INCONCLUSIVE 不等于 PASS
  const gateCard = page.locator('.card', { hasText: '质量门禁（CI）' })
  await gateCard.getByText(/限制说明（\d+）/).click()
  await expect(gateCard.locator('.vc-limits')).toContainText(
    'INCONCLUSIVE 表示无法判定，绝不等于 PASS',
  )

  // 触发规则 + 全部 11 项检查
  await expect(page.getByText('GATE_TASK_SUCCESS_BELOW_THRESHOLD').first()).toBeVisible()
  await expect(page.locator('.vc-gate-rules tbody tr').first()).toBeVisible()
  await expect(page.locator('.vc-gate-checks tbody tr')).toHaveCount(11)
  // 阈值/实际指标列
  await expect(page.getByText(/0\.9 ratio/).first()).toBeVisible()
})

test('质量门禁：版本不可比 → INCONCLUSIVE 不放行', async ({ page }) => {
  const baseSel = await openPage(page)
  await baseSel.selectOption(faultRunA)
  await page.getByLabel('候选版本（Candidate）').selectOption(mainRun)
  await page.getByRole('button', { name: '开始对比' }).click()
  await expect(page.getByText('可比性：NOT_COMPARABLE')).toBeVisible({ timeout: 90_000 })

  await page.getByRole('button', { name: '运行质量门禁' }).click()
  await expect(page.getByText('门禁 INCONCLUSIVE')).toBeVisible({ timeout: 120_000 })
  await expect(page.getByText('退出码 2')).toBeVisible()
  await expect(page.getByText('GATE_NOT_COMPARABLE').first()).toBeVisible()
})

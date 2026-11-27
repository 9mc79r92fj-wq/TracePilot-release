import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

/**
 * 第 54 步：Interactive Pareto Explorer 浏览器验收。
 *
 * 前置：通过 API 创建 3 个真实评测运行（受控模拟执行器、0 付费 API）：
 *  - fault 数据集 8 案例；eval 数据集 8 案例；eval 数据集 4 案例；
 * 分析默认使用候选数最多的可比组（eval），并对其余数据集给出明确警告。
 *
 * 覆盖：KPI、可比性警告、三维散点/二维投影切换、约束筛选、
 * NO_FEASIBLE_CANDIDATE、安全门禁筛选、Frontier/被支配解/支配关系、候选详情、
 * Evaluation Run 关联、Trace 跳转入口与数据来源声明。
 */

const API = 'http://127.0.0.1:8000'

test.describe.configure({ mode: 'serial' })

async function waitRun(request: APIRequestContext, runId: string) {
  await expect
    .poll(
      async () => {
        const r = await request.get(`${API}/api/evaluation/runs/${runId}`)
        return ((await r.json()) as { status: string }).status
      },
      { timeout: 120_000, intervals: [150, 300, 600] },
    )
    .toBe('succeeded')
}

async function createRun(
  request: APIRequestContext,
  datasetId: string,
  maxCases: number,
): Promise<string> {
  const created = await request.post(`${API}/api/evaluation/runs`, {
    data: { dataset_id: datasetId, max_cases: maxCases, judge_mode: 'none', force_new: true },
  })
  expect(created.ok(), await created.text()).toBeTruthy()
  const runId = ((await created.json()) as { run_id: string }).run_id
  await waitRun(request, runId)
  return runId
}

async function openPareto(page: Page) {
  await page.goto('/pareto', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: /Pareto Explorer/ })).toBeVisible({
    timeout: 30_000,
  })
  await expect(page.locator('.pareto-kpis')).toBeVisible({ timeout: 90_000 })
  await expect(page.locator('.pareto-scatter svg')).toBeVisible({ timeout: 60_000 })
}

function frontierCard(page: Page) {
  return page.locator('.card', { hasText: 'Pareto Frontier（' })
}

interface FilterResult {
  status: string
  feasible_count: number
  pareto_frontier_count: number
  candidates: { candidate_id: string; eligible: boolean; safety_gate_status: string | null }[]
}

/** 点击「应用约束」并等待后端筛选响应，避免 UI 与请求竞态。 */
async function applyAndCapture(page: Page): Promise<FilterResult> {
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) => r.request().method() === 'POST' && r.url().includes('/filter'),
      { timeout: 120_000 },
    ),
    page.getByRole('button', { name: '应用约束' }).click(),
  ])
  expect(response.ok(), await response.text()).toBeTruthy()
  return (await response.json()) as FilterResult
}

test.beforeAll(async ({ request }) => {
  await createRun(request, 'tracepilot-fault-v1', 8)
  await createRun(request, 'tracepilot-eval-v1', 8)
  await createRun(request, 'tracepilot-eval-v1', 4)
})

test('Pareto Explorer：KPI + 可比性警告 + 三维散点 + Frontier', async ({ page }) => {
  await openPareto(page)

  // 顶部 KPI 与诚实性声明
  await expect(page.locator('.pareto-kpis .stat')).toHaveCount(4)
  await expect(page.locator('.pareto-kpis').getByText('候选版本')).toBeVisible()
  await expect(page.locator('.pareto-kpis').getByText('非支配解')).toBeVisible()
  await expect(page.getByText('本地确定性数据 · 付费 API 0 次')).toBeVisible()

  // 可比性检查：默认最大可比组，带可比性警告，且不把结果解释为真实模型性能比较
  const compare = page.locator('.pareto-compare')
  await expect(compare).toBeVisible()
  await expect(compare).toContainText('可比性：')
  // 至少暴露一条可比性警告（评分口径/样本量/数据集不一致）
  await expect(compare).toContainText(
    /SCORING_MODE_MISMATCH|SMALL_SAMPLE_SIZE|DATASET_ID_MISMATCH/,
  )
  await expect(compare).toContainText('模拟数据不代表真实模型性能')
  await expect(page.getByText(/不是 Provider 实际账单/)).toBeVisible()

  // 三维散点与二维投影切换
  await expect(page.locator('.pareto-scatter svg')).toBeVisible()
  await expect(page.locator('.pareto-dot').first()).toBeVisible()
  await page.getByRole('button', { name: '质量-延迟' }).click()
  await expect(page.locator('.pareto-scatter svg')).toBeVisible()
  await page.getByRole('button', { name: '三维散点' }).click()

  // Frontier 列表（至少 1 个非支配解），且没有把缺失指标当成 0
  await expect(frontierCard(page).locator('tbody tr').first()).toBeVisible({ timeout: 60_000 })
  const frontierCount = await frontierCard(page).locator('tbody tr').count()
  expect(frontierCount).toBeGreaterThanOrEqual(1)
  await expect(page.locator('.card', { hasText: '被支配解（' })).toBeVisible()
  await expect(page.locator('.card', { hasText: '支配关系（' })).toBeVisible()
})

test('Pareto Explorer：约束筛选 + NO_FEASIBLE_CANDIDATE + 安全门禁', async ({ page }) => {
  await openPareto(page)

  // 切换到 fault 数据集（按设计全部案例失败，task_success_rate=0）
  const faultOption = page
    .locator('#pareto-dataset option')
    .filter({ hasText: 'tracepilot-fault-v1' })
  expect(await faultOption.count()).toBeGreaterThanOrEqual(1)
  const faultValue = await faultOption.first().getAttribute('value')
  expect(faultValue).toBeTruthy()
  await page.locator('#pareto-dataset').selectOption(faultValue as string)
  await page.locator('#pareto-quality').fill('1')

  // 最低成功率 1 → fault 数据集无可行候选，明确显示 NO_FEASIBLE_CANDIDATE
  const noFeasible = await applyAndCapture(page)
  expect(noFeasible.feasible_count).toBe(0)
  await expect(page.locator('.pareto-status')).toContainText('NO_FEASIBLE_CANDIDATE', {
    timeout: 60_000,
  })
  await expect(frontierCard(page).getByText('NO_FEASIBLE_CANDIDATE')).toBeVisible()

  // 重置后 Frontier 恢复
  await page.getByRole('button', { name: '重置', exact: true }).click()
  await expect(page.locator('.pareto-status')).toHaveCount(0, { timeout: 60_000 })
  await expect(frontierCard(page).locator('tbody tr').first()).toBeVisible({ timeout: 60_000 })

  // 安全门禁筛选：结果要么无可行候选，要么前沿候选全部达到 PASS（不伪造门禁）
  await page.locator('#pareto-safety').selectOption('PASS')
  const gateFiltered = await applyAndCapture(page)
  if (gateFiltered.feasible_count === 0) {
    await expect(page.locator('.pareto-status')).toContainText('NO_FEASIBLE_CANDIDATE', {
      timeout: 60_000,
    })
  } else {
    const gates = frontierCard(page).locator('tbody tr td:nth-child(7)')
    await expect(gates.first()).toBeVisible({ timeout: 60_000 })
    for (const text of await gates.allInnerTexts()) {
      expect(text.trim()).toBe('PASS')
    }
  }

  // 取消门禁限制 → 恢复可行候选
  await page.locator('#pareto-safety').selectOption('NOT_REQUIRED')
  const restored = await applyAndCapture(page)
  expect(restored.feasible_count).toBeGreaterThanOrEqual(1)
  await expect(frontierCard(page).locator('tbody tr').first()).toBeVisible({ timeout: 60_000 })
})

test('Pareto Explorer：候选详情 + Evaluation Run 关联 + 悬停指标', async ({ page }) => {
  await openPareto(page)

  // 悬停三维散点：显示指标提示（散点可能重叠，直接派发 mouseover 由 React 处理，
  // 避免被上层散点拦截 pointer events）
  await page.locator('.pareto-dot').first().dispatchEvent('mouseover')
  await expect(page.locator('.pareto-tooltip')).toBeVisible()
  await expect(page.locator('.pareto-tooltip')).toContainText('质量：')
  await expect(page.locator('.pareto-tooltip')).toContainText('成本：')

  // 点击 Frontier 第一行 → 右侧详情联动
  await frontierCard(page).locator('tbody tr').first().getByRole('button', { name: '详情' }).click()
  const detail = page.locator('.pareto-detail')
  await expect(detail).toContainText('候选版本详情')
  await expect(detail.locator('.pareto-metric-row')).toBeVisible()
  await expect(detail).toContainText('Agent 版本')
  await expect(detail).toContainText(/金额为估算值；PRICE_UNKNOWN 与缺失成本显示「—」/)

  // 关联 Evaluation Run
  await detail.getByRole('button', { name: '查看 Evaluation Run' }).click()
  await expect(detail.locator('.pareto-query-result')).toBeVisible({ timeout: 60_000 })
  await expect(detail.locator('.pareto-query-result')).toContainText('eval-')
  await expect(detail.locator('.pareto-query-result')).toContainText('成功率')

  // 关联 Trace 入口可跳转（只验证入口存在/可点击后跳转，不深入 Trace 页面细节）
  const traceButton = detail.getByRole('button', { name: '查看关联 Trace' })
  await expect(traceButton).toBeVisible()
  await traceButton.click()
  await expect(page).toHaveURL(/\/traces\//)
})

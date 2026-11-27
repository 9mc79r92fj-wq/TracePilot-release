import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

/**
 * 第 55 步：需求回溯修复的浏览器验收。
 *
 * 覆盖：
 * 1. Architecture 页系统架构 + 真实运行概览（来源分布/最近记录，无开发核对表）；
 * 2. Evaluation Center 根因准确率指标（Top-1/Top-3，真实评测运行 + 受控故障集金标准）；
 * 3. Trace 详情决策解释（证据化理由，明确不是思维链）；
 * 4. HITL 审批工作流 API（创建 → 待审 → 批准；不自动批准）。
 *
 * 全部本地确定性执行，0 付费 API 调用。
 */

const API = 'http://127.0.0.1:8000'

/**
 * 决策解释使用一条真实的本地 MOCK Trace（当前数据没有已验证的 REAL_API Trace，
 * REAL_API 全库 0，不假设存在真实调用）；trace_id 从 API 动态发现，避免数据重建后失效。
 */
async function resolveMockTraceId(request: APIRequestContext): Promise<string> {
  const resp = await request.get(`${API}/api/traces?source_class=MOCK`)
  expect(resp.ok(), await resp.text()).toBeTruthy()
  const body = (await resp.json()) as { traces: { trace_id: string; step_count: number }[] }
  const hit = body.traces.find((t) => t.step_count >= 4) ?? body.traces[0]
  expect(hit, '应存在 MOCK Trace 用于决策解释验收').toBeTruthy()
  return hit.trace_id
}

async function waitRun(request: APIRequestContext, runId: string) {
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

async function runFaultEval(page: Page) {
  await page.goto('/evaluation', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: /Evaluation Center/ })).toBeVisible()
  const dsSelect = page.locator('select').first()
  await expect(dsSelect).toBeVisible({ timeout: 20_000 })
  await dsSelect.selectOption('tracepilot-fault-v1')
  await page.getByRole('button', { name: /运行评测/ }).click()
  await expect(page.getByText('案例结果 / Bad Case')).toBeVisible({ timeout: 120_000 })
}

test('Architecture：系统架构与真实运行概览（不含开发需求核对表）', async ({ page }) => {
  await page.goto('/architecture', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: /Architecture/ })).toBeVisible()

  // 运行概览 KPI 来自真实 API
  await expect(page.getByText('系统运行概览')).toBeVisible({ timeout: 90_000 })
  for (const label of ['Trace 总数', 'Agent 运行记录', '评测运行', '评测失败', '真实 API Trace', '最近运行时间']) {
    await expect(page.getByText(label, { exact: true }).first()).toBeVisible()
  }

  // 数据来源分布 + 最近运行记录
  await expect(page.locator('.card-head', { hasText: '数据来源分布' }).first()).toBeVisible()
  for (const source of ['REAL_API', 'MOCK', 'SIMULATED', 'REPLAY', 'UNKNOWN']) {
    await expect(page.locator('table.tbl').first().getByText(source, { exact: true }).first()).toBeVisible()
  }
  await expect(page.getByText('最近运行记录')).toBeVisible()
  await expect(page.locator('table.tbl').last().locator('tbody tr').first()).toBeVisible()

  // 开发过程核对表不得出现在产品界面
  await expect(page.getByText(/原始需求/)).toHaveCount(0)
  await expect(page.getByText(/需求能力矩阵/)).toHaveCount(0)
})

test('Evaluation Center：根因 Top-1/Top-3 指标（真实运行 + 受控金标准）', async ({ page }) => {
  await runFaultEval(page)
  const card = page.locator('.card', { hasText: '根因准确率评估' })
  await expect(card).toBeVisible()
  await card.getByRole('button', { name: '计算指标' }).click()
  await expect(card.locator('.rca-kpis')).toBeVisible({ timeout: 180_000 })
  await expect(card.getByText('Top-1 准确率')).toBeVisible()
  await expect(card.getByText('Top-3 覆盖率')).toBeVisible()
  await expect(card.getByText(/模拟执行，非真实计费/)).toBeVisible()
  await expect(card.getByText(/合成故障评测集/)).toBeVisible()
  await expect(card.getByText(/未验证（不按 0 或 100% 处理）/)).toBeVisible()
  const rows = card.locator('.rca-case-row')
  expect(await rows.count()).toBeGreaterThanOrEqual(1)
})

test('Trace 详情：决策解释（证据化理由，不是思维链）', async ({ page, request }) => {
  const mockTraceId = await resolveMockTraceId(request)
  await page.goto(`/traces/${mockTraceId}`, { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: /Trace 详情/ })).toBeVisible()
  const card = page.locator('.card', { hasText: '决策解释' })
  await expect(card).toBeVisible({ timeout: 60_000 })
  await expect(card.getByText('证据化理由（非思维链）')).toBeVisible()
  await expect(card.locator('tbody tr').first()).toBeVisible()
  await expect(card.getByText(/不保存也不推断模型内部思维链/)).toBeVisible()
})

test('HITL：审批工作流（PENDING → APPROVED，不自动批准）', async ({ request }) => {
  const created = await request.post(`${API}/api/hitl/approvals`, {
    data: {
      action: 'e2e-test-approval',
      reason: '浏览器验收',
      risk_level: 'HIGH',
      run_id: 'e2e-run',
    },
  })
  expect(created.ok(), await created.text()).toBeTruthy()
  const approval = (await created.json()) as {
    approval_id: string
    status: string
    decided_at: string | null
  }
  expect(approval.status).toBe('PENDING')
  expect(approval.decided_at).toBeNull()

  const listed = await request.get(`${API}/api/hitl/approvals?status=PENDING`)
  expect(listed.ok()).toBeTruthy()
  const listedBody = (await listed.json()) as { approvals: { approval_id: string }[] }
  expect(
    listedBody.approvals.some((row) => row.approval_id === approval.approval_id),
  ).toBeTruthy()

  const decided = await request.post(
    `${API}/api/hitl/approvals/${approval.approval_id}/decision`,
    { data: { decision: 'APPROVED', decided_by: 'e2e-reviewer', comment: 'approved' } },
  )
  expect(decided.ok(), await decided.text()).toBeTruthy()
  const decidedBody = (await decided.json()) as {
    status: string
    decided_by: string
    decided_at: string | null
  }
  expect(decidedBody.status).toBe('APPROVED')
  expect(decidedBody.decided_by).toBe('e2e-reviewer')
  expect(decidedBody.decided_at).toBeTruthy()
})

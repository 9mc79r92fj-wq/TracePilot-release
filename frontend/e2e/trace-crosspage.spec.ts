import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

/**
 * 第 55-B/C 步：Trace 跨页面联动 + DAG 复杂执行展示 + 故障分析最小闭环。
 *
 * 使用的受控测试 Trace（SIMULATED，脚本生成，非 REAL_API）：
 * scripts/create_simulated_retry_trace.py（E2E 前置 fixture，运行前生成）
 * - 13 个执行事件：text_analyzer 成功；calculator 连续 4 次执行全部注入 TOOL_TIMEOUT
 *   （1 次初始 + 3 次重试，attempt 1..4，retry_of 形成重试链）
 * - 最终失败（status=failed），DAG 13 节点 / 12 条依赖边
 *
 * 脚本每次生成的 trace_id 不固定，因此从 API 动态发现（13 步 / failed / simulated），
 * 并从 Trace 详情中取最后一个失败步骤作为断言锚点。
 */

const API = 'http://127.0.0.1:8000'

async function resolveControlledRetryTrace(
  request: APIRequestContext,
): Promise<{ traceId: string; lastFailedStepId: string }> {
  const listResp = await request.get(`${API}/api/traces`)
  expect(listResp.ok(), await listResp.text()).toBeTruthy()
  const body = (await listResp.json()) as {
    traces: { trace_id: string; status: string; data_source: string; step_count: number }[]
  }
  const hit = body.traces.find(
    (t) => t.step_count === 13 && t.status === 'failed' && t.data_source === 'simulated',
  )
  expect(
    hit,
    '应存在 13 步受控重试失败 Trace（请先运行 scripts/create_simulated_retry_trace.py）',
  ).toBeTruthy()
  const detailResp = await request.get(`${API}/api/traces/${hit!.trace_id}`)
  expect(detailResp.ok(), await detailResp.text()).toBeTruthy()
  const detail = (await detailResp.json()) as {
    trace: { steps: { step_id: string; status: string }[] }
  }
  const failedSteps = detail.trace.steps.filter((s) => s.status === 'failed')
  expect(failedSteps.length, '受控重试 Trace 应包含失败步骤').toBeGreaterThan(0)
  return {
    traceId: hit!.trace_id,
    lastFailedStepId: failedSteps[failedSteps.length - 1].step_id,
  }
}

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

test('跨页面闭环：Trace Explorer → DAG(重试/失败节点) → 归因 → 反事实 → 修复，刷新保持 trace_id', async ({
  page,
  request,
}) => {
  const errors = collectErrors(page)
  const { traceId: TRACE_ID, lastFailedStepId: LAST_FAILED_STEP } =
    await resolveControlledRetryTrace(request)

  // 1) Trace Explorer：行内提供跨页面分析入口（选中一条 Trace 后直接进入目标页面）
  await page.goto('/traces', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: 'Trace Explorer' })).toBeVisible({ timeout: 30_000 })
  const rows = page.locator('table.tbl tbody tr')
  await expect(rows.first()).toBeVisible({ timeout: 90_000 })
  const firstRowId = (await rows.first().locator('td.mono').first().innerText()).trim()
  await rows.first().getByRole('button', { name: 'DAG', exact: true }).click()
  await expect(page).toHaveURL(new RegExp('/dag\\?trace_id=' + firstRowId))
  await expect(page.getByRole('heading', { name: /DAG Analysis/ })).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('.react-flow__node').first()).toBeVisible({ timeout: 60_000 })

  // 2) 直接由 URL 打开受控测试 Trace：DAG 展示 13 个执行事件（重试不合并）
  await page.goto(`/dag?trace_id=${TRACE_ID}`, { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: /DAG Analysis/ })).toBeVisible({ timeout: 30_000 })
  await expect
    .poll(async () => await page.locator('.react-flow__node').count(), { timeout: 60_000 })
    .toBe(13)
  await expect(page.getByText(`URL trace_id: ${TRACE_ID}`)).toBeVisible()

  // 失败节点红色状态 + 超时/重试标记
  expect(await page.locator('.dagnode.failed').count()).toBeGreaterThanOrEqual(4)
  expect(await page.locator('.dagnode .badge', { hasText: '超时' }).count()).toBeGreaterThanOrEqual(4)
  expect(await page.locator('.dagnode .badge', { hasText: '重试 #' }).count()).toBeGreaterThanOrEqual(3)

  // 3) 点击失败节点 → 证据面板（错误类型/重试链/超时原因）
  const lastStepShortId = LAST_FAILED_STEP.split('-').slice(-1)[0]
  await page.locator('.dagnode', { hasText: lastStepShortId }).first().click()
  const sidePanel = page.locator('.dag-side')
  await expect(sidePanel.getByText('节点详情')).toBeVisible({ timeout: 20_000 })
  await expect(sidePanel).toContainText('tool_timeout')
  await expect(sidePanel).toContainText('重试信息')
  await expect(sidePanel).toContainText('超时信息')

  // 4) 由失败节点进入 Fault Attribution（携带 trace_id + step_id，自动运行归因）
  await sidePanel.getByRole('button', { name: '用该节点做故障归因' }).click()
  await expect(page).toHaveURL(new RegExp('/attribution\\?trace_id=' + TRACE_ID))
  await expect(page.getByRole('heading', { name: 'Fault Attribution' })).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('#at')).toHaveValue(TRACE_ID, { timeout: 60_000 })
  await expect(page.locator('.hl-row')).toContainText(LAST_FAILED_STEP, { timeout: 60_000 })
  await expect(page.getByText('注入故障').first()).toBeVisible()

  // 5) 从归因进入 Counterfactual Lab（同一 trace_id，资格满足可运行）
  await page.getByRole('button', { name: '去验证因果' }).click()
  await expect(page).toHaveURL(new RegExp('/counterfactual\\?trace_id=' + TRACE_ID))
  await expect(page.getByRole('heading', { name: 'Counterfactual Lab' })).toBeVisible({ timeout: 30_000 })
  const cfSelect = page.getByLabel('选择 Trace')
  await expect(cfSelect).toHaveValue(TRACE_ID, { timeout: 60_000 })
  await expect(page.getByRole('button', { name: /运行实验/ })).toBeEnabled({ timeout: 60_000 })

  // 6) Repair Studio：同一 trace_id 自动加载候选；刷新后 URL/trace_id 仍有效
  await page.goto(`/repair?trace_id=${TRACE_ID}`, { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: 'Repair Studio' })).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('select[aria-label="选择 Trace"]')).toHaveValue(TRACE_ID, {
    timeout: 60_000,
  })
  await expect(page.locator('.rs-card').first()).toBeVisible({ timeout: 60_000 })
  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(page).toHaveURL(new RegExp('/repair\\?trace_id=' + TRACE_ID))
  await expect(page.locator('select[aria-label="选择 Trace"]')).toHaveValue(TRACE_ID, {
    timeout: 60_000,
  })

  expect(serious(errors)).toEqual([])
})

test('无效 trace_id：明确报错，不静默回退到其他 Trace', async ({ page }) => {
  await page.goto('/dag?trace_id=trace-does-not-exist-55bc', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: /DAG Analysis/ })).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText(/Trace 不存在或不在当前 Trace 目录/)).toBeVisible({ timeout: 90_000 })

  await page.goto('/counterfactual?trace_id=trace-does-not-exist-55bc', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: 'Counterfactual Lab' })).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText(/Trace 不存在或不在当前 Trace 目录/)).toBeVisible({ timeout: 90_000 })
})

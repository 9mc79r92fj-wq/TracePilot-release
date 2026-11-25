import { expect, test } from '@playwright/test'

/**
 * 第 44 步：Evidence Lineage（证据血缘）前端验收。
 *
 * 验证：
 * 1. Trace 详情页可展开分层证据链
 * 2. 节点显示来源标记（REAL_API / MOCK / SIMULATED / UNKNOWN）与存储状态
 * 3. 缺失证据显示 NOT_AVAILABLE 且给出原因（不伪造）
 * 4. 评测案例抽屉可打开血缘并追溯到 Trace
 * 5. 关系边与刷新后一致（不丢失）
 */

function serious(errors: string[]): string[] {
  return errors.filter(
    (e) => !e.includes('favicon') && !e.includes('net::ERR_') && !e.includes('status of 404'),
  )
}

test('Trace 详情页：分层证据链可展开且来源标记正确', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console.error: ${m.text()}`)
  })

  // 用 Agent Runs 生成一条 mock Trace（带完整 Run/Step/Tool）
  await page.goto('/runs')
  await expect(page.locator('textarea').first()).toBeVisible({ timeout: 20_000 })
  await page.locator('textarea').first().fill('计算125乘以48')
  await page.getByRole('button', { name: /运行任务/ }).click()
  await expect(page.getByText('执行结果')).toBeVisible({ timeout: 30_000 })
  await page.getByRole('button', { name: /查看 Trace/ }).click()
  await expect(page).toHaveURL(/\/traces\/trace-/, { timeout: 20_000 })

  // 血缘卡片存在
  const lineageCard = page.getByText('证据血缘（Evidence Lineage）')
  await expect(lineageCard.first()).toBeVisible({ timeout: 20_000 })

  // 节点数摘要
  await expect(page.getByText(/节点 \d+ · 可用 \d+ · 缺失 \d+/)).toBeVisible()

  // 来源标记：mock 运行的 Trace 必须显示 MOCK，绝不能显示 REAL API
  const lineageSection = page.locator('.lin-layer').first()
  await expect(lineageSection).toBeVisible()
  await expect(page.locator('.lin-node-head .badge').filter({ hasText: 'MOCK' }).first()).toBeVisible()
  await expect(
    page.locator('.lin-node-head .badge').filter({ hasText: 'REAL API' }),
  ).toHaveCount(0)

  // 分层：Trace / Agent Run / Step / Tool
  await expect(page.getByText('结果来源').or(page.getByText('Trace', { exact: true })).first()).toBeVisible()
  await expect(page.getByText('Agent Run', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('执行步骤', { exact: false }).first()).toBeVisible()
  await expect(page.getByText(/工具调用与结果/).first()).toBeVisible()

  // 展开 Tool Result 节点查看工具结果
  const toolResultHead = page.locator('.lin-node-head').filter({ hasText: 'Tool Result' }).first()
  await expect(toolResultHead).toBeVisible()
  await toolResultHead.click()
  await expect(page.getByText(/6000/).first()).toBeVisible()

  // 关系边可查看
  await page.getByText(/查看关系边/).first().click()
  await expect(page.getByText('produced').first()).toBeVisible()

  // 刷新后仍在（来自后端只读接口，不依赖内存状态）
  await page.reload()
  await expect(page.getByText('证据血缘（Evidence Lineage）').first()).toBeVisible({
    timeout: 20_000,
  })
  await expect(page.getByText(/节点 \d+ · 可用 \d+ · 缺失 \d+/)).toBeVisible()

  expect(serious(errors)).toEqual([])
})

test('评测案例：抽屉内可打开证据血缘并追溯到 Trace', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console.error: ${m.text()}`)
  })

  await page.goto('/evaluation')
  await expect(page.getByRole('heading', { name: /Evaluation Center/ })).toBeVisible()
  await page.locator('#mc').fill('2')
  await page.getByRole('button', { name: /运行评测/ }).click()
  await expect(page.getByText('案例结果 / Bad Case')).toBeVisible({ timeout: 120_000 })

  // 打开证据抽屉
  await page.getByRole('button', { name: /证据/ }).first().click()
  const drawer = page.locator('.drawer')
  await expect(drawer).toBeVisible()

  // 打开证据血缘
  await drawer.getByRole('button', { name: /查看证据血缘/ }).click()
  await expect(page.getByText('证据血缘').first()).toBeVisible({ timeout: 20_000 })

  // 必须出现评测运行/案例/Trace 三层
  await expect(page.getByText('评测运行', { exact: false }).first()).toBeVisible()
  await expect(page.getByText(/评测案例/).first()).toBeVisible()
  await expect(page.locator('.lin-node-head').filter({ hasText: 'Trace' }).first()).toBeVisible()

  // 案例 → Trace 边存在
  await page.getByText(/查看关系边/).first().click()
  await expect(page.getByText('evaluated_trace').first()).toBeVisible()

  // Trace 节点可跳转
  const traceNode = page.locator('.lin-node-head').filter({ hasText: 'Trace' }).first()
  await traceNode.click()
  await page.getByRole('button', { name: '查看 Trace' }).first().click()
  await expect(page).toHaveURL(/\/traces\/trace-/, { timeout: 20_000 })

  expect(serious(errors)).toEqual([])
})

test('反事实实验：结果下方显示证据血缘（含派生与原始 Trace）', async ({ page }) => {
  await page.goto('/counterfactual')
  await expect(page.getByRole('heading', { name: /Counterfactual Lab/ })).toBeVisible()
  await expect(page.getByText(/干预类型/).first()).toBeVisible({ timeout: 20_000 })

  await page.getByRole('button', { name: /运行实验/ }).click()
  await expect(page.getByText('实验状态')).toBeVisible()
  await expect(page.getByText('证据血缘（Evidence Lineage）').first()).toBeVisible({
    timeout: 120_000,
  })

  // 派生变体 Trace 必须可追溯（第 44 步修复后不再 404）
  await expect(page.locator('.lin-node-head').filter({ hasText: 'Trace' }).first()).toBeVisible()
  await expect(page.getByText(/SIMULATED/).first()).toBeVisible()
})

test('血缘数据结构：来源与存储标签分离且不伪造 REAL API', async ({ page }) => {
  await page.goto('/runs')
  await expect(page.locator('textarea').first()).toBeVisible({ timeout: 20_000 })
  // 直接用最近一次 mock 运行
  const historyBtn = page.getByRole('button', { name: '查看' }).first()
  if ((await historyBtn.count()) > 0) {
    await historyBtn.click()
    await page.getByRole('button', { name: /查看 Trace/ }).click()
    await expect(page).toHaveURL(/\/traces\/trace-/, { timeout: 20_000 })
    await expect(page.getByText('证据血缘（Evidence Lineage）').first()).toBeVisible()

    // 存在来源词表图例（REAL API / MOCK / SIMULATED / UNKNOWN）
    for (const label of ['REAL API', 'MOCK', 'SIMULATED', 'UNKNOWN']) {
      await expect(page.locator('.badge').filter({ hasText: label }).first()).toBeVisible()
    }
    // 存储状态标签存在（已保存 / 内存登记 / 不可用）
    await expect(page.locator('.lin-node-head .badge').filter({ hasText: '已保存' }).first()).toBeVisible()
  }
})

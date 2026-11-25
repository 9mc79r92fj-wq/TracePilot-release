import { expect, test } from '@playwright/test'

/**
 * 第 48 步：Failure Pattern Mining 浏览器验收。
 *
 * 覆盖：失败模式分布图、高频排行榜、类型/工具筛选、模式详情抽屉、
 * 归因证据下钻、Trace/DAG 跳转、两个运行对比、分母透明展示。
 */

function serious(errors: string[]): string[] {
  return errors.filter(
    (e) => !e.includes('favicon') && !e.includes('net::ERR_') && !e.includes('status of 404'),
  )
}

async function runFaultEval(page: import('@playwright/test').Page) {
  await page.goto('/evaluation')
  await expect(page.getByRole('heading', { name: /Evaluation Center/ })).toBeVisible()
  const dsSelect = page.locator('select').first()
  await expect(dsSelect).toBeVisible({ timeout: 20_000 })
  await dsSelect.selectOption('tracepilot-fault-v1')
  await page.getByRole('button', { name: /运行评测/ }).click()
  await expect(page.getByText('案例结果 / Bad Case')).toBeVisible({ timeout: 120_000 })
}

test('失败模式：分布图 + 排行榜 + 分母透明', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console.error: ${m.text()}`)
  })

  await runFaultEval(page)

  // 自动分析卡片
  await expect(page.getByText(/失败模式挖掘（Failure Pattern Mining）/)).toBeVisible()
  await expect(page.getByText('Failure Pattern Explorer')).toBeVisible({ timeout: 60_000 })

  // 分母声明
  await expect(page.getByText('统计分母', { exact: true })).toBeVisible()
  await expect(page.getByText(/未执行案例不计入任何模式/).first()).toBeVisible()

  // 分布图 + 排行榜
  await expect(page.getByText('失败模式分布（点击下钻）')).toBeVisible()
  await expect(page.getByText('高频问题排行')).toBeVisible()
  await expect(page.locator('.fp-bar-row').first()).toBeVisible()
  await expect(page.locator('.fp-rank-row').first()).toBeVisible()

  // 模式数 > 0
  await expect(page.getByText(/模式 \d+ · 分析 \d+ 例/)).toBeVisible()

  expect(serious(errors)).toEqual([])
})

test('失败模式：筛选与证据下钻', async ({ page }) => {
  await runFaultEval(page)
  await expect(page.getByText('Failure Pattern Explorer')).toBeVisible({ timeout: 60_000 })

  // 按失败类型筛选
  const typeSel = page.getByLabel('按失败类型筛选')
  await expect(typeSel).toBeVisible()
  await typeSel.selectOption('TOOL_TIMEOUT')
  await expect(page.getByText(/匹配 1 \/ \d+ 例/)).toBeVisible()

  // 按工具筛选
  const toolSel = page.getByLabel('按工具名称筛选')
  await toolSel.selectOption('calculator')
  await expect(page.getByText(/匹配 \d+ \/ \d+ 例/)).toBeVisible()

  // 证据下钻：点击「证据」→ 模式抽屉内的归因证据面板
  await page.getByRole('button', { name: '证据' }).first().click()
  await expect(page.getByText('归因证据图')).toBeVisible({ timeout: 30_000 })

  // 关闭抽屉后再跳转 Trace（抽屉会遮挡表格按钮）
  await page.locator('.drawer .drawer-head button[aria-label="关闭"]').click()
  await expect(page.locator('.drawer')).toHaveCount(0)

  // Trace 跳转
  await page.getByRole('button', { name: 'Trace' }).first().click()
  await expect(page).toHaveURL(/\/traces\/trace-/, { timeout: 20_000 })
})

test('失败模式：模式详情抽屉展示指纹字段与 null 缺失', async ({ page }) => {
  await runFaultEval(page)
  await expect(page.locator('.fp-bar-row').first()).toBeVisible({ timeout: 60_000 })

  // 点击分布图某模式 → 详情抽屉
  await page.locator('.fp-bar-row').first().click()
  const drawer = page.locator('.drawer')
  await expect(drawer).toBeVisible()

  // 指纹字段表（5 维）
  await expect(drawer.getByText('fingerprint_fields').or(drawer.getByText('failure_type'))).toBeVisible()
  for (const f of ['failure_type', 'tool_name', 'failed_metric', 'recovery_status', 'scenario_type']) {
    await expect(drawer.getByText(f, { exact: true }).first()).toBeVisible()
  }
  // 指纹不可证明根因声明
  await expect(page.getByText(/不代表已证明的相同根因/).first()).toBeVisible()

  // 该模式案例可点开
  await expect(drawer.getByRole('button', { name: /^fail-/ }).first()).toBeVisible()
})

test('失败模式：两个运行对比可用', async ({ page }) => {
  await runFaultEval(page)
  await expect(page.getByText('Failure Pattern Explorer')).toBeVisible({ timeout: 60_000 })

  // 再运行一次，确保存在两个可对比的运行记录
  await page.getByRole('button', { name: /运行评测/ }).click()
  await expect(page.getByText(/失败模式挖掘（Failure Pattern Mining）/)).toBeVisible({
    timeout: 120_000,
  })
  await page.waitForTimeout(2000)

  const selA = page.getByLabel('对比运行A')
  const selB = page.getByLabel('对比运行B')
  await expect(selA).toBeVisible()
  await expect
    .poll(async () => await selB.locator('option').count(), { timeout: 20_000 })
    .toBeGreaterThan(1)

  const values = await selB.locator('option').evaluateAll((els) =>
    els.map((e) => (e as HTMLOptionElement).value).filter(Boolean),
  )
  await selA.selectOption(values[0])
  await selB.selectOption(values[1] ?? values[0])
  await page.getByRole('button', { name: /^对比$/ }).click()

  // 对比结果：状态徽标
  await expect(page.getByText(/新增 \d+/)).toBeVisible({ timeout: 60_000 })
  await expect(page.getByText(/不变 \d+/)).toBeVisible()
  await expect(page.getByText(/Bad Case：A \d+ → B \d+/)).toBeVisible()
})

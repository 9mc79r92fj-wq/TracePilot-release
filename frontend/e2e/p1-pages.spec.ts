import { expect, test } from '@playwright/test'

/**
 * 第 43 步：Trace Comparison 与 Fault Attribution 页面验收。
 *
 * 这两个页面此前是占位页（P1-1 / P1-2：后端已实现但前端不可用）。
 * 本用例验证真实交互链路，数据来自后端真实 mock Trace（data_source=mock）。
 */

function collectErrors(page: import('@playwright/test').Page): string[] {
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

test('Trace Comparison: 选择两份 Trace → 运行对比 → 差异与观察结论', async ({ page }) => {
  const errors = collectErrors(page)
  await page.goto('/comparison')
  await expect(page.getByRole('heading', { name: /Trace Comparison/ })).toBeVisible()

  // 下拉框加载真实 Trace
  const refSelect = page.locator('#ref')
  const tgtSelect = page.locator('#tgt')
  await expect(refSelect).toBeVisible({ timeout: 20_000 })
  await expect
    .poll(async () => await refSelect.locator('option').count(), { timeout: 20_000 })
    .toBeGreaterThan(0)

  // 目标选一份「含故障」的 Trace
  const faultedValue = await tgtSelect.locator('option').evaluateAll((els) => {
    const hit = els.find((e) => (e.textContent ?? '').includes('故障:'))
    return hit ? (hit as HTMLOptionElement).value : null
  })
  expect(faultedValue, '应存在含故障的 Trace 可供对比').toBeTruthy()
  await tgtSelect.selectOption(faultedValue!)

  // 参考选一份无故障 Trace（避免与目标相同）
  const cleanValue = await refSelect.locator('option').evaluateAll((els) => {
    const hit = els.find((e) => (e.textContent ?? '').includes('无故障'))
    return hit ? (hit as HTMLOptionElement).value : null
  })
  if (cleanValue) await refSelect.selectOption(cleanValue)

  await page.getByRole('button', { name: /运行对比/ }).click()

  // 结果区
  await expect(page.getByText('Trace 概览对照')).toBeVisible({ timeout: 60_000 })
  await expect(page.getByText('顶层差异')).toBeVisible()
  await expect(page.getByText('步骤匹配与未匹配')).toBeVisible()

  // 观察/推断/未验证三方严格区分
  await expect(page.getByText(/观察（observed）/)).toBeVisible()
  await expect(page.getByText(/推断（inferred，非因果）/)).toBeVisible()
  await expect(page.getByText(/未验证（unverified）/)).toBeVisible()

  // 有对照后应识别出差异节点或明确说明无差异
  const diffCard = page.getByText(/差异节点（\d+）/)
  await expect(diffCard).toBeVisible()

  // 跳转 DAG
  await page.getByRole('button', { name: /查看该 Trace DAG/ }).first().click()
  await expect(page).toHaveURL(/\/dag\/trace-/, { timeout: 20_000 })

  expect(serious(errors)).toEqual([])
})

test('Trace Comparison: 相同 Trace 对比给出明确拒绝', async ({ page }) => {
  await page.goto('/comparison')
  const refSelect = page.locator('#ref')
  await expect
    .poll(async () => await refSelect.locator('option').count(), { timeout: 20_000 })
    .toBeGreaterThan(0)
  const first = await refSelect.locator('option').first().getAttribute('value')
  expect(first).toBeTruthy()
  await refSelect.selectOption(first!)
  await expect(refSelect).toHaveValue(first!)
  await page.locator('#tgt').selectOption(first!)
  await expect(page.locator('#tgt')).toHaveValue(first!)
  await page.getByRole('button', { name: /运行对比/ }).click()
  await expect(page.getByText(/不能相同/)).toBeVisible({ timeout: 20_000 })
})

test('Fault Attribution: 故障 Trace + 基线 → 节点分类与结论口径', async ({ page }) => {
  const errors = collectErrors(page)
  await page.goto('/attribution')
  await expect(page.getByRole('heading', { name: /Fault Attribution/ })).toBeVisible()

  const traceSelect = page.locator('#at')
  await expect(traceSelect).toBeVisible({ timeout: 20_000 })
  await expect
    .poll(async () => await traceSelect.locator('option').count(), { timeout: 20_000 })
    .toBeGreaterThan(0)

  // 选含故障 Trace
  const faultedValue = await traceSelect.locator('option').evaluateAll((els) => {
    const hit = els.find((e) => (e.textContent ?? '').includes('故障:'))
    return hit ? (hit as HTMLOptionElement).value : null
  })
  expect(faultedValue).toBeTruthy()
  await traceSelect.selectOption(faultedValue!)

  // 基线选无故障 Trace
  const baselineSelect = page.locator('#bl')
  const cleanValue = await baselineSelect.locator('option').evaluateAll((els) => {
    const hit = els.find((e) => (e.textContent ?? '').includes('无故障'))
    return hit ? (hit as HTMLOptionElement).value : null
  })
  if (cleanValue) await baselineSelect.selectOption(cleanValue)

  await page.getByRole('button', { name: /运行传播分析/ }).click()

  // 指标卡与分类表（用 stat 卡的 .k 精确定位，避免与徽标/下拉重复匹配）
  const statKeys = page.locator('.card.stat .k')
  await expect(statKeys.filter({ hasText: '注入故障' }).first()).toBeVisible({ timeout: 60_000 })
  await expect(statKeys.filter({ hasText: '潜在受影响' }).first()).toBeVisible()
  await expect(page.getByText(/节点归因分类/)).toBeVisible()
  await expect(page.locator('table.tbl tbody tr').first()).toBeVisible()

  // 结论口径：可达≠因果 必须显式声明
  await expect(page.getByText(/可达 ≠ 因果/)).toBeVisible()
  await expect(page.getByText(/DAG 可达性与基线对比/)).toBeVisible()

  // 分类筛选可用
  const filter = page.locator('.card-head select')
  if (await filter.count()) {
    await filter.first().selectOption('confirmed_fault')
    await expect(page.locator('table.tbl tbody tr').first()).toBeVisible()
  }

  expect(serious(errors)).toEqual([])
})

test('导航：两个页面已标记为已实现（不再是占位页）', async ({ page }) => {
  await page.goto('/')
  const nav = page.locator('.sidebar .nav')
  // 不再出现「待实现」后缀
  await expect(nav.getByRole('link', { name: 'Trace Comparison', exact: true })).toBeVisible()
  await expect(nav.getByRole('link', { name: 'Fault Attribution', exact: true })).toBeVisible()
  await expect(nav.getByRole('link', { name: /待实现/ })).toHaveCount(0)

  await nav.getByRole('link', { name: 'Trace Comparison', exact: true }).click()
  await expect(page).toHaveURL(/\/comparison$/)
  await expect(page.getByText('该模块尚未实现')).toHaveCount(0)

  await nav.getByRole('link', { name: 'Fault Attribution', exact: true }).click()
  await expect(page).toHaveURL(/\/attribution$/)
  await expect(page.getByText('该模块尚未实现')).toHaveCount(0)
})

import { expect, test } from '@playwright/test'

/**
 * 第 46 步：Adversarial Evaluation Lab 浏览器验收。
 *
 * 覆盖：类型/难度选择 → 生成案例 → 选择案例运行 → 覆盖率地图 →
 * Bad Case Explorer 筛选 → 证据与 Trace/DAG 跳转 → 数据来源标记。
 */

function serious(errors: string[]): string[] {
  return errors.filter(
    (e) => !e.includes('favicon') && !e.includes('net::ERR_') && !e.includes('status of 404'),
  )
}

test('Adversarial Lab：生成 → 选择案例运行 → 覆盖率地图 → Bad Case', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console.error: ${m.text()}`)
  })

  await page.goto('/adversarial')
  await expect(page.getByRole('heading', { name: /Adversarial Evaluation Lab/ })).toBeVisible()

  // 类型与难度选择框存在（6 类型 + 3 难度）
  await expect(page.locator('input[type="checkbox"]')).toHaveCount(10, { timeout: 20_000 })

  // 小规模：1 变体、固定种子
  await page.locator('#adv-var').fill('1')
  await page.locator('#adv-seed').fill('42')

  // 1) 生成案例
  await page.getByRole('button', { name: /生成案例/ }).click()
  await expect(page.getByText(/生成结果（\d+ 例）/)).toBeVisible({ timeout: 60_000 })
  await expect(page.getByText(/数据来源/).first()).toBeVisible()

  // 2) 案例列表包含安全约束徽标
  await expect(page.locator('td .badge').filter({ hasText: 'bounded_steps' }).first()).toBeVisible()

  // 3) 运行所选（默认全选）
  await page.getByRole('button', { name: /运行所选/ }).click()

  // 4) 指标 + 覆盖率地图
  await expect(page.getByText('安全通过率', { exact: true })).toBeVisible({ timeout: 120_000 })
  await expect(page.getByText('任务成功率（独立）', { exact: true })).toBeVisible()
  await expect(page.getByText(/Attack Coverage Map/)).toBeVisible()

  // 覆盖率表格七列齐全
  for (const h of ['generated', 'executed', 'not_executed', 'safety_passed', 'safety_failed', 'unknown', 'coverage_status']) {
    await expect(page.locator('th').filter({ hasText: h }).first()).toBeVisible()
  }

  // 6 类攻击类型均出现在覆盖表中
  for (const t of [
    'prompt_injection',
    'tool_misuse',
    'permission_boundary',
    'invalid_arguments',
    'conflicting_instructions',
    'fault_recovery',
  ]) {
    await expect(page.locator('td').filter({ hasText: t }).first()).toBeVisible()
  }

  // 5) Bad Case Explorer（金丝雀负控必为 UNSAFE）
  await expect(page.getByText(/Bad Case Explorer（/)).toBeVisible()
  await expect(page.getByRole('button', { name: '证据' }).first()).toBeVisible({ timeout: 20_000 })

  // 6) 证据面板
  await page.getByRole('button', { name: '证据' }).first().click()
  await expect(page.getByText('安全约束逐条结果')).toBeVisible()
  await expect(page.getByText('任务判定（与安全分离）')).toBeVisible()
  await expect(page.getByText('关联证据')).toBeVisible()

  // 7) 跳转 Trace
  await page.getByRole('button', { name: '查看 Trace' }).first().click()
  await expect(page).toHaveURL(/\/traces\/trace-rt-/, { timeout: 20_000 })

  expect(serious(errors)).toEqual([])
})

test('Adversarial Lab：Bad Case 筛选可用（风险类型/安全状态/原因）', async ({ page }) => {
  await page.goto('/adversarial')
  await expect(page.locator('#adv-var')).toBeVisible({ timeout: 20_000 })
  await page.locator('#adv-var').fill('1')
  await page.getByRole('button', { name: /生成案例/ }).click()
  await expect(page.getByText(/生成结果（\d+ 例）/)).toBeVisible({ timeout: 60_000 })
  await page.getByRole('button', { name: /运行所选/ }).click()
  await expect(page.getByText(/Bad Case Explorer（/)).toBeVisible({ timeout: 120_000 })

  // 筛选控件齐全
  await expect(page.getByLabel('按风险类型筛选')).toBeVisible()
  await expect(page.getByLabel('按执行状态筛选')).toBeVisible()
  await expect(page.getByLabel('按安全验证筛选')).toBeVisible()
  await expect(page.getByLabel('按失败原因筛选')).toBeVisible()

  // 按安全状态筛选 UNSAFE → 仍有行
  await page.getByLabel('按安全验证筛选').selectOption('UNSAFE')
  await expect(page.getByRole('button', { name: '证据' }).first()).toBeVisible()

  // 按原因筛选一个不存在的关键字 → 空状态
  await page.getByLabel('按失败原因筛选').fill('不存在的失败原因xyz')
  await expect(page.getByText(/无匹配的 Bad Case/)).toBeVisible({ timeout: 10_000 })
})

test('Adversarial Lab：跳转 DAG 可用', async ({ page }) => {
  await page.goto('/adversarial')
  await expect(page.locator('#adv-var')).toBeVisible({ timeout: 20_000 })
  await page.locator('#adv-var').fill('1')
  await page.getByRole('button', { name: /生成案例/ }).click()
  await expect(page.getByText(/生成结果（\d+ 例）/)).toBeVisible({ timeout: 60_000 })
  await page.getByRole('button', { name: /运行所选/ }).click()
  await expect(page.getByText(/Bad Case Explorer（/)).toBeVisible({ timeout: 120_000 })

  await page.getByRole('button', { name: 'DAG' }).first().click()
  await expect(page).toHaveURL(/\/dag\/trace-rt-/, { timeout: 20_000 })
  await expect(page.getByRole('heading', { name: /DAG Analysis/ })).toBeVisible()
})

test('导航：Adversarial Lab 入口可达', async ({ page }) => {
  await page.goto('/')
  const nav = page.locator('.sidebar .nav')
  await nav.getByRole('link', { name: 'Adversarial Lab', exact: true }).click()
  await expect(page).toHaveURL(/\/adversarial$/)
  await expect(page.getByRole('heading', { name: /Adversarial Evaluation Lab/ })).toBeVisible()
})

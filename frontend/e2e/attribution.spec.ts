import { expect, test } from '@playwright/test'

/**
 * 第 47 步：Automated Bad Case Attribution 浏览器验收。
 *
 * 覆盖：评测中心批量归因 → 单案例归因抽屉 → 证据链分层
 * （直接证据/观察差异/依赖关系/干预证据/未知）→ Trace/DAG 跳转 → 失败指标展示。
 */

function serious(errors: string[]): string[] {
  return errors.filter(
    (e) => !e.includes('favicon') && !e.includes('net::ERR_') && !e.includes('status of 404'),
  )
}

async function runFaultEval(page: import('@playwright/test').Page) {
  await page.goto('/evaluation')
  await expect(page.getByRole('heading', { name: /Evaluation Center/ })).toBeVisible()
  // 选择受控失败数据集
  const dsSelect = page.locator('select').first()
  await expect(dsSelect).toBeVisible({ timeout: 20_000 })
  await dsSelect.selectOption('tracepilot-fault-v1')
  await page.getByRole('button', { name: /运行评测/ }).click()
  await expect(page.getByText('任务成功率')).toBeVisible({ timeout: 120_000 })
  await expect(page.getByText('案例结果 / Bad Case')).toBeVisible()
}

test('归因：批量归因面板可运行并展示证据等级分布', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console.error: ${m.text()}`)
  })

  await runFaultEval(page)

  // 批量归因卡片
  await expect(page.getByText(/自动归因（Automated Bad Case Attribution）/)).toBeVisible()
  await page.getByRole('button', { name: /^运行归因$/ }).click()

  // 结果：Bad Case 数 + 已归因数 + 证据等级徽标
  // 用 .badge.muted 精确定位批量归因面板的统计徽标：失败模式卡片中的
  // 「统计分母 … Bad Case 6」是同文本，会被 /Bad Case \d+/ 误匹配。
  await expect(
    page.locator('.badge.muted').filter({ hasText: /Bad Case \d+/ }).first(),
  ).toBeVisible({ timeout: 120_000 })
  await expect(page.getByText(/已归因 \d+/)).toBeVisible()
  // 证据等级徽标（用 badge 定位，避免字符间距/multi-text-node 造成的精确匹配问题）
  await expect(
    page.locator('.badge').filter({ hasText: '直接证据' }).first(),
  ).toBeVisible({ timeout: 30_000 })

  // 归因结果表含异常类型
  await expect(page.locator('td .badge').filter({ hasText: 'TOOL_TIMEOUT' }).first()).toBeVisible()

  expect(serious(errors)).toEqual([])
})

test('归因：单案例抽屉展示证据图与失败指标', async ({ page }) => {
  await runFaultEval(page)

  // 打开 Bad Case 证据抽屉
  await page.getByRole('button', { name: /证据/ }).first().click()
  const drawer = page.locator('.drawer')
  await expect(drawer).toBeVisible()

  // 点击「自动归因」
  await drawer.getByRole('button', { name: '自动归因' }).click()
  await expect(page.getByText(/自动归因 ·/)).toBeVisible({ timeout: 60_000 })

  // 失败指标 + 异常事件 + 证据图
  await expect(page.getByText(/失败指标（/)).toBeVisible()
  await expect(page.getByText(/异常事件（/)).toBeVisible()
  await expect(page.getByText('归因证据图')).toBeVisible()

  // 证据等级分层标签
  await expect(page.getByText(/直接证据（Trace 记录）/)).toBeVisible()
  await expect(page.getByText(/依赖关系（DAG 可达，不构成因果）/)).toBeVisible()

  // 候选归因节点
  await expect(page.locator('.attr-node').first()).toBeVisible()
  // 依据与不确定性必须展示（不伪造置信概率）
  await expect(page.getByText(/依据：/).first()).toBeVisible()

  // 跳转 DAG
  await page.getByRole('button', { name: '查看 DAG' }).first().click()
  await expect(page).toHaveURL(/\/dag\/trace-/, { timeout: 20_000 })
  await expect(page.getByRole('heading', { name: /DAG Analysis/ })).toBeVisible()
})

test('归因：普通（非 Bad Case）不显示归因按钮', async ({ page }) => {
  // 使用全通过数据集
  await page.goto('/evaluation')
  const dsSelect = page.locator('select').first()
  await expect(dsSelect).toBeVisible({ timeout: 20_000 })
  await dsSelect.selectOption('tracepilot-eval-v1')
  await page.locator('#mc').fill('3')
  await page.getByRole('button', { name: /运行评测/ }).click()
  await expect(page.getByText('案例结果 / Bad Case')).toBeVisible({ timeout: 120_000 })

  await page.getByRole('button', { name: /证据/ }).first().click()
  const drawer = page.locator('.drawer')
  await expect(drawer).toBeVisible()
  await expect(drawer.getByRole('button', { name: '自动归因' })).toHaveCount(0)
})

test('归因：批量面板在无 Bad Case 时给出明确说明', async ({ page }) => {
  await page.goto('/evaluation')
  const dsSelect = page.locator('select').first()
  await expect(dsSelect).toBeVisible({ timeout: 20_000 })
  await dsSelect.selectOption('tracepilot-eval-v1')
  await page.locator('#mc').fill('2')
  await page.getByRole('button', { name: /运行评测/ }).click()
  await expect(page.getByText('案例结果 / Bad Case')).toBeVisible({ timeout: 120_000 })

  await page.getByRole('button', { name: /^运行归因$/ }).click()
  await expect(page.getByText(/已归因 0|没有 Bad Case/).first()).toBeVisible({ timeout: 60_000 })
})

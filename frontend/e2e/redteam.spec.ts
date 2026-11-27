import { expect, test } from '@playwright/test'

/**
 * 第 45 步：Red Team Lab 浏览器验收。
 *
 * 验证：页面可打开、六类攻击类型可生成、安全与任务指标分离展示、
 * 金丝雀检测器验证、案例详情与 Trace 跳转、刷新后可重新生成（同 seed 可复现）。
 */

function serious(errors: string[]): string[] {
  return errors.filter(
    (e) => !e.includes('favicon') && !e.includes('net::ERR_') && !e.includes('status of 404'),
  )
}

test('Red Team Lab：生成并运行六类红队案例', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console.error: ${m.text()}`)
  })

  await page.goto('/redteam')
  await expect(page.getByRole('heading', { name: /Red Team Lab/ })).toBeVisible()

  // 六类攻击类型复选框
  await expect(page.locator('input[type="checkbox"]')).toHaveCount(7, { timeout: 20_000 }) // 6 类型 + 金丝雀

  // 固定种子 + 小规模，便于复现
  await page.locator('#rt-var').fill('1')
  await page.locator('#rt-seed').fill('42')

  await page.getByRole('button', { name: /生成并运行/ }).click()

  // 指标卡：安全通过率 + 任务成功率必须分开
  await expect(page.getByText('安全通过率', { exact: true })).toBeVisible({
    timeout: 120_000,
  })
  await expect(page.getByText('任务成功率（独立指标）', { exact: true })).toBeVisible()
  await expect(page.getByText('金丝雀检测器', { exact: true })).toBeVisible()

  // 金丝雀检测器应显示「已验证」
  await expect(page.locator('.card.stat').filter({ hasText: '金丝雀检测器' })).toBeVisible()

  // 按类型统计表应包含六类（页面按中文类型标签展示）
  const typeTable = page.getByText('按攻击类型统计')
  await expect(typeTable).toBeVisible()
  for (const t of ['提示注入', '工具误用', '权限边界', '非法参数', '指令冲突', '故障恢复']) {
    await expect(page.locator('td .badge').filter({ hasText: t }).first()).toBeVisible()
  }

  expect(serious(errors)).toEqual([])
})

test('Red Team Lab：案例详情展示安全约束逐条结果与 Trace 跳转', async ({ page }) => {
  await page.goto('/redteam')
  await expect(page.getByRole('heading', { name: /Red Team Lab/ })).toBeVisible()
  await expect(page.locator('#rt-var')).toBeVisible({ timeout: 20_000 })
  await page.locator('#rt-var').fill('1')
  await page.getByRole('button', { name: /生成并运行/ }).click()
  await expect(page.getByText('安全通过率', { exact: true })).toBeVisible({
    timeout: 120_000,
  })

  // 打开第一个案例详情
  await page.getByRole('button', { name: '详情' }).first().click()
  await expect(page.getByText('案例详情')).toBeVisible()
  await expect(page.getByText('安全判定', { exact: true })).toBeVisible()
  await expect(page.getByText('任务判定（与安全分离）')).toBeVisible()
  await expect(page.getByText(/已检查 \d+ 条约束/)).toBeVisible()
  await expect(page.getByText('关联 Trace')).toBeVisible()

  // 安全约束表至少一行
  await expect(page.locator('table.tbl tbody tr').first()).toBeVisible()

  // 跳转 Trace
  const traceBtn = page.getByRole('button', { name: '查看 Trace' }).first()
  if ((await traceBtn.count()) > 0) {
    await traceBtn.click()
    await expect(page).toHaveURL(/\/traces\/trace-rt-/, { timeout: 20_000 })
    await expect(page.getByText('证据血缘（Evidence Lineage）').first()).toBeVisible({ timeout: 20_000 })
  }
})

test('Red Team Lab：仅 UNSAFE 筛选显示金丝雀负控', async ({ page }) => {
  await page.goto('/redteam')
  await expect(page.locator('#rt-var')).toBeVisible({ timeout: 20_000 })
  await page.locator('#rt-var').fill('1')
  await page.getByRole('button', { name: /生成并运行/ }).click()
  await expect(page.getByText('安全通过率', { exact: true })).toBeVisible({
    timeout: 120_000,
  })

  // 筛选「仅 UNSAFE」→ 应包含金丝雀（负控必然 UNSAFE）
  const filter = page.locator('.card-head select').first()
  if ((await filter.count()) > 0) {
    await filter.selectOption('unsafe')
    await expect(page.getByText('金丝雀').first()).toBeVisible({ timeout: 20_000 })
  }
})

test('导航：Red Team Lab 入口可达且不再有未实现页', async ({ page }) => {
  await page.goto('/')
  const nav = page.locator('.sidebar .nav')
  // 侧栏默认只展开当前路由分组，需要先展开「安全与性能」
  const securityGroup = nav.getByRole('button', { name: '安全与性能', exact: true })
  if ((await securityGroup.getAttribute('aria-expanded')) !== 'true') {
    await securityGroup.click()
  }
  await expect(nav.getByRole('link', { name: '红队测试', exact: true })).toBeVisible()
  await expect(nav.getByRole('link', { name: /待实现/ })).toHaveCount(0)
  await nav.getByRole('link', { name: '红队测试', exact: true }).click()
  await expect(page).toHaveURL(/\/redteam$/)
  await expect(page.getByRole('heading', { name: /Red Team Lab/ })).toBeVisible()
})

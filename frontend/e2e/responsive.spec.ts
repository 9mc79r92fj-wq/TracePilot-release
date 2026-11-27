import { expect, test } from '@playwright/test'

/**
 * 响应式布局与 UI 验收：PC(1440) / 平板(834) / 手机(390)。
 *
 * 检查项：
 * 1. 导航可用（手机端侧栏应被隐藏并可展开）
 * 2. 表格是否溢出
 * 3. 按钮可点击
 * 4. 文字可阅读（字号）
 * 5. 弹窗不超屏
 * 6. DAG 可缩放
 * 7. 无横向异常滚动
 */

const PAGES = [
  { path: '/', name: 'Overview' },
  { path: '/traces', name: 'Trace Explorer' },
  { path: '/dag', name: 'DAG Analysis' },
  { path: '/runs', name: 'Agent Runs' },
  { path: '/fault-lab', name: 'Fault Lab' },
  { path: '/counterfactual', name: 'Counterfactual Lab' },
  { path: '/repair', name: 'Repair Studio' },
  { path: '/evaluation', name: 'Evaluation Center' },
  { path: '/calibration', name: 'Judge Calibration' },
  { path: '/multi-judge', name: 'Disagreement Explorer' },
  { path: '/architecture', name: 'Architecture' },
  { path: '/comparison', name: 'Trace Comparison' },
  { path: '/attribution', name: 'Fault Attribution' },
  { path: '/redteam', name: 'Red Team Lab' },
  { path: '/adversarial', name: 'Adversarial Lab' },
  { path: '/version-compare', name: 'Version Comparison' },
  { path: '/pipelines', name: 'Evaluation Pipeline' },
  { path: '/costs', name: 'Cost Analytics' },
  { path: '/pareto', name: 'Pareto Explorer' },
]

const VIEWPORTS = [
  { name: 'PC', width: 1440, height: 900 },
  { name: 'Tablet', width: 834, height: 1112 },
  { name: 'Mobile', width: 390, height: 844 },
]

for (const vp of VIEWPORTS) {
  test(`响应式 ${vp.name}(${vp.width}x${vp.height}): 无横向溢出且内容可读`, async ({ page }) => {
    await page.setViewportSize({ width: vp.width, height: vp.height })

    for (const p of PAGES) {
      await page.goto(p.path, { waitUntil: 'domcontentloaded' })
      // 等待主内容真正渲染（数据量增长后列表可能较慢，不能只等容器出现）
      await expect(page.locator('main')).toBeVisible({ timeout: 30_000 })
      await expect
        .poll(async () => (await page.locator('main').innerText()).trim().length, {
          timeout: 60_000,
        })
        .toBeGreaterThan(10)
      await page.waitForTimeout(200)

      // 1) 无横向异常滚动（允许 2px 容差）
      const overflow = await page.evaluate(() => {
        const doc = document.documentElement
        return {
          scrollW: doc.scrollWidth,
          clientW: doc.clientWidth,
          bodyOverflowX: getComputedStyle(document.body).overflowX,
        }
      })
      expect(
        overflow.scrollW - overflow.clientW,
        `${vp.name} ${p.name} 横向溢出 ${overflow.scrollW - overflow.clientW}px`,
      ).toBeLessThanOrEqual(2)

      // 2) 文字可读：主区域字号不小于 11px
      // 用轮询而非单次测量：数据量增长后首屏可能仍在渲染，单次测量会误得 0
      const measureMinFont = () =>
        page.evaluate(() => {
          const main = document.querySelector('main')
          if (!main) return 0
          const sizes: number[] = []
          main.querySelectorAll('p, td, th, li, span, div').forEach((el) => {
            const txt = (el.textContent ?? '').trim()
            if (!txt || el.children.length > 0) return
            const fs = parseFloat(getComputedStyle(el).fontSize || '0')
            if (fs > 0) sizes.push(fs)
          })
          return sizes.length ? Math.min(...sizes) : 0
        })

      await expect
        .poll(measureMinFont, { timeout: 60_000, message: `${vp.name} ${p.name} 字号测量` })
        .toBeGreaterThan(0)

      const minFont = await measureMinFont()
      expect(minFont, `${vp.name} ${p.name} 最小字号 ${minFont}px`).toBeGreaterThanOrEqual(11)

      // 3) 页面无白屏
      const len = (await page.locator('main').innerText()).trim().length
      expect(len, `${vp.name} ${p.name} 内容为空`).toBeGreaterThan(10)
    }
  })
}

test('手机端：侧栏默认收起，汉堡按钮可展开导航', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  const sidebar = page.locator('.sidebar')
  // 移动端侧栏应被移出视口（transform: translateX(-100%)）
  const boxBefore = await sidebar.boundingBox()
  expect(boxBefore === null || boxBefore.x + boxBefore.width <= 1).toBeTruthy()

  await page.getByRole('button', { name: '菜单' }).click()
  await expect(sidebar).toHaveClass(/open/)
  await page.waitForTimeout(300)
  const boxAfter = await sidebar.boundingBox()
  expect(boxAfter?.x ?? -1).toBeGreaterThanOrEqual(0)

  // 点击导航项后可跳转且侧栏关闭
  await sidebar.getByRole('button', { name: 'Agent 执行与轨迹', exact: true }).click()
  await sidebar.getByRole('link', { name: 'Agent 运行', exact: true }).click()
  await expect(page).toHaveURL(/\/runs$/)
  await expect(page.getByRole('heading', { name: /Agent Runs/ })).toBeVisible()
})

test('手机端：表格在容器内横向滚动而不撑破页面', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/traces')
  // Trace 列表数据量大（数千条），完整测试套件下渲染较慢，给足等待时间
  await expect(page.locator('table.tbl').first()).toBeVisible({ timeout: 60_000 })
  const doc = await page.evaluate(() => ({
    scrollW: document.documentElement.scrollWidth,
    clientW: document.documentElement.clientWidth,
  }))
  expect(doc.scrollW - doc.clientW).toBeLessThanOrEqual(2)
})

test('DAG 画布：缩放控件可用且节点可见', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/dag')
  await expect(page.locator('.react-flow__node').first()).toBeVisible({ timeout: 25_000 })

  const before = await page.locator('.react-flow__node').first().boundingBox()
  await page.locator('.react-flow__controls-zoomin').click()
  await page.waitForTimeout(400)
  const after = await page.locator('.react-flow__node').first().boundingBox()
  expect(before && after).toBeTruthy()
  // 放大后节点尺寸应变化（至少宽度变化 > 1px）
  expect(Math.abs((after?.width ?? 0) - (before?.width ?? 0))).toBeGreaterThan(1)

  // 适应视图按钮存在
  await expect(page.locator('.react-flow__controls-fitview')).toBeVisible()
})

test('弹窗（抽屉）不超出屏幕', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/evaluation')
  await expect(page.getByRole('heading', { name: /Evaluation Center/ })).toBeVisible()

  // 运行小规模评测以产生 Bad Case / 案例表
  await page.locator('#mc').fill('2')
  await page.getByRole('button', { name: /运行评测/ }).click()
  await expect(page.getByText('案例结果 / Bad Case')).toBeVisible({ timeout: 120_000 })

  await page.getByRole('button', { name: /证据/ }).first().click()
  const drawer = page.locator('.drawer')
  await expect(drawer).toBeVisible()
  const box = await drawer.boundingBox()
  const vp = page.viewportSize()!
  expect(box).toBeTruthy()
  expect(box!.x).toBeGreaterThanOrEqual(-1)
  expect(box!.x + box!.width).toBeLessThanOrEqual(vp.width + 1)
})

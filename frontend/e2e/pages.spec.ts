import { expect, Page, test } from '@playwright/test'

/**
 * 全站页面冒烟验收：每个路由都能打开、无白屏、无严重 JS 异常。
 *
 * 真实页面清单（依据 src/App.tsx）：
 *   /        Overview
 *   /traces  Trace Explorer
 *   /dag     DAG Analysis
 *   /runs    Agent Runs
 *   /fault-lab          Fault Lab (Failure Replay)
 *   /counterfactual     Counterfactual Lab
 *   /repair             Repair Studio
 *   /evaluation         Evaluation Center
 *   /calibration        Judge Calibration
 *   /multi-judge        Disagreement Explorer
 *   /architecture       Architecture
 *   /comparison         Trace Comparison（第 43 步实现）
 *   /attribution        Fault Attribution（第 43 步实现）
 *   /redteam            Red Team Lab（第 45 步实现）
 *   /adversarial        Adversarial Evaluation Lab（第 46 步实现）
 *   /version-compare    Version Comparison / Regression（第 49/50 步实现）
 *   /pipelines          Evaluation Pipeline（第 52 步实现）
 *   /costs              Cost Analytics（第 53 步实现）
 *   /pareto             Pareto Explorer（第 54 步实现）
 *
 * 第 53 步起：全部页面均为真实实现，无占位页（当前 19 个）。
 */

const REAL_PAGES: { path: string; marker: string }[] = [
  { path: '/', marker: 'Overview' },
  { path: '/traces', marker: 'Trace Explorer' },
  { path: '/dag', marker: 'DAG Analysis' },
  { path: '/runs', marker: 'Agent Runs' },
  { path: '/fault-lab', marker: 'Fault Lab' },
  { path: '/counterfactual', marker: 'Counterfactual Lab' },
  { path: '/repair', marker: 'Repair Studio' },
  { path: '/evaluation', marker: 'Evaluation Center' },
  { path: '/calibration', marker: 'Judge Calibration' },
  { path: '/multi-judge', marker: 'Disagreement Explorer' },
  { path: '/architecture', marker: 'Architecture' },
  { path: '/comparison', marker: 'Trace Comparison' },
  { path: '/attribution', marker: 'Fault Attribution' },
  { path: '/redteam', marker: 'Red Team Lab' },
  { path: '/adversarial', marker: 'Adversarial Evaluation Lab' },
  { path: '/version-compare', marker: 'Version Comparison' },
  { path: '/pipelines', marker: 'Evaluation Pipeline' },
  { path: '/costs', marker: 'Cost Analytics' },
  { path: '/pareto', marker: 'Pareto Explorer' },
]

const PLACEHOLDER_PAGES: string[] = []

/** 收集页面 JS 异常与 console error。 */
function attachErrorCollector(page: Page): string[] {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(`console.error: ${msg.text()}`)
  })
  return errors
}

/** 过滤掉与业务无关的噪音（如后端未启动时的网络错误由具体断言处理）。 */
function seriousErrors(errors: string[]): string[] {
  return errors.filter(
    (e) =>
      !e.includes('favicon') &&
      !e.includes('Failed to load resource: the server responded with a status of 404') &&
      !e.includes('net::ERR_'),
  )
}

for (const { path, marker } of REAL_PAGES) {
  test(`页面可打开且无白屏: ${path}`, async ({ page }) => {
    const errors = attachErrorCollector(page)
    const resp = await page.goto(path, { waitUntil: 'domcontentloaded' })
    expect(resp?.status(), `${path} HTTP 状态`).toBeLessThan(400)

    // 不白屏：body 有实质内容
    const bodyText = (await page.locator('body').innerText()).trim()
    expect(bodyText.length, `${path} 内容长度`).toBeGreaterThan(10)

    // 侧边栏与页面标题存在
    await expect(page.locator('.sidebar')).toBeVisible()
    await expect(page.getByRole('heading', { name: new RegExp(marker) }).first()).toBeVisible({
      timeout: 20_000,
    })

    expect(seriousErrors(errors), `${path} 严重 JS 异常`).toEqual([])
  })

  test(`页面刷新不 404: ${path}`, async ({ page }) => {
    await page.goto(path, { waitUntil: 'domcontentloaded' })
    await page.reload({ waitUntil: 'domcontentloaded' })
    const resp = await page.goto(path, { waitUntil: 'domcontentloaded' })
    expect(resp?.status()).toBeLessThan(400)
    const bodyText = (await page.locator('body').innerText()).trim()
    expect(bodyText.length).toBeGreaterThan(10)
  })
}

for (const path of PLACEHOLDER_PAGES) {
  test(`占位页明确标注未实现: ${path}`, async ({ page }) => {
    await page.goto(path)
    await expect(page.getByText('该模块尚未实现')).toBeVisible()
    // 占位页必须明示，不能伪装成真实功能
    await expect(page.getByText('不会用假数据冒充真实功能')).toBeVisible()
  })
}

test('侧边栏导航可跳转且高亮正确', async ({ page }) => {
  await page.goto('/')
  const nav = page.locator('.sidebar .nav')
  await expect(nav).toBeVisible()

  // 逐个点击真实页面导航
  const items = [
    { group: 'Agent执行与轨迹', label: 'Agent运行', path: '/runs', heading: 'Agent Runs' },
    { group: 'Agent执行与轨迹', label: '轨迹浏览', path: '/traces', heading: 'Trace Explorer' },
    { group: 'Agent执行与轨迹', label: 'DAG分析', path: '/dag', heading: 'DAG Analysis' },
    { group: '故障诊断', label: '故障实验室', path: '/fault-lab', heading: 'Fault Lab' },
    { group: '故障诊断', label: '反事实分析', path: '/counterfactual', heading: 'Counterfactual Lab' },
    { group: '故障诊断', label: '修复工作台', path: '/repair', heading: 'Repair Studio' },
    { group: '评测中心', label: '评测总览', path: '/evaluation', heading: 'Evaluation Center' },
    { group: '评测中心', label: '版本对比', path: '/version-compare', heading: 'Version Comparison' },
    { group: '评测中心', label: '评测流水线', path: '/pipelines', heading: 'Evaluation Pipeline' },
    { group: '安全与性能', label: '成本分析', path: '/costs', heading: 'Cost Analytics' },
    { group: '安全与性能', label: 'Pareto分析', path: '/pareto', heading: 'Pareto Explorer' },
    { group: '评测中心', label: 'Judge校准', path: '/calibration', heading: 'Judge Calibration' },
    { group: '评测中心', label: '多Judge分歧', path: '/multi-judge', heading: 'Disagreement Explorer' },
    { group: '故障诊断', label: '轨迹对比', path: '/comparison', heading: 'Trace Comparison' },
    { group: '故障诊断', label: '故障归因', path: '/attribution', heading: 'Fault Attribution' },
    { group: '安全与性能', label: '红队测试', path: '/redteam', heading: 'Red Team Lab' },
    { group: '安全与性能', label: '对抗评测', path: '/adversarial', heading: 'Adversarial Evaluation Lab' },
    { group: '系统', label: '系统架构', path: '/architecture', heading: 'Architecture' },
    { group: '工作台', label: '总览', path: '/', heading: 'Overview' },
  ]

  for (const item of items) {
    // 展开所在分组（默认只展开当前路由所在分组）
    const groupTrigger = nav.getByRole('button', { name: item.group, exact: true })
    if ((await groupTrigger.getAttribute('aria-expanded')) !== 'true') {
      await groupTrigger.click()
    }
    await nav.getByRole('link', { name: item.label, exact: true }).click()
    await expect(page).toHaveURL(new RegExp(`${item.path === '/' ? '/$' : item.path}$`))
    await expect(page.getByRole('heading', { name: new RegExp(item.heading) }).first()).toBeVisible()
    // 当前项应高亮
    await expect(nav.locator('.nav-item.active')).toContainText(item.label)
  }
})

test('侧边栏可折叠', async ({ page }) => {
  await page.goto('/')
  const sidebar = page.locator('.sidebar')
  await expect(sidebar).not.toHaveClass(/collapsed/)

  // 折叠：按钮的可访问名称应为「收起侧栏」
  await page.getByRole('button', { name: '收起侧栏' }).click()
  await expect(sidebar).toHaveClass(/collapsed/)

  // 折叠后按钮仍必须有可访问名称（否则键盘/读屏用户无法恢复）
  const expandBtn = page.getByRole('button', { name: '展开侧栏' })
  await expect(expandBtn).toBeVisible()
  await expect(expandBtn).toHaveAttribute('aria-expanded', 'false')
  await expandBtn.click()
  await expect(sidebar).not.toHaveClass(/collapsed/)
})

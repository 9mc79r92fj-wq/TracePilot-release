import { expect, Page, test } from '@playwright/test'

/**
 * 6 大核心业务流程浏览器验收。
 *
 * 全部使用 Mock / 模拟模式（禁止付费 API）：
 * - Agent 运行使用 mock 模式
 * - 评测、Judge、校准、仲裁均默认 Mock
 *
 * 允许使用真实本地后端数据（Trace 为 mock 运行产物，明确标记 data_source=mock）。
 */

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

// ---------------------------------------------------------------- A. Agent 运行

test('流程A: Agent 运行（mock）→ 结果 → Trace → DAG', async ({ page }) => {
  const errors = collectErrors(page)
  await page.goto('/runs')
  await expect(page.getByRole('heading', { name: /Agent Runs/ })).toBeVisible()

  // 后端能力状态加载（真实 API 请求）：预算由 /api/real-api/budget 异步返回
  await expect(page.getByText(/剩余 \/ 上限：/)).toBeVisible({ timeout: 20_000 })

  // 输入任务
  const textarea = page.locator('textarea').first()
  await textarea.fill('计算125乘以48')

  // 提交执行（默认 mock 模式）
  await page.getByRole('button', { name: /运行任务/ }).click()

  // 结果出现：数据来源徽标 + 最终回答
  await expect(page.getByText('执行结果')).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText(/MOCK · 模拟模型/).first()).toBeVisible()
  await expect(page.getByText(/计算结果为 6000/)).toBeVisible({ timeout: 20_000 })

  // 工具调用详情
  await expect(page.getByText('工具调用（1）')).toBeVisible()
  await expect(page.getByText(/125\*48/).first()).toBeVisible()

  // 打开 Trace
  await page.getByRole('button', { name: /查看 Trace/ }).click()
  await expect(page).toHaveURL(/\/traces\/trace-/, { timeout: 20_000 })
  await expect(page.getByRole('heading', { name: /Trace 详情/ })).toBeVisible()
  // 只匹配概览中的字段名，避免与证据血缘面板的「origin 表示数据来源…」说明冲突
  await expect(page.getByText('数据来源', { exact: true })).toBeVisible()
  await expect(page.getByText(/MOCK \/ 模拟/).first()).toBeVisible()

  // 打开 DAG
  await page.getByRole('button', { name: /查看 DAG/ }).first().click()
  await expect(page).toHaveURL(/\/dag\/trace-/, { timeout: 20_000 })
  await expect(page.getByRole('heading', { name: /DAG Analysis/ })).toBeVisible()
  await expect(page.locator('.react-flow__node').first()).toBeVisible({ timeout: 20_000 })

  expect(serious(errors)).toEqual([])
})

// ---------------------------------------------------------------- B. 故障分析

test('流程B: 故障分析（Trace → 故障 → 传播 → 对比）', async ({ page }) => {
  const errors = collectErrors(page)
  // 1) Trace Explorer 选择 Trace
  await page.goto('/traces')
  await expect(page.getByRole('heading', { name: /Trace Explorer/ })).toBeVisible()
  const rows = page.locator('table.tbl tbody tr')
  // Trace 数量增长 + 全站并发验收时列表接口可能较慢；显式放宽等待上限
  await expect(rows.first()).toBeVisible({ timeout: 60_000 })
  await rows.first().click()
  await expect(page.getByRole('heading', { name: /Trace 详情/ })).toBeVisible({ timeout: 20_000 })

  // 2) 查看故障信息（故障类型标签或步骤表）
  // 步骤表标题带数量，避免与证据血缘面板中的「执行步骤」分组标题冲突
  await expect(page.getByText(/执行步骤（\d+）/)).toBeVisible()

  // 3) 传播分析：Fault Lab 的 Recorded Replay 是只读重放，展示故障位置
  await page.goto('/fault-lab')
  await expect(page.getByRole('heading', { name: /故障重放/ })).toBeVisible()
  await expect(page.getByText(/重放配置/)).toBeVisible({ timeout: 20_000 })
  await page.getByRole('button', { name: /开始重放/ }).click()
  await expect(page.getByText(/重放结果/)).toBeVisible({ timeout: 30_000 })

  // 4) 对比结果：DAG 页面同时可对比正常/故障 Trace 的结构差异（此处验证 DAG 可切换 Trace）
  await page.goto('/dag')
  await expect(page.getByRole('heading', { name: /DAG Analysis/ })).toBeVisible()
  const selector = page.locator('.dag-toolbar select').first()
  await expect(selector).toBeVisible({ timeout: 20_000 })
  await expect
    .poll(async () => await selector.locator('option').count(), { timeout: 20_000 })
    .toBeGreaterThan(1)
  expect(serious(errors)).toEqual([])
})

// ---------------------------------------------------------------- C. 反事实实验

test('流程C: 反事实实验（选 Trace → 干预 → 受控实验 → 结果）', async ({ page }) => {
  const errors = collectErrors(page)
  await page.goto('/counterfactual')
  await expect(page.getByRole('heading', { name: /Counterfactual Lab/ })).toBeVisible()

  // 等待 Trace 列表加载（用 aria-label 精确锁定「原始故障 Trace」下拉框）
  const traceSelect = page.getByLabel('选择 Trace')
  await expect(traceSelect).toBeVisible({ timeout: 20_000 })
  await expect
    .poll(async () => await traceSelect.locator('option').count(), { timeout: 20_000 })
    .toBeGreaterThan(0)

  // 页面会自动选中一条「反事实资格可用」的故障 Trace（calculator 工具故障；
  // 当前数据由 scripts/create_mock_failure_trace.py 生成的受控失败 Trace 提供），
  // 资格接口返回前按钮 disabled，因此以按钮可用作为资格就绪信号。
  const runBtn = page.getByRole('button', { name: '运行实验', exact: true })
  await expect(runBtn).toBeEnabled({ timeout: 30_000 })
  await expect(traceSelect.locator('option:checked')).toContainText('故障:')

  // 配置干预：remove_fault（默认）
  await expect(page.getByText(/干预类型/).first()).toBeVisible()

  // 运行受控实验
  await runBtn.click()

  // 结果：实验状态 + Evidence Strength（模拟模型徽标必须出现在结果卡内）
  await expect(page.getByText('实验状态')).toBeVisible()
  const strengthCard = page.locator('.card', { hasText: 'Evidence Strength' })
  await expect(strengthCard).toBeVisible({ timeout: 60_000 })
  await expect(strengthCard.getByText('模拟模型', { exact: true })).toBeVisible()

  expect(serious(errors)).toEqual([])
})

// ---------------------------------------------------------------- D. 修复验证

test('流程D: 修复验证（候选 → 验证 → 结果）', async ({ page }) => {
  const errors = collectErrors(page)
  await page.goto('/repair')
  await expect(page.getByRole('heading', { name: /Repair Studio/ })).toBeVisible()

  // 选择一条含可修复故障类型的 Trace（tool_timeout 等）
  const traceSelect = page.locator('select').first()
  await expect(traceSelect).toBeVisible({ timeout: 20_000 })
  await expect
    .poll(async () => await traceSelect.locator('option').count(), { timeout: 20_000 })
    .toBeGreaterThan(0)
  const repairableValue = await traceSelect.locator('option').evaluateAll((els) => {
    const hit = els.find((e) =>
      /故障:.*(tool_timeout|tool_exception|invalid_arguments|permission_denied)/.test(
        e.textContent ?? '',
      ),
    )
    return hit ? (hit as HTMLOptionElement).value : null
  })
  if (repairableValue) {
    await traceSelect.selectOption(repairableValue)
  }

  // 等待候选卡片（用结构定位，避免与说明文字冲突）
  const candidateCards = page.locator('.rs-card')
  await expect(candidateCards.first()).toBeVisible({ timeout: 30_000 })

  // 选择第一个候选查看详情
  await candidateCards.first().click()
  await expect(page.getByText('候选详情')).toBeVisible()

  // 验证修复（全部候选）—— 用精确名称，避免命中真实 API 卡片里的「真实 API 修复验证」
  await page.getByRole('button', { name: '验证修复（全部候选）', exact: true }).click()
  // Repair Confidence Breakdown 现渲染为「修复候选评估」区块（真实报告字段推导，不生成虚假成功率）
  const breakdown = page.locator('.cf-note', { hasText: '修复候选评估' })
  await expect(breakdown).toBeVisible({ timeout: 120_000 })
  await expect(breakdown).toContainText('验证状态')
  await expect(breakdown.locator('tr', { hasText: '验证状态' })).toContainText(
    /已验证有效|验证未通过|未验证/,
  )
  await expect(breakdown).toContainText('以上为模拟环境下的验证元数据，不代表生产结果')

  expect(serious(errors)).toEqual([])
})

// ---------------------------------------------------------------- E. Agent 评测

test('流程E: Agent 评测（数据集 → 运行 → 指标 → Bad Case → Trace）', async ({ page }) => {
  const errors = collectErrors(page)
  await page.goto('/evaluation')
  await expect(page.getByRole('heading', { name: /Evaluation Center/ })).toBeVisible()

  // 1) 选择数据集（默认已选）
  const dsSelect = page.locator('select').first()
  await expect(dsSelect).toBeVisible({ timeout: 20_000 })
  await expect(dsSelect.locator('option').first()).toContainText('tracepilot-eval-v1')

  // 限制案例数以加速
  const maxCases = page.locator('#mc')
  await maxCases.fill('3')

  // 2) 运行评测
  await page.getByRole('button', { name: /运行评测/ }).click()

  // 3) 查看指标
  await expect(page.getByText('任务成功率')).toBeVisible({ timeout: 120_000 })
  await expect(page.getByText(/六维评测指标/)).toBeVisible()
  await expect(page.getByText('案例结果 / Bad Case')).toBeVisible()

  // 4) 查看案例证据（下钻）
  await page.getByRole('button', { name: /证据/ }).first().click()
  await expect(page.locator('.drawer')).toBeVisible()
  await expect(page.getByText(/Trace/).first()).toBeVisible()

  // 5) 从抽屉跳转 Trace
  await page.getByRole('button', { name: /查看 Trace/ }).click()
  await expect(page).toHaveURL(/\/traces\/trace-/, { timeout: 20_000 })
  await expect(page.getByRole('heading', { name: /Trace 详情/ })).toBeVisible()

  expect(serious(errors)).toEqual([])
})

// ---------------------------------------------------------------- F. Judge 评测

test('流程F1: Mock Judge 评分（Rubric → 运行 → 评分展示）', async ({ page }) => {
  const errors = collectErrors(page)
  await page.goto('/evaluation')
  await expect(page.getByRole('heading', { name: /Evaluation Center/ })).toBeVisible()

  // 选择 Mock Judge 模式
  const judgeSelect = page.locator('#jm')
  await expect(judgeSelect).toBeVisible({ timeout: 20_000 })
  await judgeSelect.selectOption('mock')

  // Rubric 选择出现
  await expect(page.locator('#jr')).toBeVisible()
  await expect(page.getByText('模拟评审')).toBeVisible()

  await page.locator('#mc').fill('2')
  await page.getByRole('button', { name: /运行评测/ }).click()

  // 查看 Judge 评分
  await expect(page.getByText(/LLM Judge 评审/)).toBeVisible({ timeout: 120_000 })
  await expect(page.getByText(/MOCK（模拟评审）/)).toBeVisible()
  await expect(page.getByText(/Rubric answer_quality/).first()).toBeVisible()

  expect(serious(errors)).toEqual([])
})

test('流程F2: Judge 校准（运行 → 一致性指标 → 混淆矩阵）', async ({ page }) => {
  const errors = collectErrors(page)
  await page.goto('/calibration')
  await expect(page.getByRole('heading', { name: /Judge Calibration/ })).toBeVisible()

  await expect(page.getByText(/synthetic_reference/).first()).toBeVisible({ timeout: 20_000 })

  await page.getByRole('button', { name: /运行校准/ }).click()

  await expect(page.getByText('完全一致率')).toBeVisible({ timeout: 120_000 })
  await expect(page.getByText(/Confusion Matrix/)).toBeVisible()
  // 断言可见的版本/来源信息（避免匹配到 <option> 这类不可见元素）
  await expect(page.getByText(/数据集 v1\.0\.0/)).toBeVisible()
  await expect(page.getByText(/synthetic_reference/).first()).toBeVisible()
  await expect(page.getByText(/Rubric answer_quality/).first()).toBeVisible()

  expect(serious(errors)).toEqual([])
})

test('流程F3: 多 Judge 仲裁（配置 → 运行 → 分歧与仲裁）', async ({ page }) => {
  const errors = collectErrors(page)
  await page.goto('/multi-judge')
  await expect(page.getByRole('heading', { name: /Disagreement Explorer/ })).toBeVisible()

  await page.locator('#mc').fill('2')
  await page.getByRole('button', { name: /运行多 Judge 评测/ }).click()

  await expect(page.getByText(/独立性评估/)).toBeVisible({ timeout: 120_000 })

  await expect(page.getByText(/mock_prompt_variants/).first()).toBeVisible({ timeout: 120_000 })
  await expect(page.getByText('分歧指标')).toBeVisible()
  await expect(page.getByText(/^仲裁结果（策略/)).toBeVisible()

  expect(serious(errors)).toEqual([])
})

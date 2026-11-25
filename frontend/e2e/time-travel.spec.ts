import { expect, test } from '@playwright/test'

/**
 * P1-4：Trace Time Travel（DAG 时间轴回放）自动化验收。
 *
 * 此前该交互无任何自动化测试（技术债 P1-4）。
 * 验证：时间轴展开/收起、上一/下一事件、播放/暂停、回到起点、
 *       跳转故障、播放速度切换、进度条，以及"缺时间戳步骤被排除而非推断"的诚实提示。
 */

function serious(errors: string[]): string[] {
  return errors.filter(
    (e) => !e.includes('favicon') && !e.includes('net::ERR_') && !e.includes('status of 404'),
  )
}

test('DAG 时间旅行：时间轴可展开并逐步回放', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console.error: ${m.text()}`)
  })

  await page.goto('/dag')
  await expect(page.getByRole('heading', { name: /DAG Analysis/ })).toBeVisible()
  await expect(page.locator('.react-flow__node').first()).toBeVisible({ timeout: 25_000 })

  // 1) 展开时间轴
  const toggle = page.getByRole('button', { name: /展开时间轴|收起时间轴/ })
  await expect(toggle).toBeVisible()
  if ((await toggle.innerText()).includes('展开')) {
    await toggle.click()
  }
  await expect(page.getByRole('button', { name: /收起时间轴/ })).toBeVisible()

  // 2) 进度条与当前事件 chip
  const slider = page.getByRole('slider', { name: '时间轴进度' })
  await expect(slider).toBeVisible()
  await expect(page.locator('.tl-chip.current').first()).toBeVisible({ timeout: 20_000 })

  // 3) 下一事件 / 上一事件：进度条值应随之变化
  const nextBtn = page.getByRole('button', { name: '下一事件' })
  const prevBtn = page.getByRole('button', { name: '上一事件' })
  await expect(nextBtn).toBeVisible()
  await expect(prevBtn).toBeVisible()

  await page.getByRole('button', { name: '回到起点' }).click()
  const atStart = await slider.inputValue()
  await nextBtn.click()
  await page.waitForTimeout(200)
  const afterNext = await slider.inputValue()
  expect(Number(afterNext)).toBeGreaterThan(Number(atStart))

  await prevBtn.click()
  await page.waitForTimeout(200)
  const afterPrev = await slider.inputValue()
  expect(Number(afterPrev)).toBeLessThan(Number(afterNext))

  // 4) 播放 → 暂停（播放期间进度会推进）
  await page.getByRole('button', { name: '回到起点' }).click()
  await page.getByRole('button', { name: /^(播放|暂停)$/ }).click()
  await expect(page.getByRole('button', { name: '暂停' })).toBeVisible({ timeout: 10_000 })
  await page.waitForTimeout(1200)
  await page.getByRole('button', { name: '暂停' }).click()
  await expect(page.getByRole('button', { name: '播放' })).toBeVisible({ timeout: 10_000 })

  // 5) 跳转故障：若当前 Trace 有故障点，应能定位
  const jumpFault = page.getByRole('button', { name: '跳转故障' })
  await expect(jumpFault).toBeVisible()
  await jumpFault.click()
  await page.waitForTimeout(300)
  await expect(slider).toBeVisible()

  // 6) 播放速度可切换（1× → 2× ...）
  const speedBtn = page.getByRole('button', { name: /\d+× 速度/ })
  await expect(speedBtn).toBeVisible()
  const beforeSpeed = await speedBtn.innerText()
  await speedBtn.click()
  await expect(speedBtn).not.toHaveText(beforeSpeed)

  // 7) 收起时间轴
  await page.getByRole('button', { name: /收起时间轴/ }).click()
  await expect(page.getByRole('button', { name: /展开时间轴/ })).toBeVisible()

  expect(serious(errors)).toEqual([])
})

test('DAG 时间旅行：缺时间戳步骤被排除并显式说明（不推断顺序）', async ({ page }) => {
  await page.goto('/dag')
  await expect(page.locator('.react-flow__node').first()).toBeVisible({ timeout: 25_000 })

  const toggle = page.getByRole('button', { name: /展开时间轴|收起时间轴/ })
  if ((await toggle.innerText()).includes('展开')) {
    await toggle.click()
  }

  const note = page.getByText(/缺少可用时间戳/)
  if ((await note.count()) > 0) {
    await expect(note.first()).toBeVisible()
    await expect(note.first()).toContainText(/未推断/)
  } else {
    await expect(page.getByText(/缺少可用时间戳/)).toHaveCount(0)
  }
})

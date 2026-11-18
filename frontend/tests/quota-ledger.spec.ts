import { expect, test } from '@playwright/test'

/**
 * API 配额消耗数据台账 UI 验收：
 * 覆盖两阶段记账、请求编号幂等、余额查询、周期指针、重启恢复视图、
 * 运行监控观测与告警回写、告警中心待办、重新进入页面结果保留。
 * 后端进程级重启持久化由 backend/tests/test_quota_ledger.py 覆盖。
 */
// worker=1 顺序执行，用模块级变量在观测用例与告警用例间共享本次调用方
let lastOverLimitCaller = ''
test.beforeEach(async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('response', (r) => {
    if (r.url().includes('/api/') && r.status() >= 400) errors.push(`${r.status()} ${r.url()}`)
  })
  // @ts-expect-error 挂到 page 上供用例末尾统一断言
  page.__quotaErrors = errors
})

async function submitConsumption(
  page: import('@playwright/test').Page,
  values: Record<string, string>,
) {
  await page.getByTestId('quota-consume-请求编号').fill(values['请求编号'])
  await page.getByTestId('quota-consume-调用方').fill(values['调用方'])
  if (values['配置编号']) await page.getByTestId('quota-consume-配置编号').fill(values['配置编号'])
  if (values['API版本']) await page.getByTestId('quota-consume-API版本').fill(values['API版本'])
  await page.getByTestId('quota-consume-消耗额度').fill(values['消耗额度'])
  await page.getByTestId('quota-consume-btn').click()
}

test('采集API：配额台账记账、幂等、两阶段回执、余额与重新进入保留', async ({ page }) => {
  // 唯一调用方，保证测试可重复运行、互不污染
  const caller = `ui-alpha-${Date.now()}`
  const req1 = `UIREQ-1-${Date.now()}`
  const req2 = `UIREQ-2-${Date.now()}`
  await page.goto('/apigate')
  await expect(page.getByTestId('quota-ledger-panel')).toBeVisible()

  // 初始余额查询
  await page.getByTestId('quota-balance-caller').fill(caller)
  await page.getByTestId('quota-balance-btn').click()
  await expect(page.getByTestId('quota-message')).toContainText('剩余额度为 1000')

  // 提交一笔消耗：待确认 + 携带请求编号/调用方/API版本
  await submitConsumption(page, {
    请求编号: req1, 调用方: caller, 配置编号: 'APIG-0001', API版本: 'v3', 消耗额度: '150',
  })
  await expect(page.getByTestId('quota-message')).toContainText('待确认')
  const row1 = page.locator('[data-testid^="quota-row-"]').filter({ hasText: req1 })
  await expect(row1).toHaveCount(1)
  await expect(row1).toHaveAttribute('data-status', '待确认')
  await expect(row1).toContainText(caller)
  await expect(row1).toContainText('v3')

  // 重复请求编号：连续送交只生成一份
  await submitConsumption(page, {
    请求编号: req1, 调用方: caller, 配置编号: 'APIG-0001', 消耗额度: '150',
  })
  await expect(page.getByTestId('quota-message')).toContainText('重复')
  await expect(page.locator('[data-testid^="quota-row-"]').filter({ hasText: req1 })).toHaveCount(1)

  // 回执确认：待确认 → 已确认
  await row1.getByTestId('quota-confirm').click()
  await expect(page.getByTestId('quota-message')).toContainText('已确认')
  await expect(page.locator('[data-testid^="quota-row-"]').filter({ hasText: req1 }))
    .toHaveAttribute('data-status', '已确认')

  // 余额反映真实消耗
  await page.getByTestId('quota-balance-btn').click()
  await expect(page.getByTestId('quota-message')).toContainText('剩余额度为 850')

  // 撤销未完成请求：预占释放
  await submitConsumption(page, {
    请求编号: req2, 调用方: caller, 配置编号: 'APIG-0001', 消耗额度: '50',
  })
  const row2 = page.locator('[data-testid^="quota-row-"]').filter({ hasText: req2 })
  await row2.getByTestId('quota-revoke').click()
  await expect(row2).toHaveAttribute('data-status', '已作废')

  // 调用链溯源
  await page.getByTestId('quota-trace-input').fill(caller)
  await expect(page.getByTestId('quota-trace-btn')).toBeVisible()
  await page.getByTestId('quota-trace-btn').click()
  await expect(page.getByTestId('quota-trace-result')).toContainText('API配置来源')
  await expect(page.getByTestId('quota-trace-result')).toContainText('台账记账')

  // 重新进入页面：台账结果保留
  await page.goto('/targetsite')
  await page.goto('/apigate')
  await expect(page.getByTestId('quota-ledger-panel')).toBeVisible()
  await expect(page.locator('[data-testid^="quota-row-"]').filter({ hasText: req1 })).toHaveCount(1)

  // 周期指针切换
  await page.getByTestId('quota-period-input').fill('2026-10')
  await page.getByTestId('quota-period-switch').click()
  await expect(page.getByTestId('quota-current-period')).toContainText('2026-10')
  await expect(page.getByTestId('quota-period-list')).toContainText('2026-10')
  // 切回默认周期，便于后续观测用例
  await page.getByTestId('quota-period-input').fill('2026-11')
  await page.getByTestId('quota-period-switch').click()
  await expect(page.getByTestId('quota-current-period')).toContainText('2026-11')

  // @ts-expect-error 读取 beforeEach 收集的浏览器错误
  expect(page.__quotaErrors).toEqual([])
})

test('运行监控：从台账观测、超限扫描幂等回写告警待办', async ({ page }) => {
  const caller = `ui-gamma-${Date.now()}`
  lastOverLimitCaller = caller
  const reqG1 = `UIREQ-G1-${Date.now()}`
  const reqG2 = `UIREQ-G2-${Date.now()}`
  // 先在采集API打满一个调用方（APIG-0002 上限 1000）
  await page.goto('/apigate')
  await submitConsumption(page, { 请求编号: reqG1, 调用方: caller, 配置编号: 'APIG-0002', 消耗额度: '600' })
  let grow = page.locator('[data-testid^="quota-row-"]').filter({ hasText: reqG1 })
  await grow.getByTestId('quota-confirm').click()
  await submitConsumption(page, { 请求编号: reqG2, 调用方: caller, 配置编号: 'APIG-0002', 消耗额度: '400' })
  grow = page.locator('[data-testid^="quota-row-"]').filter({ hasText: reqG2 })
  await grow.getByTestId('quota-confirm').click()

  await page.goto('/monitor')
  await expect(page.getByTestId('quota-monitor-panel')).toBeVisible()
  const gammaRow = page.getByTestId(`quota-obs-row-${caller}-APIG-0002`)
  await expect(gammaRow).toContainText('超限')
  await expect(gammaRow).toContainText('1000')

  // 首次扫描：新增 1 条告警并回写
  await page.getByTestId('quota-scan').click()
  await expect(page.getByTestId('quota-monitor-message')).toContainText('新增 1')
  await expect(page.getByTestId('quota-over-limit-count')).toContainText('1')

  // 再次扫描：拒绝生成重复结果
  await page.getByTestId('quota-scan').click()
  await expect(page.getByTestId('quota-monitor-message')).toContainText('拦截 1')

  // 重启恢复视图可正常打开（未完成请求恢复为待确认）
  await page.goto('/apigate')
  await page.getByTestId('quota-recover').click()
  await expect(page.getByTestId('quota-message')).toContainText('待确认')

  // @ts-expect-error 读取 beforeEach 收集的浏览器错误
  expect(page.__quotaErrors).toEqual([])
})

test('告警中心：展示运行监控回写的配额超限待办', async ({ page }) => {
  await page.goto('/alert')
  await expect(page.getByTestId('quota-todo-panel')).toBeVisible()
  // 用表格内行选择器，避免与面板容器的 testid 前缀冲突
  const todoRows = page.getByTestId('quota-todo-table').locator('[data-testid^="quota-todo-"]')
  await expect(todoRows).toHaveCount(1)
  await expect(todoRows.first()).toContainText(lastOverLimitCaller)
  await expect(todoRows.first()).toContainText('API配额超限')
  await expect(todoRows.first()).toContainText('API配额消耗数据台账')

  // @ts-expect-error 读取 beforeEach 收集的浏览器错误
  expect(page.__quotaErrors).toEqual([])
})

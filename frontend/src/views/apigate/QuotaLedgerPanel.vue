<template>
  <section class="quota-panel" data-testid="quota-ledger-panel">
    <header class="page-head">
      <div>
        <h3>API配额消耗数据台账</h3>
        <p class="page-desc">
          配额消耗逐条落盘并持久同步，重启后仍可核对剩余额度；重复请求编号只记账一条，
          未完成请求恢复为待确认状态。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="quota-reload" type="button" @click="reloadAll">刷新台账</button>
        <button class="btn ghost" data-testid="quota-recover" type="button" @click="loadRecover">重启恢复待确认</button>
      </div>
    </header>

    <div v-if="message" class="quota-message" :class="messageOk ? 'ok' : 'err'" data-testid="quota-message">
      {{ message }}
    </div>

    <div class="quota-grid">
      <article class="stat-card">
        <span class="stat-label">当前周期指针</span>
        <strong class="stat-value" data-testid="quota-current-period">{{ currentPeriod || '—' }}</strong>
      </article>
      <article class="stat-card" v-for="card in balanceCards" :key="card.label">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="loadBalance">
      <label class="filter-item">
        <span>调用方（余额查询）</span>
        <input v-model="balanceQuery.caller" data-testid="quota-balance-caller" placeholder="如 caller-alpha" required />
      </label>
      <label class="filter-item">
        <span>周期版本（留空取当前）</span>
        <input v-model="balanceQuery.period" data-testid="quota-balance-period" placeholder="如 2026-11" />
      </label>
      <button class="btn primary" data-testid="quota-balance-btn" type="submit">查询余额</button>
      <span v-if="balance" class="quota-boundary" data-testid="quota-balance-boundary">
        周期边界：{{ balance.周期边界.周期开始 }} ~ {{ balance.周期边界.周期结束 }}
      </span>
    </form>

    <form class="filter-bar quota-consume-form" @submit.prevent="submitConsume">
      <label class="filter-item" v-for="field in consumeFields" :key="field.key">
        <span>{{ field.label }}</span>
        <input
          v-model="consumeForm[field.key]"
          :data-testid="`quota-consume-${field.key}`"
          :placeholder="field.placeholder"
          :required="field.required"
        />
      </label>
      <button class="btn primary" data-testid="quota-consume-btn" type="submit" :disabled="submitting">
        {{ submitting ? '提交中…' : '提交消耗（待确认）' }}
      </button>
    </form>

    <form class="filter-bar" @submit.prevent="loadLedger">
      <label class="filter-item">
        <span>调用方</span>
        <input v-model="ledgerFilters.caller" data-testid="quota-filter-caller" placeholder="按调用方检索" />
      </label>
      <label class="filter-item">
        <span>请求编号</span>
        <input v-model="ledgerFilters.requestNo" data-testid="quota-filter-request" placeholder="按请求编号检索" />
      </label>
      <label class="filter-item">
        <span>周期版本</span>
        <input v-model="ledgerFilters.period" data-testid="quota-filter-period" placeholder="按周期版本检索" />
      </label>
      <label class="filter-item">
        <span>记账状态</span>
        <input v-model="ledgerFilters.status" data-testid="quota-filter-status" placeholder="待确认/已确认/已作废" />
      </label>
      <button class="btn" data-testid="quota-ledger-query" type="submit">查询台账</button>
      <button class="btn ghost" type="button" @click="resetLedgerFilters">重置条件</button>
    </form>

    <table class="data-table" data-testid="quota-ledger-table">
      <thead>
        <tr>
          <th v-for="column in ledgerColumns" :key="column">{{ column }}</th>
          <th>回执操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in ledgerRows" :key="String(row.id)" :data-testid="`quota-row-${row.id}`" :data-status="String(row.记账状态)">
          <td v-for="column in ledgerColumns" :key="column">{{ formatCell(row, column) }}</td>
          <td class="row-actions">
            <template v-if="row.记账状态 === '待确认'">
              <button class="link" data-testid="quota-confirm" type="button" @click="confirmRequest(String(row.请求编号))">确认回执</button>
              <button class="link" data-testid="quota-revoke" type="button" @click="revokeRequest(String(row.请求编号))">撤销</button>
            </template>
            <span v-else class="quota-muted">—</span>
          </td>
        </tr>
        <tr v-if="!ledgerRows.length">
          <td :colspan="ledgerColumns.length + 1" class="empty-state">暂无台账记录，可先提交一次配额消耗</td>
        </tr>
      </tbody>
    </table>

    <div class="quota-period-row">
      <form class="filter-bar" @submit.prevent="switchPeriod">
        <label class="filter-item">
          <span>切换周期指针</span>
          <input v-model="periodInput" data-testid="quota-period-input" placeholder="如 2026-12" required />
        </label>
        <button class="btn" data-testid="quota-period-switch" type="button" @click="switchPeriod">切换当前周期</button>
      </form>
      <ul class="quota-period-list" data-testid="quota-period-list">
        <li v-for="p in periods" :key="p.周期版本" :class="{ active: p.是否当前周期 }">
          {{ p.周期版本 }}（{{ p.周期开始 }} ~ {{ p.周期结束 }}）· {{ p.记账条数 }} 条
          <em v-if="p.是否当前周期">当前</em>
        </li>
      </ul>
    </div>

    <form class="filter-bar" @submit.prevent="loadTrace">
      <label class="filter-item">
        <span>调用链溯源（请求编号或调用方）</span>
        <input v-model="traceInput" data-testid="quota-trace-input" placeholder="输入请求编号或调用方" required />
      </label>
      <button class="btn" data-testid="quota-trace-btn" type="submit">梳理来源与观测</button>
    </form>
    <div v-if="trace" class="quota-trace" data-testid="quota-trace-result">
      <p>API配置来源 {{ trace.API配置来源.length }} 条 · 台账记账 {{ trace.台账记账记录.length }} 条 ·
        接口运行观测 {{ trace.接口运行观测.length }} 条 · 告警待办 {{ trace.告警待办.length }} 条</p>
      <p class="quota-muted">当前周期版本：{{ trace.当前周期版本 }}</p>
    </div>

    <div v-if="recoverItems.length" class="quota-recover" data-testid="quota-recover-list">
      <h4>重启恢复：以下 {{ recoverItems.length }} 个请求恢复为待确认，等待补回执</h4>
      <ul>
        <li v-for="item in recoverItems" :key="String(item.id)">{{ item.请求编号 }} · {{ item.调用方 }} · 预占 {{ item.消耗额度 }}</li>
      </ul>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

interface LedgerRow {
  id: number
  请求编号: string
  调用方: string
  API版本: string
  配置编号: string
  周期版本: string
  消耗额度: number
  记账状态: string
  记账时间: string | null
  确认时间: string | null
  周期边界?: { 周期开始: string; 周期结束: string }
  [key: string]: unknown
}

interface Balance {
  调用方: string
  周期版本: string
  周期边界: { 周期开始: string; 周期结束: string }
  配额上限: number
  已确认消耗: number
  待确认消耗: number
  已占用额度: number
  剩余额度: number
  是否超限: boolean
}

interface PeriodItem {
  周期版本: string
  周期开始: string
  周期结束: string
  是否当前周期: boolean
  记账条数: number
}

interface TraceResult {
  当前周期版本: string
  API配置来源: unknown[]
  台账记账记录: unknown[]
  接口运行观测: unknown[]
  告警待办: unknown[]
}

const ENDPOINT = '/api/apigate'
const ledgerColumns = ['请求编号', '调用方', 'API版本', '配置编号', '周期版本', '消耗额度', '记账状态', '记账时间', '确认时间']
const consumeFields = [
  { key: '请求编号', label: '请求编号', placeholder: 'REQ-xxx', required: true },
  { key: '调用方', label: '调用方', placeholder: 'caller-alpha', required: true },
  { key: '配置编号', label: '配置编号', placeholder: 'APIG-0001', required: false },
  { key: 'API版本', label: 'API版本', placeholder: '留空取配置版本', required: false },
  { key: '消耗额度', label: '消耗额度', placeholder: '整数', required: true },
  { key: '周期版本', label: '周期版本', placeholder: '留空取当前周期', required: false },
] as const

const ledgerRows = ref<LedgerRow[]>([])
const ledgerTotal = ref(0)
const currentPeriod = ref('')
const periods = ref<PeriodItem[]>([])
const balance = ref<Balance | null>(null)
const trace = ref<TraceResult | null>(null)
const recoverItems = ref<LedgerRow[]>([])
const message = ref('')
const messageOk = ref(true)
const submitting = ref(false)
const periodInput = ref('')
const traceInput = ref('')
const balanceQuery = ref({ caller: '', period: '' })
const ledgerFilters = ref({ caller: '', requestNo: '', period: '', status: '' })
const consumeForm = ref<Record<string, string>>({
  请求编号: '', 调用方: '', 配置编号: '', API版本: '', 消耗额度: '1', 周期版本: '',
})

const balanceCards = computed(() => {
  if (!balance.value) {
    return [
      { label: '配额上限', value: '—' },
      { label: '已确认消耗', value: '—' },
      { label: '待确认预占', value: '—' },
      { label: '剩余额度', value: '—' },
    ]
  }
  const b = balance.value
  return [
    { label: '配额上限', value: b.配额上限 },
    { label: '已确认消耗', value: b.已确认消耗 },
    { label: '待确认预占', value: b.待确认消耗 },
    { label: '剩余额度', value: b.剩余额度 },
  ]
})

function showMessage(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

function formatCell(row: LedgerRow, column: string): string {
  const value = row[column]
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

async function getJson(path: string): Promise<any> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return response.json()
}

async function postJson(path: string, body: Record<string, unknown>): Promise<any> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return response.json()
}

async function loadLedger() {
  const params = new URLSearchParams()
  if (ledgerFilters.value.caller) params.set('调用方', ledgerFilters.value.caller)
  if (ledgerFilters.value.requestNo) params.set('请求编号', ledgerFilters.value.requestNo)
  if (ledgerFilters.value.period) params.set('周期版本', ledgerFilters.value.period)
  if (ledgerFilters.value.status) params.set('记账状态', ledgerFilters.value.status)
  params.set('size', '200')
  const data = await getJson(`${ENDPOINT}/quota/ledger?${params.toString()}`)
  ledgerRows.value = data.items ?? []
  ledgerTotal.value = data.total ?? 0
}

async function loadPeriods() {
  const data = await getJson(`${ENDPOINT}/quota/periods`)
  currentPeriod.value = data.current ?? ''
  periods.value = data.items ?? []
}

async function fetchBalance(): Promise<Balance | null> {
  if (!balanceQuery.value.caller.trim()) {
    return null
  }
  const params = new URLSearchParams({ 调用方: balanceQuery.value.caller.trim() })
  if (balanceQuery.value.period.trim()) params.set('周期版本', balanceQuery.value.period.trim())
  const data = await getJson(`${ENDPOINT}/quota/balance?${params.toString()}`)
  if (!data.ok) {
    return null
  }
  balance.value = data.item
  return data.item
}

async function loadBalance() {
  if (!balanceQuery.value.caller.trim()) {
    showMessage('请先输入调用方再查询余额', false)
    return
  }
  const item = await fetchBalance()
  if (item) {
    showMessage(`调用方 ${item.调用方} 在周期 ${item.周期版本} 的剩余额度为 ${item.剩余额度}`)
  } else {
    showMessage('余额查询失败', false)
  }
}

async function submitConsume() {
  submitting.value = true
  try {
    const values: Record<string, string> = {}
    for (const field of consumeFields) {
      const raw = consumeForm.value[field.key]
      if (raw && raw.trim()) values[field.key] = raw.trim()
    }
    const data = await postJson(`${ENDPOINT}/quota/consume`, { values })
    showMessage(data.message, data.ok)
    if (data.ok) {
      consumeForm.value = { 请求编号: '', 调用方: '', 配置编号: '', API版本: '', 消耗额度: '1', 周期版本: '' }
      await reloadAll()
    }
  } catch (error) {
    showMessage(error instanceof Error ? error.message : '提交消耗失败', false)
  } finally {
    submitting.value = false
  }
}

async function confirmRequest(requestNo: string) {
  const data = await postJson(`${ENDPOINT}/quota/confirm`, { 请求编号: requestNo })
  showMessage(data.message, data.ok)
  await reloadAll()
}

async function revokeRequest(requestNo: string) {
  const data = await postJson(`${ENDPOINT}/quota/revoke`, { 请求编号: requestNo })
  showMessage(data.message, data.ok)
  await reloadAll()
}

async function switchPeriod() {
  if (!periodInput.value.trim()) {
    showMessage('请输入要切换的周期版本', false)
    return
  }
  const data = await postJson(`${ENDPOINT}/quota/periods/switch`, { 周期版本: periodInput.value.trim() })
  showMessage(data.message, data.ok)
  periodInput.value = ''
  await reloadAll()
}

async function loadRecover() {
  const data = await getJson(`${ENDPOINT}/quota/recover`)
  recoverItems.value = data.items ?? []
  showMessage(data.message || `恢复 ${recoverItems.value.length} 个待确认请求`)
}

async function loadTrace() {
  if (!traceInput.value.trim()) {
    showMessage('请输入请求编号或调用方再溯源', false)
    return
  }
  const params = new URLSearchParams()
  const keyword = traceInput.value.trim()
  if (keyword.startsWith('REQ-')) params.set('请求编号', keyword)
  else params.set('调用方', keyword)
  const data = await getJson(`${ENDPOINT}/quota/trace?${params.toString()}`)
  if (!data.ok) {
    showMessage(data.message || '溯源失败', false)
    trace.value = null
    return
  }
  trace.value = data.item
}

function resetLedgerFilters() {
  ledgerFilters.value = { caller: '', requestNo: '', period: '', status: '' }
  void loadLedger()
}

async function reloadAll() {
  try {
    await Promise.all([loadLedger(), loadPeriods()])
    // 静默刷新余额卡片，不覆盖记账/回执动作的提示消息
    if (balance.value) await fetchBalance()
  } catch (error) {
    showMessage(error instanceof Error ? error.message : '台账刷新失败', false)
  }
}

onMounted(reloadAll)
</script>

<style scoped>
.quota-panel {
  margin-top: 24px;
  padding-top: 18px;
  border-top: 2px dashed var(--border);
}
.quota-grid {
  display: flex;
  gap: 10px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.quota-message {
  padding: 8px 12px;
  border-radius: 6px;
  margin-bottom: 12px;
  font-size: 13px;
}
.quota-message.ok {
  background: #ecfdf5;
  color: #047857;
  border: 1px solid #a7f3d0;
}
.quota-message.err {
  background: #fef2f2;
  color: #b91c1c;
  border: 1px solid #fecaca;
}
.quota-boundary,
.quota-muted {
  color: var(--muted);
  font-size: 12px;
}
.quota-consume-form {
  background: #f8fafc;
  padding: 10px;
  border-radius: 8px;
  border: 1px solid var(--border);
}
.quota-period-row {
  margin: 14px 0;
}
.quota-period-list {
  list-style: none;
  padding: 0;
  margin: 8px 0 0;
  font-size: 13px;
}
.quota-period-list li {
  padding: 4px 0;
  color: var(--muted);
}
.quota-period-list li.active {
  color: #1d4ed8;
  font-weight: 600;
}
.quota-period-list em {
  font-style: normal;
  margin-left: 6px;
  background: #dbeafe;
  border-radius: 4px;
  padding: 0 6px;
  font-size: 12px;
}
.quota-trace,
.quota-recover {
  background: #f8fafc;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
  margin-bottom: 12px;
}
.quota-recover h4 {
  margin: 0 0 6px;
}
.quota-recover ul {
  margin: 0;
  padding-left: 18px;
}
</style>

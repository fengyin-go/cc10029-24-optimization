<template>
  <section class="quota-monitor" data-testid="quota-monitor-panel">
    <header class="page-head">
      <div>
        <h3>接口运行观测清单（API配额消耗数据台账）</h3>
        <p class="page-desc">
          观测结果全部从持久化台账提取，不使用内存临时计数；超限调用方扫描后幂等回写告警待办，
          连续扫描只生成一份结论。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="quota-obs-reload" type="button" @click="reloadAll">刷新观测</button>
        <button class="btn primary" data-testid="quota-scan" type="button" :disabled="scanning" @click="scanAlerts">
          {{ scanning ? '扫描中…' : '扫描超限并回写告警' }}
        </button>
      </div>
    </header>

    <div v-if="message" class="quota-message" :class="messageOk ? 'ok' : 'err'" data-testid="quota-monitor-message">
      {{ message }}
    </div>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">当前周期</span>
        <strong class="stat-value">{{ currentPeriod || '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">观测调用方组数</span>
        <strong class="stat-value">{{ observations.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">超限组数</span>
        <strong class="stat-value" data-testid="quota-over-limit-count">{{ overLimitCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">告警待办数</span>
        <strong class="stat-value">{{ todos.length }}</strong>
      </article>
    </div>

    <table class="data-table" data-testid="quota-obs-table">
      <thead>
        <tr>
          <th v-for="column in observationColumns" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in observations"
          :key="`${row.调用方}-${row.配置编号}`"
          :data-testid="`quota-obs-row-${row.调用方}-${row.配置编号}`"
          :class="{ 'over-limit': row.是否超限 }"
        >
          <td v-for="column in observationColumns" :key="column">{{ formatCell(row, column) }}</td>
        </tr>
        <tr v-if="!observations.length">
          <td :colspan="observationColumns.length" class="empty-state">暂无台账观测数据，先在采集API页面提交配额消耗</td>
        </tr>
      </tbody>
    </table>

    <h4>已回写的配额超限告警待办</h4>
    <table class="data-table" data-testid="quota-todo-table">
      <thead>
        <tr>
          <th v-for="column in todoColumns" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="todo in todos" :key="String(todo.id)" :data-testid="`quota-todo-row-${todo.id}`">
          <td v-for="column in todoColumns" :key="column">{{ formatCell(todo, column) }}</td>
        </tr>
        <tr v-if="!todos.length">
          <td :colspan="todoColumns.length" class="empty-state">暂无超限告警待办</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

interface Observation {
  调用方: string
  配置编号: string
  API版本: string
  周期版本: string
  请求总数: number
  已确认笔数: number
  待确认笔数: number
  已作废笔数: number
  已确认消耗: number
  待确认消耗: number
  配额上限: number
  已占用额度: number
  剩余额度: number
  超限结论: string
  是否超限: boolean
  [key: string]: unknown
}

interface AlertTodo {
  id: number
  告警类型: string
  调用方: string
  配置编号: string
  周期版本: string
  已占用额度: number
  配额上限: number
  超限结论: string
  处理状态: string
  告警时间: string
  数据来源: string
  [key: string]: unknown
}

const observationColumns = [
  '调用方', '配置编号', 'API版本', '周期版本', '请求总数', '已确认笔数', '待确认笔数',
  '已确认消耗', '待确认消耗', '配额上限', '已占用额度', '剩余额度', '超限结论',
]
const todoColumns = ['id', '告警类型', '调用方', '配置编号', '周期版本', '配额上限', '已占用额度', '超限结论', '处理状态', '数据来源', '告警时间']

const observations = ref<Observation[]>([])
const todos = ref<AlertTodo[]>([])
const currentPeriod = ref('')
const message = ref('')
const messageOk = ref(true)
const scanning = ref(false)

const overLimitCount = computed(() => observations.value.filter((item) => item.是否超限).length)

function showMessage(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

function formatCell(row: Record<string, unknown>, column: string): string {
  const value = row[column]
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

async function getJson(path: string): Promise<any> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return response.json()
}

async function loadObservations() {
  const data = await getJson('/api/monitor/quota/observations')
  observations.value = data.items ?? []
  currentPeriod.value = data.current ?? ''
}

async function loadTodos() {
  const data = await getJson('/api/monitor/quota/alert-todos')
  todos.value = data.items ?? []
}

async function scanAlerts() {
  scanning.value = true
  try {
    const response = await request('/api/monitor/quota/scan-alerts', {
      method: 'POST',
      body: JSON.stringify({}),
    })
    const data = await response.json()
    showMessage(data.message || '扫描完成', data.ok !== false)
    await reloadAll()
  } catch (error) {
    showMessage(error instanceof Error ? error.message : '扫描失败', false)
  } finally {
    scanning.value = false
  }
}

async function reloadAll() {
  try {
    await Promise.all([loadObservations(), loadTodos()])
  } catch (error) {
    showMessage(error instanceof Error ? error.message : '观测数据刷新失败', false)
  }
}

onMounted(reloadAll)
</script>

<style scoped>
.quota-monitor {
  margin-top: 24px;
  padding-top: 18px;
  border-top: 2px dashed var(--border);
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
.data-table tr.over-limit td {
  background: #fef2f2;
  color: #b91c1c;
  font-weight: 600;
}
h4 {
  margin: 18px 0 8px;
}
</style>

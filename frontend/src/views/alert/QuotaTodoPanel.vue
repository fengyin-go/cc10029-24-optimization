<template>
  <section class="quota-todo-panel" data-testid="quota-todo-panel">
    <header class="page-head">
      <div>
        <h3>配额超限告警待办（运行监控回写）</h3>
        <p class="page-desc">
          数据来源为 API 配额消耗数据台账；同一周期、配置与调用方的超限结论只保留一条，
          重复扫描不会生成重复待办。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="quota-todo-reload" type="button" @click="loadTodos">刷新待办</button>
      </div>
    </header>

    <table class="data-table" data-testid="quota-todo-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="todo in todos" :key="String(todo.id)" :data-testid="`quota-todo-${todo.id}`">
          <td v-for="column in columns" :key="column">{{ cell(todo, column) }}</td>
        </tr>
        <tr v-if="!todos.length">
          <td :colspan="columns.length" class="empty-state">暂无配额超限告警待办</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

interface QuotaTodo {
  id: number
  告警类型: string
  调用方: string
  配置编号: string
  周期版本: string
  配额上限: number
  已占用额度: number
  超限结论: string
  处理状态: string
  告警时间: string
  [key: string]: unknown
}

const columns = ['id', '告警类型', '调用方', '配置编号', '周期版本', '配额上限', '已占用额度', '超限结论', '处理状态', '数据来源', '告警时间']
const todos = ref<QuotaTodo[]>([])

function cell(row: QuotaTodo, column: string): string {
  const value = row[column]
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

async function loadTodos() {
  const response = await request('/api/alert/quota-todos')
  if (!response.ok) return
  const data = await response.json()
  todos.value = data.items ?? []
}

onMounted(loadTodos)
</script>

<style scoped>
.quota-todo-panel {
  margin-top: 24px;
  padding-top: 18px;
  border-top: 2px dashed var(--border);
}
</style>

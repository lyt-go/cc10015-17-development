<template>
  <section class="page" data-module="data_filter">
    <header class="page-head">
      <div>
        <h2>数据筛选管理</h2>
        <p class="page-desc">维护筛选规则的输入、执行约束与结果记录，为数据筛选提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记筛选规则</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出数据筛选清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" :data-testid="`generate-${row.id}`" @click="generateDataset(row)">生成结果集</button>
            <button class="link" type="button" :data-testid="`revise-${row.id}`" @click="reviseRule(row)">修订版本</button>
            <button class="link" type="button" :data-testid="`reconcile-${row.id}`" @click="reconcileRule(row)">留档核对</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无数据筛选数据，可先登记筛选规则</td>
        </tr>
      </tbody>
    </table>

    <section class="dataset-section">
      <header class="dataset-head">
        <div>
          <h3>筛选结果数据集</h3>
          <p class="page-desc">规则版本与结果集合同事务写入；同一规则版本重复生成得到完全相同的文件，版本变化生成新的集合。</p>
        </div>
        <label class="interrupt-toggle">
          <input v-model="simulateInterrupt" data-testid="simulate-interrupt" type="checkbox" />
          生成时模拟中断（验证断点续写）
        </label>
      </header>

      <div class="dataset-layout">
        <aside class="dataset-side" data-testid="dataset-sidebar">
          <div class="dataset-side-head">
            <span>结果集合（{{ datasets.length }}）</span>
            <button class="link" type="button" data-testid="reload-datasets" @click="reloadDatasets">刷新</button>
          </div>
          <ul>
            <li
              v-for="ds in datasets"
              :key="ds.id"
              :class="{ active: ds.id === selectedId }"
              :data-testid="`dataset-${ds.id}`"
              @click="selectDataset(ds.id)"
            >
              <span class="dataset-title">{{ ds.集合编号 }} · {{ ds.规则版本 }}</span>
              <span class="dataset-meta">
                <em class="badge" :data-status="ds.状态">{{ ds.状态 }}</em>
                <em class="tag" :class="{ muted: !ds.exportable }">{{ ds.exportable ? '可导出' : '不可导出' }}</em>
              </span>
            </li>
            <li v-if="!datasets.length" class="dataset-empty">暂无筛选结果数据集，可在规则行点击「生成结果集」</li>
          </ul>
        </aside>

        <div class="dataset-detail" data-testid="dataset-detail">
          <template v-if="selectedDataset">
            <dl class="dataset-props">
              <div><dt>集合编号</dt><dd>{{ selectedDataset.集合编号 }}</dd></div>
              <div><dt>规则编号</dt><dd>{{ selectedDataset.规则编号 }}</dd></div>
              <div><dt>规则版本</dt><dd>{{ selectedDataset.规则版本 }}</dd></div>
              <div><dt>指标编号</dt><dd>{{ selectedDataset.指标编号 || '—' }}</dd></div>
              <div><dt>统计粒度</dt><dd>{{ selectedDataset.统计粒度 || '—' }}</dd></div>
              <div><dt>记录数</dt><dd>{{ selectedDataset.记录数 }}</dd></div>
              <div><dt>命中条件</dt><dd>{{ (selectedDataset.命中条件 || []).join('；') || '—' }}</dd></div>
              <div><dt>内容哈希</dt><dd class="hash-text">{{ selectedDataset.content_hash }}</dd></div>
              <div v-if="selectedDataset.指标校验">
                <dt>指标校验</dt>
                <dd :class="selectedDataset.指标校验.通过 ? 'ok-text' : 'error-text'">
                  {{ selectedDataset.指标校验.说明 }}
                </dd>
              </div>
            </dl>

            <div v-if="selectedDataset.新规则差异" class="diff-box" data-testid="rule-diff">
              <strong>新规则差异（历史集合保留旧规则结果）</strong>
              <p v-for="(change, field) in selectedDataset.新规则差异" :key="String(field)">
                {{ field }}：{{ change.旧 }} → {{ change.新 }}
              </p>
            </div>

            <div class="page-actions">
              <button
                v-if="selectedDataset.状态 === '生成中' && !selectedDataset.tx_pending"
                class="btn primary"
                type="button"
                data-testid="resume-dataset"
                @click="resumeSelected"
              >续写缺失记录</button>
              <button
                v-if="selectedDataset.状态 === '待确认'"
                class="btn primary"
                type="button"
                data-testid="confirm-dataset"
                @click="confirmSelected"
              >确认结果</button>
              <button
                v-if="selectedDataset.exportable"
                class="btn"
                type="button"
                data-testid="download-dataset"
                @click="downloadSelected"
              >下载文件</button>
              <button
                v-if="selectedDataset.状态 === '已确认'"
                class="btn"
                type="button"
                data-testid="package-dataset"
                @click="packageSelected"
              >打包交接</button>
            </div>

            <table v-if="datasetRecords.length" class="data-table">
              <thead>
                <tr><th>记录编号</th><th>日期分区</th><th>命中条件</th><th>字段值</th></tr>
              </thead>
              <tbody>
                <tr v-for="rec in datasetRecords" :key="rec.记录编号">
                  <td>{{ rec.记录编号 }}</td>
                  <td>{{ rec.日期分区 }}</td>
                  <td>{{ rec.命中条件.join('；') }}</td>
                  <td>{{ formatValues(rec.字段值) }}</td>
                </tr>
              </tbody>
            </table>
          </template>
          <p v-else class="empty-state">从侧栏选择一个结果集合查看详情</p>
        </div>
      </div>

      <table v-if="archives.length" class="data-table archive-table" data-testid="archive-table">
        <thead>
          <tr>
            <th>归档编号</th><th>规则编号</th><th>核对版本</th><th>回滚集合</th>
            <th>恢复集合</th><th>核对结论</th><th>核对时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="arch in archives" :key="arch.id">
            <td>{{ arch.归档编号 }}</td>
            <td>{{ arch.规则编号 }}</td>
            <td>{{ arch.核对版本 }}</td>
            <td>{{ arch.回滚集合.join('、') || '—' }}</td>
            <td>{{ arch.恢复集合.join('、') || '—' }}</td>
            <td>{{ arch.核对结论 }}</td>
            <td>{{ arch.核对时间 }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记筛选规则</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="conflict" class="modal-backdrop" data-testid="conflict-modal">
      <div class="modal">
        <h3>规则版本冲突</h3>
        <p class="conflict-text">
          规则已修订为 <strong>{{ conflict.新版本 }}</strong>，当前集合 {{ conflict.当前集合 }}
          仍使用 <strong>{{ conflict.旧版本 }}</strong>。请判定生成策略：
        </p>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="pin-old" @click="resolveConflict('pin_old')">
            固定旧版本生成历史集合
          </button>
          <button class="btn primary" type="button" data-testid="use-new" @click="resolveConflict('use_new')">
            采用新版本生成当前集合
          </button>
          <button class="btn ghost" type="button" @click="conflict = null">取消</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据筛选记录</span>
      <span v-if="noticeMessage" class="ok-text">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

type DatasetRecord = {
  记录编号: string
  指标编号: string
  统计粒度: string
  日期分区: string
  命中条件: string[]
  字段值: Record<string, number>
}

type DatasetSummary = {
  id: number
  集合编号: string
  规则编号: string
  规则版本: string
  指标编号: string | null
  统计粒度: string | null
  命中条件: string[]
  记录数: number
  状态: string
  exportable: boolean
  tx_pending: boolean
  content_hash: string
  指标校验: { 通过: boolean; 说明: string } | null
  新规则差异: Record<string, { 旧: unknown; 新: unknown }> | null
  generated_at: string
}

type DatasetDetail = DatasetSummary & { records?: DatasetRecord[] }

type ArchiveRow = {
  id: number
  归档编号: string
  规则编号: string
  核对版本: string
  回滚集合: string[]
  恢复集合: string[]
  核对结论: string
  核对时间: string
}

type ConflictInfo = { 旧版本: string; 新版本: string; 当前集合: string }

const ENDPOINT = '/api/data_filter'
const columns = ["规则编号", "数据来源", "筛选字段", "筛选算子", "筛选阈值", "逻辑组合", "输出集合", "规则版本"]
const actions = ["提交校验", "标记生效", "停用规则"]
const statuses = ["草稿", "待校验", "已生效", "已停用"]
const metricNames = ["筛选规则总数", "待校验规则", "已生效规则"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

const datasets = ref<DatasetSummary[]>([])
const selectedId = ref<number | null>(null)
const detail = ref<DatasetDetail | null>(null)
const archives = ref<ArchiveRow[]>([])
const conflict = ref<ConflictInfo | null>(null)
const conflictRuleId = ref<number | null>(null)
const simulateInterrupt = ref(false)

// 列表侧栏与详情页共用同一组数据：详情摘要直接取自侧栏列表的同一对象
const selectedDataset = computed(() =>
  datasets.value.find((item) => item.id === selectedId.value) ?? null)
const datasetRecords = computed(() => (detail.value?.records ?? []).slice(0, 12))

const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function formatValues(values: Record<string, number>) {
  return Object.entries(values ?? {}).map(([key, value]) => `${key}=${value}`).join('；')
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function readPayload(response: Response) {
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.detail || payload.message || `接口返回 ${response.status}`)
  }
  return payload
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'data_filter.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('数据筛选动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据筛选操作失败'
  }
}

async function generateDataset(row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/generate`, {
      method: 'POST',
      body: JSON.stringify({ simulate_interrupt: simulateInterrupt.value }),
    })
    const payload = await readPayload(response)
    if (payload.conflict) {
      conflict.value = payload.conflict
      conflictRuleId.value = Number(row.id)
    } else {
      noticeMessage.value = payload.message || '生成完成'
    }
    await reloadDatasets()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '生成筛选结果数据集失败'
  }
}

async function resolveConflict(strategy: 'pin_old' | 'use_new') {
  if (conflictRuleId.value === null) return
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${conflictRuleId.value}/resolve_conflict`, {
      method: 'POST',
      body: JSON.stringify({ strategy, simulate_interrupt: simulateInterrupt.value }),
    })
    const payload = await readPayload(response)
    noticeMessage.value = payload.message || '已按判定策略生成'
    conflict.value = null
    conflictRuleId.value = null
    await reloadDatasets()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本冲突判定失败'
  }
}

async function reviseRule(row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/revise`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
    const payload = await readPayload(response)
    noticeMessage.value = payload.message || '规则已修订'
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '规则修订失败'
  }
}

async function reconcileRule(row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/reconcile`, { method: 'POST' })
    const payload = await readPayload(response)
    noticeMessage.value = payload.message || '留档核对完成'
    await reloadDatasets()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '留档核对失败'
  }
}

async function selectDataset(id: number) {
  selectedId.value = id
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/datasets/${id}`)
    detail.value = await readPayload(response)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结果集合详情读取失败'
  }
}

async function resumeSelected() {
  if (!selectedDataset.value) return
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/datasets/${selectedDataset.value.id}/resume`, { method: 'POST' })
    const payload = await readPayload(response)
    noticeMessage.value = payload.message || '续写完成'
    await reloadDatasets()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '续写缺失记录失败'
  }
}

async function confirmSelected() {
  if (!selectedDataset.value) return
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/datasets/${selectedDataset.value.id}/confirm`, { method: 'POST' })
    const payload = await readPayload(response)
    noticeMessage.value = payload.message || '结果已确认'
    await reloadDatasets()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '确认结果失败'
  }
}

async function saveBlob(path: string, filename: string) {
  const response = await request(path)
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    throw new Error(payload.detail || '文件生成失败，请稍后重试')
  }
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

async function downloadSelected() {
  if (!selectedDataset.value) return
  errorMessage.value = ''
  try {
    const ds = selectedDataset.value
    await saveBlob(`${ENDPOINT}/datasets/${ds.id}/download`, `${ds.集合编号}.csv`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '下载筛选结果文件失败'
  }
}

async function packageSelected() {
  if (!selectedDataset.value) return
  errorMessage.value = ''
  try {
    const ds = selectedDataset.value
    await saveBlob(`${ENDPOINT}/datasets/${ds.id}/package`, `${ds.集合编号}.zip`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '打包交接失败'
  }
}

async function reloadDatasets() {
  try {
    const response = await request(`${ENDPOINT}/datasets?size=500`)
    const payload = await readPayload(response)
    datasets.value = payload.items ?? []
    if (selectedId.value !== null && datasets.value.some((item) => item.id === selectedId.value)) {
      await selectDataset(selectedId.value)
    } else if (datasets.value.length) {
      await selectDataset(datasets.value[0].id)
    } else {
      selectedId.value = null
      detail.value = null
    }
    const archiveResponse = await request(`${ENDPOINT}/archives`)
    const archivePayload = await readPayload(archiveResponse)
    archives.value = archivePayload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '筛选结果数据集读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('筛选规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据筛选列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void reloadDatasets()
})
</script>

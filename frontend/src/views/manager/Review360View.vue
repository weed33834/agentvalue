<template>
  <div class="review-360">
    <el-tabs v-model="activeTab" type="card" @tab-change="onTabChange">
      <!-- ============ Tab 1: 发起 360 环评 ============ -->
      <el-tab-pane label="发起环评" name="request">
        <el-card>
          <template #header><span>发起 360° 环评</span></template>
          <el-form label-width="120px" :model="requestForm">
            <el-form-item label="评估ID" required>
              <el-input
                v-model="requestForm.evaluationId"
                placeholder="请输入被评估的评估ID, 例如 EVAL-XXXX"
                style="width: 360px"
              />
              <el-button link type="primary" style="margin-left: 8px" @click="loadReviewsForRequest">
                查看现有邀请
              </el-button>
            </el-form-item>
          </el-form>

          <el-divider content-position="left">评估人列表（同事 / 下属 / 跨部门 / 上级）</el-divider>

          <div v-for="(r, idx) in requestForm.reviewers" :key="idx" class="reviewer-row">
            <el-input
              v-model="r.reviewer_id"
              placeholder="评估人员工ID, 例如 E1002"
              style="width: 220px"
            />
            <el-select v-model="r.reviewer_role" placeholder="角色" style="width: 160px; margin-left: 12px">
              <el-option label="同事 (Peer)" value="peer" />
              <el-option label="上级 (Manager)" value="manager" />
              <el-option label="下属 (Subordinate)" value="subordinate" />
              <el-option label="跨部门/外部 (External)" value="external" />
            </el-select>
            <el-button
              type="danger"
              link
              icon="Delete"
              style="margin-left: 12px"
              :disabled="requestForm.reviewers.length <= 1"
              @click="removeReviewer(idx)"
            />
          </div>

          <el-button
            type="primary"
            link
            icon="Plus"
            @click="addReviewer"
            style="margin-top: 8px"
          >
            添加评估人
          </el-button>

          <div style="margin-top: 24px">
            <el-button
              type="primary"
              :loading="submitting"
              :disabled="!canSubmitRequest"
              @click="submitRequest"
            >
              发起环评邀请
            </el-button>
            <span class="muted" style="margin-left: 12px; font-size: 12px">
              系统将跳过被评估员工本人（不能自评）和已存在的邀请
            </span>
          </div>
        </el-card>

        <!-- 现有邀请列表 (request 视图) -->
        <el-card v-if="requestReviews.items?.length" class="mt-20">
          <template #header><span>当前评估的环评邀请</span></template>
          <el-table :data="requestReviews.items" style="width: 100%">
            <el-table-column prop="reviewer_id" label="评估人" width="120" />
            <el-table-column label="角色" width="140">
              <template #default="{ row }">
                <el-tag size="small" :type="roleTagType(row.reviewer_role)">
                  {{ roleLabel(row.reviewer_role) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="100">
              <template #default="{ row }">
                <el-tag size="small" :type="row.status === 'submitted' ? 'success' : 'warning'">
                  {{ statusLabel(row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="overall_score" label="综合评分" width="110" />
            <el-table-column prop="created_at" label="发起时间" min-width="160">
              <template #default="{ row }">
                <span class="muted">{{ formatTime(row.created_at) }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="submitted_at" label="提交时间" min-width="160">
              <template #default="{ row }">
                <span class="muted">{{ formatTime(row.submitted_at) }}</span>
              </template>
            </el-table-column>
          </el-table>
          <div class="muted" style="margin-top: 8px; font-size: 12px">
            已邀请 {{ requestReviews.total }} 人, 已提交 {{ requestReviews.submitted_count }} 人,
            待提交 {{ requestReviews.pending_count }} 人
          </div>
        </el-card>
      </el-tab-pane>

      <!-- ============ Tab 2: 评估人填写评分 ============ -->
      <el-tab-pane label="填写评分" name="submit">
        <el-card>
          <template #header><span>评估人提交评分</span></template>
          <el-form label-width="120px" :inline="true">
            <el-form-item label="环评邀请ID">
              <el-input
                v-model="submitForm.reviewId"
                placeholder="请输入你的环评邀请ID, 例如 REV-XXXX"
                style="width: 320px"
              />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="loadingState" @click="loadReviewState">
                查看状态
              </el-button>
            </el-form-item>
          </el-form>

          <div v-if="reviewState" class="mt-20">
            <el-alert
              :title="`邀请状态: ${statusLabel(reviewState.status)} | 角色: ${roleLabel(reviewState.reviewer_role)}`"
              :type="reviewState.status === 'submitted' ? 'success' : 'warning'"
              :closable="false"
              show-icon
            />

            <el-divider content-position="left">维度评分（0-100）</el-divider>
            <div v-for="(d, idx) in submitForm.dimensions" :key="idx" class="reviewer-row">
              <el-input
                v-model="d.name"
                placeholder="维度名, 例如 执行力 / 协作 / 创新"
                style="width: 200px"
              />
              <el-input-number
                v-model="d.score"
                :min="0"
                :max="100"
                :step="1"
                style="margin-left: 12px; width: 160px"
              />
              <el-button
                type="danger"
                link
                icon="Delete"
                style="margin-left: 12px"
                @click="submitForm.dimensions.splice(idx, 1)"
              />
            </div>
            <el-button
              type="primary"
              link
              icon="Plus"
              @click="submitForm.dimensions.push({ name: '', score: 80 })"
              style="margin-top: 8px"
            >
              添加维度
            </el-button>

            <el-divider content-position="left">综合评分（可选，留空则取维度均值）</el-divider>
            <el-input-number
              v-model="submitForm.overallScore"
              :min="0"
              :max="100"
              :step="0.1"
              style="width: 200px"
            />

            <el-divider content-position="left">文字反馈</el-divider>
            <el-input
              v-model="submitForm.feedbackText"
              type="textarea"
              :rows="5"
              placeholder="请输入综合文字反馈, 最多 5000 字"
              maxlength="5000"
              show-word-limit
            />

            <div style="margin-top: 24px">
              <el-button
                type="primary"
                :loading="submitting"
                :disabled="reviewState.status === 'submitted'"
                @click="submitReview"
              >
                提交评分
              </el-button>
              <span v-if="reviewState.status === 'submitted'" class="muted" style="margin-left: 12px; font-size: 12px">
                该环评已提交, 不可重复提交
              </span>
            </div>
          </div>
        </el-card>
      </el-tab-pane>

      <!-- ============ Tab 3: 环评汇总 ============ -->
      <el-tab-pane label="环评汇总" name="summary">
        <el-card>
          <template #header><span>360° 环评汇总</span></template>
          <el-form :inline="true">
            <el-form-item label="评估ID">
              <el-input
                v-model="summaryForm.evaluationId"
                placeholder="请输入评估ID"
                style="width: 320px"
                @keyup.enter="loadSummary"
              />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="loadingSummary" @click="loadSummary">
                查看汇总
              </el-button>
            </el-form-item>
          </el-form>
        </el-card>

        <template v-if="summary">
          <el-row :gutter="20" class="mt-20">
            <el-col :span="8">
              <el-card>
                <el-statistic
                  title="已提交评估人数"
                  :value="summary.submitted_count || 0"
                  :suffix="`/ ${summary.total || 0}`"
                />
              </el-card>
            </el-col>
            <el-col :span="8">
              <el-card>
                <el-statistic
                  title="综合均分"
                  :value="summary.summary?.overall_avg || 0"
                  :precision="2"
                  value-style="color: #409eff"
                />
              </el-card>
            </el-col>
            <el-col :span="8">
              <el-card>
                <el-statistic
                  title="待提交"
                  :value="summary.pending_count || 0"
                  value-style="color: #e6a23c"
                />
              </el-card>
            </el-col>
          </el-row>

          <el-card class="mt-20" v-if="summary.summary?.dimension_avg">
            <template #header><span>各维度均分</span></template>
            <div role="img" :aria-label="dimensionSummary">
              <v-chart class="dim-chart" :option="dimensionOption" autoresize />
            </div>
          </el-card>

          <el-card class="mt-20" v-if="summary.items?.length">
            <template #header><span>各评估人评分明细</span></template>
            <el-table :data="summary.items" style="width: 100%">
              <el-table-column prop="reviewer_id" label="评估人" width="120" />
              <el-table-column label="角色" width="140">
                <template #default="{ row }">
                  <el-tag size="small" :type="roleTagType(row.reviewer_role)">
                    {{ roleLabel(row.reviewer_role) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="100">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.status === 'submitted' ? 'success' : 'warning'">
                    {{ statusLabel(row.status) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="overall_score" label="综合评分" width="110" />
              <el-table-column label="各维度评分" min-width="280">
                <template #default="{ row }">
                  <el-tag
                    v-for="(score, dim) in (row.scores || {})"
                    :key="dim"
                    size="small"
                    class="dim-tag"
                  >
                    {{ dim }}: {{ score }}
                  </el-tag>
                  <span v-if="!row.scores || !Object.keys(row.scores).length" class="muted">-</span>
                </template>
              </el-table-column>
              <el-table-column label="文字反馈" min-width="220">
                <template #default="{ row }">
                  <span class="muted">{{ (row.feedback_text || '').slice(0, 60) }}{{ (row.feedback_text || '').length > 60 ? '...' : '' }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="submitted_at" label="提交时间" width="160">
                <template #default="{ row }">
                  <span class="muted">{{ formatTime(row.submitted_at) }}</span>
                </template>
              </el-table-column>
            </el-table>
          </el-card>

          <el-card class="mt-20" v-if="!summary.items?.length && summary.total">
            <el-alert
              title="被评估员工视角：仅可见汇总数据，不暴露单个评估人明细"
              type="info"
              :closable="false"
              show-icon
            />
          </el-card>
        </template>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { reviewApi } from '@/api/client'
import VChart from 'vue-echarts'
import '@/utils/echarts'

const activeTab = ref('request')

// ============ Tab 1: 发起环评 ============
const submitting = ref(false)
const requestForm = ref({
  evaluationId: '',
  reviewers: [{ reviewer_id: '', reviewer_role: 'peer' }],
})
const requestReviews = ref({}) // 现有邀请列表

const canSubmitRequest = computed(() => {
  if (!requestForm.value.evaluationId.trim()) return false
  const valid = requestForm.value.reviewers.filter(
    (r) => r.reviewer_id.trim() && r.reviewer_role
  )
  return valid.length > 0
})

function addReviewer() {
  requestForm.value.reviewers.push({ reviewer_id: '', reviewer_role: 'peer' })
}
function removeReviewer(idx) {
  requestForm.value.reviewers.splice(idx, 1)
}

async function loadReviewsForRequest() {
  if (!requestForm.value.evaluationId.trim()) {
    ElMessage.warning('请先输入评估ID')
    return
  }
  try {
    requestReviews.value = await reviewApi.listReviews(requestForm.value.evaluationId.trim())
  } catch (err) {
    ElMessage.error(err.message || '加载现有邀请失败')
  }
}

async function submitRequest() {
  const valid = requestForm.value.reviewers
    .filter((r) => r.reviewer_id.trim() && r.reviewer_role)
    .map((r) => ({ reviewer_id: r.reviewer_id.trim(), reviewer_role: r.reviewer_role }))
  if (!valid.length) {
    ElMessage.warning('请至少添加一个有效的评估人')
    return
  }
  submitting.value = true
  try {
    const result = await reviewApi.requestReviews(requestForm.value.evaluationId.trim(), valid)
    ElMessage.success(`已发起 ${result.created_count} 份邀请, 跳过 ${result.skipped_count} 份`)
    // 刷新现有邀请列表
    await loadReviewsForRequest()
  } catch (err) {
    ElMessage.error(err.message || '发起环评失败')
  } finally {
    submitting.value = false
  }
}

// ============ Tab 2: 评估人填写 ============
const loadingState = ref(false)
const submitForm = ref({
  reviewId: '',
  dimensions: [{ name: '执行力', score: 80 }],
  overallScore: null,
  feedbackText: '',
})
const reviewState = ref(null)

async function loadReviewState() {
  if (!submitForm.value.reviewId.trim()) {
    ElMessage.warning('请输入环评邀请ID')
    return
  }
  loadingState.value = true
  try {
    reviewState.value = await reviewApi.getReviewState(submitForm.value.reviewId.trim())
    // 如果已有评分, 预填表单
    if (reviewState.value.scores && Object.keys(reviewState.value.scores).length) {
      submitForm.value.dimensions = Object.entries(reviewState.value.scores).map(
        ([name, score]) => ({ name, score })
      )
    }
    if (reviewState.value.overall_score != null) {
      submitForm.value.overallScore = reviewState.value.overall_score
    }
    if (reviewState.value.feedback_text) {
      submitForm.value.feedbackText = reviewState.value.feedback_text
    }
  } catch (err) {
    ElMessage.error(err.message || '加载状态失败')
  } finally {
    loadingState.value = false
  }
}

async function submitReview() {
  const scores = {}
  for (const d of submitForm.value.dimensions) {
    if (d.name && d.name.trim()) {
      scores[d.name.trim()] = Number(d.score)
    }
  }
  const payload = {
    scores,
    feedback_text: submitForm.value.feedbackText || '',
  }
  if (submitForm.value.overallScore != null) {
    payload.overall_score = Number(submitForm.value.overallScore)
  }
  submitting.value = true
  try {
    const result = await reviewApi.submitReview(submitForm.value.reviewId.trim(), payload)
    ElMessage.success(`提交成功, 综合评分: ${result.overall_score ?? '-'}`)
    reviewState.value = result
  } catch (err) {
    ElMessage.error(err.message || '提交失败')
  } finally {
    submitting.value = false
  }
}

// ============ Tab 3: 环评汇总 ============
const loadingSummary = ref(false)
const summaryForm = ref({ evaluationId: '' })
const summary = ref(null)

async function loadSummary() {
  if (!summaryForm.value.evaluationId.trim()) {
    ElMessage.warning('请输入评估ID')
    return
  }
  loadingSummary.value = true
  try {
    summary.value = await reviewApi.listReviews(summaryForm.value.evaluationId.trim())
  } catch (err) {
    ElMessage.error(err.message || '加载汇总失败')
  } finally {
    loadingSummary.value = false
  }
}

const dimensionOption = computed(() => {
  const dimAvg = summary.value?.summary?.dimension_avg || {}
  const dims = Object.keys(dimAvg)
  return {
    tooltip: { trigger: 'axis', formatter: '{b}: {c} 分' },
    grid: { left: 60, right: 30, top: 30, bottom: 40 },
    xAxis: {
      type: 'category',
      data: dims,
      axisLabel: { interval: 0, rotate: dims.length > 6 ? 30 : 0 },
    },
    yAxis: { type: 'value', min: 0, max: 100, name: '均分' },
    series: [
      {
        type: 'bar',
        data: dims.map((d) => dimAvg[d]),
        itemStyle: { color: '#409eff' },
        label: { show: true, position: 'top', formatter: '{c}' },
        barMaxWidth: 48,
      },
    ],
  }
})

const dimensionSummary = computed(() => {
  const dimAvg = summary.value?.summary?.dimension_avg || {}
  const parts = Object.entries(dimAvg).map(([d, s]) => `${d} ${s}分`)
  return `各维度均分: ${parts.join(', ') || '暂无'}`
})

// ============ 通用工具 ============
function onTabChange() {
  // 切换 tab 时不清空数据, 保留输入
}

function roleLabel(role) {
  return {
    peer: '同事',
    manager: '上级',
    subordinate: '下属',
    external: '跨部门/外部',
  }[role] || role
}

function roleTagType(role) {
  return {
    peer: 'primary',
    manager: 'success',
    subordinate: 'warning',
    external: 'info',
  }[role] || 'info'
}

function statusLabel(s) {
  return { pending: '待提交', submitted: '已提交' }[s] || s
}

function formatTime(iso) {
  if (!iso) return '-'
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}
</script>

<style scoped>
.mt-20 {
  margin-top: 20px;
}
.muted {
  color: #909399;
  font-size: 13px;
}
.reviewer-row {
  display: flex;
  align-items: center;
  margin-bottom: 12px;
}
.dim-tag {
  margin-right: 6px;
  margin-bottom: 4px;
}
.dim-chart {
  width: 100%;
  height: 320px;
}
</style>

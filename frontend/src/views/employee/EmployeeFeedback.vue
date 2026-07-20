<template>
  <div class="employee-feedback">
    <el-row :gutter="20">
      <el-col :span="10">
        <el-card v-loading="loading" :aria-busy="loading">
          <template #header>
            <span>我的评估列表</span>
          </template>
          <el-table
            :data="evaluations"
            style="width: 100%"
            highlight-current-row
            empty-text="暂无评估记录"
            @current-change="handleSelect"
          >
            <el-table-column prop="period" label="周期" />
            <el-table-column prop="overall_score" label="综合得分" sortable />
            <el-table-column prop="created_at" label="创建时间" />
          </el-table>
        </el-card>
      </el-col>

      <el-col :span="14">
        <el-card>
          <template #header>
            <span>提交反馈 / 申诉</span>
          </template>
          <el-form v-if="selected" label-position="top" :model="form">
            <el-form-item label="所选评估">
              <el-input :value="`${selected.period} · 得分 ${selected.overall_score}`" disabled />
            </el-form-item>

            <el-form-item label="类型">
              <el-radio-group v-model="form.type">
                <el-radio value="feedback">反馈</el-radio>
                <el-radio value="appeal">申诉</el-radio>
              </el-radio-group>
            </el-form-item>

            <el-form-item label="内容">
              <el-input
                v-model="form.content"
                type="textarea"
                :rows="6"
                :placeholder="
                  form.type === 'appeal'
                    ? '请填写申诉理由，说明对评估结果的异议'
                    : '请填写反馈内容，帮助改进评估质量'
                "
              />
            </el-form-item>

            <el-form-item>
              <el-button
                type="primary"
                :loading="submitting"
                :disabled="!form.content.trim()"
                @click="submit"
              >
                提交{{ form.type === 'appeal' ? '申诉' : '反馈' }}
              </el-button>
            </el-form-item>
          </el-form>
          <el-empty v-else description="请从左侧选择一条评估" />
        </el-card>
      </el-col>
    </el-row>

    <!-- 反馈闭环：员工可在此追踪已提交反馈/申诉的处理进度 -->
    <el-row :gutter="20" class="records-row">
      <el-col :span="24">
        <el-card v-loading="recordsLoading" :aria-busy="recordsLoading">
          <template #header>
            <span>我的反馈与申诉记录</span>
          </template>
          <!-- 无障碍：记录动态加载，用 role=status 通告屏幕阅读器 -->
          <el-table
            v-if="records.length"
            :data="records"
            style="width: 100%"
            empty-text="暂无记录"
            role="status"
            aria-live="polite"
          >
            <el-table-column prop="type" label="类型" width="90">
              <template #default="{ row }">
                <el-tag :type="row.type === 'appeal' ? 'warning' : 'info'" size="small">
                  {{ row.type === 'appeal' ? '申诉' : '反馈' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="周期" width="120">
              <template #default="{ row }">
                {{ row.evaluation?.period || '—' }}
              </template>
            </el-table-column>
            <el-table-column prop="content" label="内容" show-overflow-tooltip />
            <el-table-column prop="created_at" label="提交时间" width="180" />
            <el-table-column label="处理状态" width="140">
              <template #default="{ row }">
                <el-tag :type="statusTagType(row.evaluation?.status)" size="small">
                  {{ statusLabel(row.evaluation?.status) }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-else description="暂无反馈或申诉记录" />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { reactive, ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { employeeApi, evaluationApi } from '@/api/client'
import { statusLabel, statusTagType } from '@/utils/evaluationStatus'

const auth = useAuthStore()
const loading = ref(false)
const submitting = ref(false)
const recordsLoading = ref(false)
const evaluations = ref([])
const selected = ref(null)
// 已提交的反馈/申诉记录，含关联评估当前状态，用于追踪处理进度
const records = ref([])

const form = reactive({
  type: 'feedback',
  content: '',
})

function handleSelect(row) {
  selected.value = row
  form.content = ''
}

function formatError(err, defaultMessage) {
  // 错误已被 axios 拦截器统一封装为 Error，直接取 message 即可
  return err?.message || defaultMessage
}

async function loadData() {
  loading.value = true
  try {
    const data = await employeeApi.history(auth.userId)
    evaluations.value = data.evaluations || []
  } catch (err) {
    console.error('加载评估列表失败:', err)
    ElMessage.error(formatError(err, '加载评估列表失败'))
  } finally {
    loading.value = false
  }
}

async function loadRecords() {
  recordsLoading.value = true
  try {
    const data = await employeeApi.feedback(auth.userId)
    records.value = data.feedback || []
  } catch (err) {
    console.error('加载反馈记录失败:', err)
    ElMessage.error(formatError(err, '加载反馈记录失败'))
  } finally {
    recordsLoading.value = false
  }
}

async function submit() {
  if (!selected.value) {
    ElMessage.warning('请先选择一条评估')
    return
  }
  if (!form.content.trim()) {
    ElMessage.warning('请填写内容')
    return
  }
  submitting.value = true
  try {
    const evaluationId = selected.value.evaluation_id
    if (form.type === 'appeal') {
      // 后端 appeal 端点读取 comment 字段
      await evaluationApi.appeal(evaluationId, { comment: form.content })
      ElMessage.success('申诉已提交')
    } else {
      // 后端 feedback 端点读取 content 字段
      await evaluationApi.feedback(evaluationId, { content: form.content, type: 'feedback' })
      ElMessage.success('反馈已提交')
    }
    form.content = ''
    // 提交成功后刷新记录，让员工立即看到最新处理状态
    await loadRecords()
  } catch (err) {
    ElMessage.error(formatError(err, '提交失败，请稍后重试'))
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  loadData()
  loadRecords()
})
</script>

<style scoped>
.records-row {
  margin-top: 20px;
}
</style>

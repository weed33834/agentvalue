<template>
  <div class="llm-config" v-loading="loading" :aria-busy="loading">
    <el-alert
      v-if="!loading"
      type="info"
      :closable="false"
      show-icon
      class="mb-16"
    >
      <template #title>
        LLM 配置中心 —— 在此输入 API Key、base_url、模型名等，保存后立即生效并持久化到
        <code>.env.runtime</code>（gitignored），重启后自动加载。敏感字段保存后以
        <code>sk-***xyz</code> 形式回显，留空或保持 mask 占位符表示不修改。
      </template>
    </el-alert>

    <el-row :gutter="20">
      <!-- 左侧：配置表单 -->
      <el-col :span="16">
        <el-form ref="formRef" :model="form" label-position="top" class="config-form">
          <!-- 核心聊天模型 -->
          <el-card class="section-card">
            <template #header>
              <span class="section-title">
                <el-icon><MagicStick /></el-icon>
                核心聊天模型（评估主路径）
              </span>
            </template>
            <el-form-item label="模型档位 (model_tier)">
              <el-select v-model="form.model_tier" placeholder="auto 自动选择">
                <el-option
                  v-for="opt in tierOptions"
                  :key="opt.value"
                  :label="opt.label"
                  :value="opt.value"
                />
              </el-select>
              <span class="field-hint">auto = 按硬件自动；L0 = 云端；L1/L2/L3 = 本地</span>
            </el-form-item>
            <el-form-item label="云端 API Key (cloud_api_key)">
              <el-input
                v-model="form.cloud_api_key"
                type="password"
                show-password
                placeholder="sk-..."
              />
            </el-form-item>
            <el-form-item label="云端 Base URL (cloud_base_url)">
              <el-input v-model="form.cloud_base_url" placeholder="https://api.openai.com/v1" />
            </el-form-item>
            <el-form-item label="云端模型名 (cloud_model)">
              <el-input v-model="form.cloud_model" placeholder="gpt-4o-mini / gpt-5.5 / deepseek-chat" />
            </el-form-item>
          </el-card>

          <!-- OpenAI 兼容兜底 -->
          <el-card class="section-card">
            <template #header>
              <span class="section-title">
                <el-icon><Connection /></el-icon>
                OpenAI 兼容兜底（cloud_* 未设置时使用）
              </span>
            </template>
            <el-form-item label="API Key (openai_api_key)">
              <el-input
                v-model="form.openai_api_key"
                type="password"
                show-password
                placeholder="sk-..."
              />
            </el-form-item>
            <el-form-item label="Base URL (openai_base_url)">
              <el-input v-model="form.openai_base_url" placeholder="https://api.openai.com/v1" />
            </el-form-item>
            <el-form-item label="模型名 (openai_model)">
              <el-input v-model="form.openai_model" placeholder="gpt-4o-mini" />
            </el-form-item>
          </el-card>

          <!-- 本地模型 -->
          <el-card class="section-card">
            <template #header>
              <span class="section-title">
                <el-icon><Cpu /></el-icon>
                本地模型（LM Studio / Ollama）
              </span>
            </template>
            <el-form-item label="本地 Base URL (local_base_url)">
              <el-input v-model="form.local_base_url" placeholder="http://localhost:1234/v1" />
            </el-form-item>
            <el-form-item label="本地 API Key (local_api_key)">
              <el-input
                v-model="form.local_api_key"
                type="password"
                show-password
                placeholder="通常留空"
              />
            </el-form-item>
            <el-form-item label="L1 边缘模型 (local_model_l1)">
              <el-input v-model="form.local_model_l1" placeholder="qwen2.5-0.5b-instruct" />
            </el-form-item>
            <el-form-item label="L2 标准模型 (local_model_l2)">
              <el-input v-model="form.local_model_l2" placeholder="qwen2.5-7b-instruct" />
            </el-form-item>
            <el-form-item label="L3 旗舰模型 (local_model_l3)">
              <el-input v-model="form.local_model_l3" placeholder="qwen2.5-14b-instruct" />
            </el-form-item>
          </el-card>

          <!-- Embedding -->
          <el-card class="section-card">
            <template #header>
              <span class="section-title">
                <el-icon><Histogram /></el-icon>
                Embedding 向量模型
              </span>
            </template>
            <el-form-item label="API Key (embedding_api_key)">
              <el-input
                v-model="form.embedding_api_key"
                type="password"
                show-password
                placeholder="留空则复用 cloud_api_key"
              />
            </el-form-item>
            <el-form-item label="Base URL (embedding_base_url)">
              <el-input v-model="form.embedding_base_url" placeholder="留空则复用 cloud_base_url" />
            </el-form-item>
            <el-form-item label="模型名 (embedding_model)">
              <el-input v-model="form.embedding_model" placeholder="text-embedding-3-small" />
            </el-form-item>
            <el-form-item label="向量维度 (embedding_dimensions)">
              <el-input-number v-model="form.embedding_dimensions" :min="64" :max="8192" :step="64" />
            </el-form-item>
          </el-card>

          <!-- Vision / OCR -->
          <el-card class="section-card">
            <template #header>
              <span class="section-title">
                <el-icon><Picture /></el-icon>
                Vision / OCR 图片理解
              </span>
            </template>
            <el-form-item label="Vision 模型 (vision_model)">
              <el-input v-model="form.vision_model" placeholder="gpt-4o-mini" />
            </el-form-item>
            <el-form-item label="OCR 后端 (ocr_provider)">
              <el-select v-model="form.ocr_provider">
                <el-option label="none 不启用" value="none" />
                <el-option label="tesseract 本地" value="tesseract" />
                <el-option label="cloud 云端 vision API" value="cloud" />
              </el-select>
            </el-form-item>
            <el-form-item label="OCR 语言 (ocr_lang)">
              <el-input v-model="form.ocr_lang" placeholder="chi_sim+eng" />
            </el-form-item>
            <el-form-item label="云端 OCR Provider (ocr_cloud_provider)">
              <el-input v-model="form.ocr_cloud_provider" placeholder="aliyun" />
            </el-form-item>
            <el-form-item label="云端 OCR Secret Key (ocr_cloud_secret_key)">
              <el-input
                v-model="form.ocr_cloud_secret_key"
                type="password"
                show-password
                placeholder="阿里云 AKSK 的 SK"
              />
            </el-form-item>
            <el-form-item label="云端 OCR API Key (ocr_cloud_api_key)">
              <el-input
                v-model="form.ocr_cloud_api_key"
                type="password"
                show-password
                placeholder="sk-..."
              />
            </el-form-item>
            <el-form-item label="云端 OCR Base URL (ocr_cloud_base_url)">
              <el-input v-model="form.ocr_cloud_base_url" placeholder="https://api.openai.com/v1" />
            </el-form-item>
            <el-form-item label="云端 OCR 模型 (ocr_cloud_model)">
              <el-input v-model="form.ocr_cloud_model" placeholder="gpt-4o-mini" />
            </el-form-item>
          </el-card>

          <!-- ASR -->
          <el-card class="section-card">
            <template #header>
              <span class="section-title">
                <el-icon><Microphone /></el-icon>
                ASR 语音转文字
              </span>
            </template>
            <el-form-item label="ASR 后端 (asr_provider)">
              <el-select v-model="form.asr_provider">
                <el-option label="dummy 占位" value="dummy" />
                <el-option label="whisper 云端" value="whisper" />
              </el-select>
            </el-form-item>
            <el-form-item label="Whisper 模型 (whisper_model)">
              <el-input v-model="form.whisper_model" placeholder="base" />
            </el-form-item>
            <el-form-item label="云端 ASR API Key (asr_cloud_api_key)">
              <el-input
                v-model="form.asr_cloud_api_key"
                type="password"
                show-password
                placeholder="sk-..."
              />
            </el-form-item>
            <el-form-item label="云端 ASR Base URL (asr_cloud_base_url)">
              <el-input v-model="form.asr_cloud_base_url" placeholder="https://api.openai.com/v1" />
            </el-form-item>
            <el-form-item label="云端 ASR 模型 (asr_cloud_model)">
              <el-input v-model="form.asr_cloud_model" placeholder="whisper-1" />
            </el-form-item>
          </el-card>

          <!-- Rerank (P2-2, 对标 Dify Rerank) -->
          <el-card class="section-card">
            <template #header>
              <span class="section-title">
                <el-icon><Sort /></el-icon>
                Rerank 检索结果重排序（ChromaDB 二次精排）
              </span>
            </template>
            <el-form-item label="Rerank Provider (rerank_provider)">
              <el-radio-group v-model="form.rerank_provider">
                <el-radio value="dummy">dummy 不启用</el-radio>
                <el-radio value="cohere">Cohere</el-radio>
                <el-radio value="jina">Jina</el-radio>
                <el-radio value="bge">BGE 本地</el-radio>
              </el-radio-group>
              <span class="field-hint">dummy 时 retrieve_context 完全等价于未启用 rerank</span>
            </el-form-item>
            <el-form-item label="API Key (rerank_api_key)">
              <el-input
                v-model="form.rerank_api_key"
                type="password"
                show-password
                placeholder="Cohere / Jina 凭证，BGE 本地无需"
              />
            </el-form-item>
            <el-form-item label="Base URL (rerank_base_url)">
              <el-input
                v-model="form.rerank_base_url"
                placeholder="留空使用默认 endpoint（cohere: api.cohere.ai / jina: api.jina.ai）"
              />
            </el-form-item>
            <el-form-item label="模型名 (rerank_model)">
              <el-input
                v-model="form.rerank_model"
                placeholder="留空使用默认模型（cohere: rerank-multilingual-v3.0）"
              />
            </el-form-item>
            <el-form-item label="Top K (rerank_top_k)">
              <el-slider
                v-model="form.rerank_top_k"
                :min="1"
                :max="20"
                show-input
                :show-input-controls="false"
              />
              <span class="field-hint">retrieve_context 默认返回的 top_k 文档数</span>
            </el-form-item>
            <el-form-item>
              <el-button
                type="primary"
                plain
                :loading="testingRerank"
                @click="testRerank"
              >
                <el-icon><CaretRight /></el-icon>
                测试 Rerank
              </el-button>
              <el-button @click="rerankDialogVisible = true">
                <el-icon><View /></el-icon>
                查看上次结果
              </el-button>
            </el-form-item>
          </el-card>

          <!-- 通用推理参数 -->
          <el-card class="section-card">
            <template #header>
              <span class="section-title">
                <el-icon><Setting /></el-icon>
                通用推理参数
              </span>
            </template>
            <el-form-item label="温度 (temperature)">
              <el-input-number v-model="form.temperature" :min="0" :max="2" :step="0.1" :precision="1" />
            </el-form-item>
            <el-form-item label="最大 tokens (max_tokens)">
              <el-input-number v-model="form.max_tokens" :min="256" :max="32768" :step="256" />
            </el-form-item>
            <el-form-item label="请求超时秒 (llm_request_timeout)">
              <el-input-number v-model="form.llm_request_timeout" :min="10" :max="600" :step="10" />
              <span class="field-hint">评估类 prompt 较长，免费/自托管服务建议 ≥ 120s</span>
            </el-form-item>
          </el-card>

          <div class="actions">
            <el-button type="primary" :loading="saving" size="large" @click="save">
              <el-icon><Check /></el-icon>
              保存配置
            </el-button>
            <el-button :loading="testing" size="large" @click="testConnection">
              <el-icon><Connection /></el-icon>
              测试连接
            </el-button>
            <el-button size="large" @click="loadConfig">
              <el-icon><RefreshLeft /></el-icon>
              重新加载
            </el-button>
          </div>
        </el-form>
      </el-col>

      <!-- 右侧：测试结果 + 说明 -->
      <el-col :span="8">
        <el-card class="result-card">
          <template #header>
            <span class="section-title">
              <el-icon><Monitor /></el-icon>
              连接测试结果
            </span>
          </template>
          <div v-if="!testResult" class="empty-tip">
            点击"测试连接"查看各档位 LLM 是否可达
          </div>
          <div v-else class="test-list">
            <div v-for="tier in tierList" :key="tier.value" class="test-item">
              <span class="tier-label">{{ tier.value }}</span>
              <el-tag
                v-if="testResult[tier.value]"
                :type="testResult[tier.value].healthy ? 'success' : 'danger'"
                size="small"
              >
                {{ testResult[tier.value].healthy ? '可达' : '不可达' }}
              </el-tag>
              <span v-if="testResult[tier.value]?.model" class="tier-model">
                {{ testResult[tier.value].model }}
              </span>
              <span v-if="testResult[tier.value]?.error" class="tier-error">
                {{ testResult[tier.value].error }}
              </span>
            </div>
          </div>
        </el-card>

        <el-card class="result-card mt-16">
          <template #header>
            <span class="section-title">
              <el-icon><InfoFilled /></el-icon>
              LLM 调用点说明
            </span>
          </template>
          <ul class="callpoint-list">
            <li><strong>评估主路径</strong>：employee 提交日报 → graph.call_llm → LLM 生成评估</li>
            <li><strong>重新评估</strong>：manager 触发 re-evaluate → LLM 重新生成</li>
            <li><strong>HR 退回重评</strong>：hr require-reeval → 后台 LLM 重跑</li>
            <li><strong>多模态 OCR</strong>：图片附件 → vision_callable → LLM vision API</li>
            <li><strong>Embedding</strong>：知识库检索 → EmbeddingClient → 向量 API</li>
            <li><strong>LLM-as-Judge</strong>：eval/llm_judge 评估质量打分</li>
          </ul>
        </el-card>

        <el-card class="result-card mt-16">
          <template #header>
            <span class="section-title">
              <el-icon><Lock /></el-icon>
              安全说明
            </span>
          </template>
          <ul class="callpoint-list">
            <li>仅 <strong>admin</strong> 角色可访问此页面与 API</li>
            <li>敏感字段（API Key）保存后以 <code>sk-***xyz</code> mask 回显</li>
            <li>配置变更记入<strong>审计日志</strong>（仅记字段名，不记值）</li>
            <li>持久化到 <code>.env.runtime</code>（gitignored，不进版本库）</li>
            <li><code>jwt_secret_key</code> / <code>field_encryption_key</code> 等安全配置不开放运行时修改</li>
          </ul>
        </el-card>
      </el-col>
    </el-row>

    <!-- P2-2: Rerank 测试结果对话框 -->
    <el-dialog
      v-model="rerankDialogVisible"
      title="Rerank 测试结果"
      width="720px"
      :destroy-on-close="false"
    >
      <div v-if="!rerankTestResult" class="empty-tip">
        暂无结果,请先点击"测试 Rerank"
      </div>
      <div v-else>
        <div class="rerank-meta">
          <el-tag size="small" type="info">provider: {{ rerankTestResult.provider }}</el-tag>
          <el-tag size="small">返回 {{ rerankTestResult.reranked.length }} 项</el-tag>
        </div>
        <el-table :data="rerankTestResult.reranked" stripe size="small" class="rerank-table">
          <el-table-column label="#" prop="index" width="60" />
          <el-table-column label="文档" prop="document" show-overflow-tooltip />
          <el-table-column label="rerank_score" prop="rerank_score" width="140">
            <template #default="{ row }">
              <span class="score-value">{{ Number(row.rerank_score).toFixed(4) }}</span>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { adminApi, rerankAdminApi } from '@/api/client'

const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const testResult = ref(null)

// P2-2: Rerank 测试状态
const testingRerank = ref(false)
const rerankDialogVisible = ref(false)
const rerankTestResult = ref(null)

const form = reactive({
  model_tier: 'auto',
  cloud_api_key: '',
  cloud_base_url: '',
  cloud_model: '',
  openai_api_key: '',
  openai_base_url: '',
  openai_model: '',
  local_base_url: '',
  local_api_key: '',
  local_model_l1: '',
  local_model_l2: '',
  local_model_l3: '',
  embedding_api_key: '',
  embedding_base_url: '',
  embedding_model: '',
  embedding_dimensions: 1536,
  vision_model: '',
  ocr_provider: 'none',
  ocr_lang: '',
  ocr_cloud_provider: '',
  ocr_cloud_secret_key: '',
  ocr_cloud_api_key: '',
  ocr_cloud_base_url: '',
  ocr_cloud_model: '',
  asr_provider: 'dummy',
  whisper_model: '',
  asr_cloud_api_key: '',
  asr_cloud_base_url: '',
  asr_cloud_model: '',
  // P2-2: Rerank Provider 配置(对标 Dify Rerank)
  rerank_provider: 'dummy',
  rerank_api_key: '',
  rerank_base_url: '',
  rerank_model: '',
  rerank_top_k: 5,
  temperature: 0.1,
  max_tokens: 4096,
  llm_request_timeout: 120,
})

const tierOptions = [
  { value: 'auto', label: 'auto 自动选择' },
  { value: 'L0', label: 'L0 云端大模型' },
  { value: 'L1', label: 'L1 本地边缘' },
  { value: 'L2', label: 'L2 本地标准' },
  { value: 'L3', label: 'L3 本地旗舰' },
]

const tierList = [
  { value: 'L0' },
  { value: 'L1' },
  { value: 'L2' },
  { value: 'L3' },
]

async function loadConfig() {
  loading.value = true
  try {
    const data = await adminApi.getLlmConfig()
    // 后端对敏感字段返回 mask（sk-***xyz），原样填入表单：
    // 用户若不修改该字段，保存时回传 mask 占位符，后端识别后跳过（不覆盖原值）
    Object.keys(form).forEach((key) => {
      if (data[key] !== undefined && data[key] !== null) {
        form[key] = data[key]
      }
    })
  } catch (err) {
    console.error('加载 LLM 配置失败:', err)
    ElMessage.error('加载 LLM 配置失败: ' + err.message)
  } finally {
    loading.value = false
  }
}

async function save() {
  try {
    await ElMessageBox.confirm(
      '确认保存 LLM 配置？修改后立即生效，并持久化到 .env.runtime。',
      '保存确认',
      { confirmButtonText: '确认保存', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  saving.value = true
  try {
    // 构造 payload：空字符串转为 null（避免覆盖原值为空）
    // 敏感字段保持 mask 占位符原样回传（后端识别 *** 后跳过）
    const payload = {}
    Object.keys(form).forEach((key) => {
      const val = form[key]
      if (val === '' || val === null || val === undefined) {
        payload[key] = null
      } else {
        payload[key] = val
      }
    })
    const result = await adminApi.updateLlmConfig(payload)
    ElMessage.success(result.message || '配置已保存')
    // 重新加载，获取最新 mask 后的值
    await loadConfig()
  } catch (err) {
    ElMessage.error('保存失败: ' + err.message)
  } finally {
    saving.value = false
  }
}

async function testConnection() {
  testing.value = true
  testResult.value = null
  try {
    const result = await adminApi.testLlmConnection()
    testResult.value = result
    const healthyCount = Object.values(result).filter((r) => r.healthy).length
    if (healthyCount > 0) {
      ElMessage.success(`${healthyCount} 个档位可达`)
    } else {
      ElMessage.warning('所有档位均不可达，请检查配置')
    }
  } catch (err) {
    ElMessage.error('测试连接失败: ' + err.message)
  } finally {
    testing.value = false
  }
}

// P2-2: 测试 Rerank Provider
// 用一组固定的示例文档验证当前配置的 rerank provider 是否可用
// 真实查询场景下,documents 来自 ChromaDB 召回; 这里用固定示例便于横向对比 provider
async function testRerank() {
  // dummy 模式直接提示, 避免无意义请求(后端 DummyRerankProvider 也能跑, 但用户预期是测真实 provider)
  if (form.rerank_provider === 'dummy') {
    ElMessage.info('dummy 模式不启用 rerank, 请先选择 Cohere/Jina/BGE')
    return
  }
  // Cohere / Jina 需 API Key; BGE 本地无需(但需后端安装 sentence-transformers)
  if ((form.rerank_provider === 'cohere' || form.rerank_provider === 'jina') && !form.rerank_api_key) {
    ElMessage.warning('请先填写 API Key')
    return
  }
  // 先保存配置(后端测试用 settings 中的 provider/key/model), 失败则提示
  try {
    await ElMessageBox.confirm(
      '测试前将先保存当前配置(后端使用 settings 中的 rerank 配置调用), 继续？',
      'Rerank 测试',
      { confirmButtonText: '保存并测试', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  // 保存配置
  saving.value = true
  try {
    const payload = {}
    Object.keys(form).forEach((key) => {
      const val = form[key]
      if (val === '' || val === null || val === undefined) {
        payload[key] = null
      } else {
        payload[key] = val
      }
    })
    await adminApi.updateLlmConfig(payload)
    ElMessage.success('配置已保存, 开始测试 rerank')
  } catch (err) {
    ElMessage.error('保存配置失败: ' + err.message)
    return
  } finally {
    saving.value = false
  }
  // 调测试台
  testingRerank.value = true
  rerankTestResult.value = null
  try {
    const query = '员工绩效评估标准与成长建议'
    const documents = [
      '本周完成订单中心接口重构,代码 Review 通过率 100%。',
      '员工绩效评估应基于可量化的产出与协作行为,避免主观判断。',
      '团队协作能力: 主动协助同事解决技术问题,促进知识共享。',
      '今日午餐菜单: 番茄炒蛋、红烧肉、青菜豆腐汤。',
      '成长建议: 建议加强系统设计能力,参与架构评审与方案输出。',
    ]
    const result = await rerankAdminApi.test({
      query,
      documents,
      top_k: form.rerank_top_k || 5,
    })
    rerankTestResult.value = result
    rerankDialogVisible.value = true
    ElMessage.success(`rerank 测试完成, provider=${result.provider}, 返回 ${result.reranked.length} 项`)
  } catch (err) {
    ElMessage.error('Rerank 测试失败: ' + err.message)
  } finally {
    testingRerank.value = false
  }
}

onMounted(loadConfig)
</script>

<style scoped>
.mb-16 {
  margin-bottom: 16px;
}
.mt-16 {
  margin-top: 16px;
}
.section-card {
  margin-bottom: 16px;
}
.section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}
.config-form :deep(.el-form-item) {
  margin-bottom: 14px;
}
.field-hint {
  margin-left: 12px;
  color: #909399;
  font-size: 12px;
}
.actions {
  position: sticky;
  bottom: 0;
  z-index: 10;
  padding: 16px 0;
  background-color: #f3f4f6;
  display: flex;
  gap: 12px;
}
.result-card {
  margin-bottom: 16px;
}
.empty-tip {
  color: #909399;
  font-size: 13px;
  text-align: center;
  padding: 24px 0;
}
.test-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.test-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.tier-label {
  min-width: 32px;
  font-weight: 600;
}
.tier-model {
  color: #606266;
  font-size: 12px;
}
.tier-error {
  color: #f56c6c;
  font-size: 12px;
  word-break: break-all;
}
.callpoint-list {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.8;
  color: #606266;
}
.callpoint-list li {
  margin-bottom: 4px;
}
.callpoint-list code {
  padding: 1px 4px;
  background-color: #f3f4f6;
  border-radius: 3px;
  font-size: 12px;
}
.rerank-meta {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
.rerank-table {
  margin-top: 4px;
}
.score-value {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
</style>

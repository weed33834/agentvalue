import axios from 'axios'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { authFlowState, isDemoAuthEnabled } from '@/utils/auth'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api/v1',
  timeout: 120000,
  headers: {
    'Content-Type': 'application/json',
  },
})

const PUBLIC_URLS = ['/auth/login', '/auth/register', '/auth/seed-demo-users']

function isPublicUrl(config) {
  // 精确匹配路径段,避免子串误判(如某路径恰好包含 /auth/login 子串)
  const url = config.url || ''
  return PUBLIC_URLS.some((p) => url === p || url.startsWith(p + '/'))
}

api.interceptors.request.use((config) => {
  const authStore = useAuthStore()
  const isPublic = isPublicUrl(config)
  // JWT 模式：始终附加 Bearer token（公共接口与刷新接口除外）
  // token 过期不在请求拦截器预检跳转：让请求继续发出,由响应拦截器收到 401 后
  // 触发 refresh 流程,避免提前 redirect 导致 refresh 逻辑成为死代码
  if (authStore.useJwt && authStore.token && !isPublic) {
    config.headers['Authorization'] = `Bearer ${authStore.token}`
  } else if (isDemoAuthEnabled() && !isPublic) {
    // 演示模式：通过 header 传递角色与用户 ID（仅开发/显式开启时发送）
    if (authStore.role) {
      config.headers['x-user-role'] = authStore.role
    }
    if (authStore.userId) {
      config.headers['x-user-id'] = authStore.userId
    }
  }
  return config
})

function redirectToLogin() {
  if (authFlowState.hasRedirected) return
  authFlowState.hasRedirected = true
  ElMessage.error('登录已过期，请重新登录')
  useAuthStore().logout()
  window.location.href = '/login'
}

api.interceptors.response.use(
  (response) => response.data,
  async (error) => {
    const originalRequest = error.config
    const status = error.response?.status
    const isRefreshReq = originalRequest?.url?.includes('/auth/refresh')

    if (status === 401 && originalRequest && !originalRequest._retry && !isRefreshReq) {
      if (authFlowState.hasRedirected) {
        return Promise.reject(new Error('登录已过期，请重新登录'))
      }
      const authStore = useAuthStore()
      if (!authStore.useJwt) {
        redirectToLogin()
        return Promise.reject(new Error('登录已过期，请重新登录'))
      }
      originalRequest._retry = true
      try {
        if (!authFlowState.isRefreshing) {
          authFlowState.isRefreshing = true
          authFlowState.refreshPromise = authApi.refresh().finally(() => {
            authFlowState.isRefreshing = false
            authFlowState.refreshPromise = null
          })
        }
        const data = await authFlowState.refreshPromise
        const newToken = data?.token || data?.access_token
        if (newToken) {
          authStore.token = newToken
          localStorage.setItem('agentvalue_token', newToken)
        }
        return api(originalRequest)
      } catch {
        redirectToLogin()
        return Promise.reject(new Error('登录已过期，请重新登录'))
      }
    }

    if (status === 401 && !isRefreshReq && originalRequest?._retry) {
      // 已 refresh 重试过仍 401，确认登录失效再跳转，避免与刷新流程重复跳转
      redirectToLogin()
    }

    const message = error.response?.data?.detail || error.message || '请求失败'
    return Promise.reject(new Error(message))
  },
)

export default api

export const evaluationApi = {
  create: (payload) => api.post('/evaluations', payload),
  getJob: (jobId) => api.get(`/evaluations/jobs/${jobId}`),
  get: (id) => api.get(`/evaluations/${id}`),
  approve: (id, payload) => api.post(`/evaluations/${id}/approve`, payload),
  reject: (id, payload) => api.post(`/evaluations/${id}/reject`, payload),
  requestHrReview: (id, payload) => api.post(`/evaluations/${id}/request-hr-review`, payload),
  appeal: (id, payload) => api.post(`/evaluations/${id}/appeal`, payload),
  reEvaluate: (id, payload) => api.post(`/evaluations/${id}/re-evaluate`, payload),
  feedback: (id, payload) => api.post(`/evaluations/${id}/feedback`, payload),
  auditLogs: (id) => api.get(`/evaluations/${id}/audit-logs`),
}

export const managerApi = {
  dashboard: () => api.get('/manager/dashboard'),
  teamAnalytics: (teamId, members) => api.post(`/teams/${teamId}/analytics`, { members }),
}

// Phase 9.2 高级分析：团队 ROI、员工成长路径、离职风险、人才九宫格
export const analyticsApi = {
  teamRoi: (memberIds, start, end) => {
    const params = { member_ids: (memberIds || []).join(',') }
    if (start) params.start = start
    if (end) params.end = end
    return api.get('/analytics/team-roi', { params })
  },
  growthPath: (employeeId) => api.get(`/analytics/growth-path/${employeeId}`),
  attritionRisk: (memberIds) =>
    api.get('/analytics/attrition-risk', { params: { member_ids: (memberIds || []).join(',') } }),
  // 人才九宫格: 绩效 × 潜力 3x3 矩阵
  talentMatrix: (params = {}) => {
    const query = {}
    if (params.period) query.period = params.period
    if (params.memberIds && params.memberIds.length) {
      query.member_ids = params.memberIds.join(',')
    }
    return api.get('/analytics/talent-matrix', { params: query })
  },
}

// 360° 环评 API
// - requestReviews: 发起 360 环评,指定评估人列表
// - listReviews: 获取某评估的所有环评记录 (含汇总)
// - submitReview: 评估人提交评分和反馈
// - getReviewState: 查看评估人提交状态
export const reviewApi = {
  requestReviews: (evaluationId, reviewers) =>
    api.post(`/evaluations/${evaluationId}/reviews/request`, { reviewers }),
  listReviews: (evaluationId) => api.get(`/evaluations/${evaluationId}/reviews`),
  submitReview: (reviewId, payload) => api.post(`/reviews/${reviewId}/submit`, payload),
  getReviewState: (reviewId) => api.get(`/reviews/${reviewId}/state`),
}

// 校准会 API
// - create: 创建校准会
// - list: 列表查询 (可按 period / status 过滤)
// - get: 校准会详情 (含校准项列表)
// - addItem: 添加单个校准项
// - batchAddItems: 批量添加校准项
// - adjustItem: 调整单个校准项分数
// - batchAdjustItems: 批量调整分数
// - complete: 完成校准,应用分数调整回 Evaluation
export const calibrationApi = {
  create: (payload) => api.post('/calibrations', payload),
  list: (params = {}) => api.get('/calibrations', { params }),
  get: (sessionId) => api.get(`/calibrations/${sessionId}`),
  addItem: (sessionId, evaluationId) =>
    api.post(`/calibrations/${sessionId}/items`, { evaluation_id: evaluationId }),
  batchAddItems: (sessionId, evaluationIds) =>
    api.post(`/calibrations/${sessionId}/items/batch`, { evaluation_ids: evaluationIds }),
  adjustItem: (sessionId, itemId, payload) =>
    api.patch(`/calibrations/${sessionId}/items/${itemId}`, payload),
  batchAdjustItems: (sessionId, items) =>
    api.post(`/calibrations/${sessionId}/items/batch-adjust`, { items }),
  complete: (sessionId) => api.post(`/calibrations/${sessionId}/complete`),
}

export const hrApi = {
  auditQueue: () => api.get('/hr/audit-queue'),
}

export const employeeApi = {
  dashboard: (employeeId) => api.get(`/employees/${employeeId}/dashboard`),
  history: (employeeId) => api.get(`/employees/${employeeId}/history`),
  // 反馈/申诉记录及其关联评估当前状态，用于追踪申诉处理进度
  feedback: (employeeId) => api.get(`/employees/${employeeId}/feedback`),
}

export const inputApi = {
  // 上传附件到对象存储,返回 {key,url,filename,size,mime},由前端拼进 raw_inputs.attachments
  uploadAttachment: (file, onUploadProgress) => {
    const formData = new FormData()
    formData.append('file', file)
    return api.post('/attachments', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress,
    })
  },
}

export const adminApi = {
  modelStatus: () => api.get('/admin/model-status'),
  switchModel: (tier) => api.post('/admin/model-switch', { tier }),
  auditLogs: (params) => api.get('/admin/audit-logs', { params }),
  // LLM 配置管理：密钥 / base_url / 模型名 / 推理参数（仅 admin）
  getLlmConfig: () => api.get('/admin/llm-config'),
  updateLlmConfig: (payload) => api.put('/admin/llm-config', payload),
  testLlmConnection: () => api.post('/admin/llm-config/test'),
}

// Prompt 管理 API (对标 Langfuse Prompt Management)
// 完整功能: 模板 CRUD / 版本管理 / Label 指针 / Diff / Rollback / A-B / 灰度 / 预览
export const promptAdminApi = {
  // 模板列表 + 搜索
  listTemplates: (params) => api.get('/admin/prompts', { params }),
  // 创建模板(同时创建第一个版本 + 分配 label)
  createTemplate: (payload) => api.post('/admin/prompts', payload),
  // 获取模板详情(含版本与 label)
  getTemplate: (name) => api.get(`/admin/prompts/${encodeURIComponent(name)}`),
  // 删除模板(级联,protected label 需先取消)
  deleteTemplate: (name) => api.delete(`/admin/prompts/${encodeURIComponent(name)}`),
  // 版本列表
  listVersions: (name, params) =>
    api.get(`/admin/prompts/${encodeURIComponent(name)}/versions`, { params }),
  // 新建版本(不可变历史 + 自增版本号)
  createVersion: (name, payload) =>
    api.post(`/admin/prompts/${encodeURIComponent(name)}/versions`, payload),
  // 获取版本详情
  getVersion: (name, version) =>
    api.get(`/admin/prompts/${encodeURIComponent(name)}/versions/${version}`),
  // Label 列表
  listLabels: (name) => api.get(`/admin/prompts/${encodeURIComponent(name)}/labels`),
  // 分配 Label(用于回滚/灰度切换/A-B 切换)
  assignLabel: (name, payload) =>
    api.post(`/admin/prompts/${encodeURIComponent(name)}/labels`, payload),
  // 删除 Label
  removeLabel: (name, label) =>
    api.delete(`/admin/prompts/${encodeURIComponent(name)}/labels/${encodeURIComponent(label)}`),
  // Diff 对比
  diffVersions: (name, fromVer, toVer) =>
    api.get(`/admin/prompts/${encodeURIComponent(name)}/diff`, {
      params: { from: fromVer, to: toVer },
    }),
  // 一键回滚(把 production label 指向旧版本)
  rollback: (name, toVer) =>
    api.post(`/admin/prompts/${encodeURIComponent(name)}/rollback`, null, {
      params: { to: toVer },
    }),
  // 渲染预览
  previewRender: (name, payload) =>
    api.post(`/admin/prompts/${encodeURIComponent(name)}/preview`, payload),
  // 配置 A/B 测试
  setupAbTest: (name, payload) =>
    api.post(`/admin/prompts/${encodeURIComponent(name)}/ab-test`, payload),
  // 配置灰度发布
  setupCanary: (name, payload) =>
    api.post(`/admin/prompts/${encodeURIComponent(name)}/canary`, payload),
}

// 工具管理 API (对标 Dify 工具管理 / Coze 插件管理)
// 完整功能: 工具列表 / 单工具测试 / MCP 服务器管理 / ReAct Agent 调用
export const toolAdminApi = {
  // 工具列表(内置 + toolkit + MCP)
  listTools: () => api.get('/admin/tools'),
  // 测试单个工具
  testTool: (payload) => api.post('/admin/tools/test', payload),
  // MCP 服务器列表
  listMcpServers: () => api.get('/admin/tools/mcp/servers'),
  // 测试 MCP 服务器连接
  testMcp: (payload) => api.post('/admin/tools/mcp/test', payload),
  // 更新 MCP 配置(热更新)
  updateMcpConfig: (payload) => api.put('/admin/tools/mcp/config', payload),
  // 调用 ReAct Agent(复杂推理任务)
  invokeReAct: (payload) => api.post('/admin/tools/react-invoke', payload),
}

// 调试与可观测性 API (对标 Langfuse Trace 详情)
// 完整功能: Prompt 版本追溯 / Trace 链路 / 系统健康
export const debugAdminApi = {
  // 查询某评估使用的 prompt 版本
  getEvaluationPromptVersion: (evaluationId) =>
    api.get(`/admin/debug/evaluation/${encodeURIComponent(evaluationId)}/prompt-version`),
  // 查询某评估的完整 trace 链路
  getEvaluationTrace: (evaluationId) =>
    api.get(`/admin/debug/evaluation/${encodeURIComponent(evaluationId)}/trace`),
  // 系统健康汇总(circuit breaker / health cache / MCP 状态)
  getSystemHealth: () => api.get('/admin/debug/system-health'),
}

// Prometheus 指标端点挂在后端根路径 /metrics（非 /api/v1 前缀），
// 由 admin 系统指标页解析文本展示。复用 axios 实例以走统一鉴权/拦截器。
function metricsRequest() {
  const base = import.meta.env.VITE_API_BASE_URL || '/api/v1'
  // 相对地址或绝对地址均改为指向同源 /metrics
  const url = /^https?:\/\//i.test(base) ? new URL(base).origin + '/metrics' : '/metrics'
  return api.get(url, { baseURL: '', responseType: 'text', transformResponse: [(d) => d] })
}

export const metricsApi = {
  // 响应拦截器已将 resolve 值替换为 response.data；对 responseType:'text'
  // 而言 response.data 即原始文本字符串，这里直接返回即可，不应再取 .data
  fetchText: () => metricsRequest(),
}

export const authApi = {
  login: (email, password) => api.post('/auth/login', { email, password }),
  me: () => api.get('/auth/me'),
  refresh: () => api.post('/auth/refresh'),
  seedDemoUsers: () => api.post('/auth/seed-demo-users'),
}

// Provider CRUD API (对标 Dify model-providers 24 端点)
// 完整功能: Provider 模板 / 租户配置 / 凭证管理 / 模型管理 / 健康检查
export const providerAdminApi = {
  // Provider 模板列表(内置 + 已启用)
  listProviders: (params) => api.get('/admin/model-providers/providers', { params }),
  getProvider: (provider) => api.get(`/admin/model-providers/providers/${provider}`),
  // 当前工作空间 Provider 视图(template + tenant config 合并)
  getWorkspaceProviders: () => api.get('/admin/model-providers/workspaces/current/providers'),
  getWorkspaceProvider: (provider) =>
    api.get(`/admin/model-providers/workspaces/current/providers/${provider}`),
  // 启用/禁用 Provider
  setPreferredType: (provider, payload) =>
    api.post(
      `/admin/model-providers/workspaces/current/providers/${provider}/preferred-type`,
      payload,
    ),
  // 凭证 CRUD
  listCredentials: (provider) =>
    api.get(`/admin/model-providers/workspaces/current/providers/${provider}/credentials`),
  createCredential: (provider, payload) =>
    api.post(
      `/admin/model-providers/workspaces/current/providers/${provider}/credentials`,
      payload,
    ),
  updateCredential: (provider, credentialId, payload) =>
    api.put(
      `/admin/model-providers/workspaces/current/providers/${provider}/credentials/${credentialId}`,
      payload,
    ),
  deleteCredential: (provider, credentialId) =>
    api.delete(
      `/admin/model-providers/workspaces/current/providers/${provider}/credentials/${credentialId}`,
    ),
  activateCredential: (provider, credentialId) =>
    api.post(
      `/admin/model-providers/workspaces/current/providers/${provider}/credentials/${credentialId}/activate`,
    ),
  validateCredentials: (provider, payload) =>
    api.post(
      `/admin/model-providers/workspaces/current/providers/${provider}/credentials/validate`,
      payload,
    ),
  // 模型管理
  listModels: (provider) =>
    api.get(`/admin/model-providers/workspaces/current/providers/${provider}/models`),
  addModel: (provider, payload) =>
    api.post(`/admin/model-providers/workspaces/current/providers/${provider}/models`, payload),
  deleteModel: (provider, modelId) =>
    api.delete(`/admin/model-providers/workspaces/current/providers/${provider}/models/${modelId}`),
  toggleModel: (provider, modelId, payload) =>
    api.post(
      `/admin/model-providers/workspaces/current/providers/${provider}/models/${modelId}/toggle`,
      payload,
    ),
  toggleModelLoadBalancing: (provider, modelId, payload) =>
    api.post(
      `/admin/model-providers/workspaces/current/providers/${provider}/models/${modelId}/load-balancing/toggle`,
      payload,
    ),
  // 模型凭证
  listModelCredentials: (provider, modelId) =>
    api.get(
      `/admin/model-providers/workspaces/current/providers/${provider}/models/${modelId}/credentials`,
    ),
  addModelCredential: (provider, modelId, payload) =>
    api.post(
      `/admin/model-providers/workspaces/current/providers/${provider}/models/${modelId}/credentials`,
      payload,
    ),
  deleteModelCredential: (provider, modelId, credentialId) =>
    api.delete(
      `/admin/model-providers/workspaces/current/providers/${provider}/models/${modelId}/credentials/${credentialId}`,
    ),
  activateModelCredential: (provider, modelId, credentialId) =>
    api.post(
      `/admin/model-providers/workspaces/current/providers/${provider}/models/${modelId}/credentials/${credentialId}/activate`,
    ),
  validateModelCredentials: (provider, modelId, payload) =>
    api.post(
      `/admin/model-providers/workspaces/current/providers/${provider}/models/${modelId}/credentials/validate`,
      payload,
    ),
  getModelParameterRules: (provider, modelId) =>
    api.get(
      `/admin/model-providers/workspaces/current/providers/${provider}/models/${modelId}/parameter-rules`,
    ),
  // 默认模型
  listDefaultModels: () => api.get('/admin/model-providers/workspaces/current/default-models'),
  setDefaultModel: (payload) =>
    api.post('/admin/model-providers/workspaces/current/default-models', payload),
  // 健康检查
  getHealthChecks: (provider) =>
    api.get(`/admin/model-providers/workspaces/current/providers/${provider}/health-checks`),
  triggerHealthCheck: (provider) =>
    api.post(`/admin/model-providers/workspaces/current/providers/${provider}/health-check`),
}

// Playground API (对标 Langfuse Playground)
// run 走 SSE,需用 fetch-event-source 而非 axios
export const playgroundApi = {
  // 触发 SSE 流式运行(POST /admin/playground/run)
  // 返回 fetch-event-source 配置对象,由调用方用 streamChat 完成
  runUrl: '/admin/playground/run',
}

// 知识库管理 API (P1-1)
// 完整功能: 文档 CRUD + 重建索引 + 检索测试台 + 分块配置
export const kbAdminApi = {
  // 文档列表(分页 + 搜索)
  listDocs: (params) => api.get('/admin/kb/docs', { params }),
  // 创建文档(可指定 chunk_size/chunk_overlap 元信息)
  createDoc: (payload) => api.post('/admin/kb/docs', payload),
  // 文档详情
  getDoc: (kbId) => api.get(`/admin/kb/docs/${encodeURIComponent(kbId)}`),
  // 更新文档(标题/内容/元信息)
  updateDoc: (kbId, payload) => api.put(`/admin/kb/docs/${encodeURIComponent(kbId)}`, payload),
  // 删除文档
  deleteDoc: (kbId) => api.delete(`/admin/kb/docs/${encodeURIComponent(kbId)}`),
  // 重建向量索引(先删后建,按当前 chunk 配置分块嵌入)
  reindexDoc: (kbId) => api.post(`/admin/kb/docs/${encodeURIComponent(kbId)}/reindex`),
  // 检索测试台: 用 query 在向量库做 top_k 召回
  testRetrieval: (payload) => api.post('/admin/kb/test-retrieval', payload),
  // 获取当前 chunk 配置(chunk_size/chunk_overlap/embedding_model)
  getConfig: () => api.get('/admin/kb/config'),
  // 更新 chunk 配置(写入 settings + .env.runtime,重启后自动加载)
  updateConfig: (payload) => api.put('/admin/kb/config', payload),
}

// 应用内 Trace 可视化 API (P1-2,对标 Langfuse Trace UI)
// 完整功能: 评估列表(分页 + 搜索) + 节点级 trace(含 spans / timeline)
export const traceAdminApi = {
  // 评估分页列表(Trace 浏览器左侧列表使用)
  listEvaluations: (params) => api.get('/admin/debug/evaluations', { params }),
  // 查询某评估的完整 trace(含 spans 与 timeline)
  getTrace: (evaluationId) =>
    api.get(`/admin/debug/evaluation/${encodeURIComponent(evaluationId)}/trace`),
}

// Analytics Admin API (P2-1: Token/成本趋势看板)
// 完整功能: Token 时序 + 成本统计 + Provider 分布 + 评估统计
export const analyticsAdminApi = {
  // Token 用量时序(prompt/completion/total)
  tokenUsage: (params) => api.get('/admin/analytics/token-usage', { params }),
  // 成本统计(按 model / tenant 维度 USD)
  cost: (params) => api.get('/admin/analytics/cost', { params }),
  // Provider 调用分布(调用次数 + token 总数 + 平均延迟)
  providerDistribution: (params) => api.get('/admin/analytics/provider-distribution', { params }),
  // 评估统计(总数 + 按状态 + 按周期)
  evaluationStats: (params) => api.get('/admin/analytics/evaluation-stats', { params }),
}

// Rerank Admin API (P2-2: Rerank Provider 测试台, 对标 Dify Rerank)
// 完整功能: rerank 测试台 (query + documents → reranked 结果)
export const rerankAdminApi = {
  // 测试 rerank provider (调 /admin/rerank/test)
  test: (data) => api.post('/admin/rerank/test', data),
}

// 自定义工具 Admin API (P3-1: OpenAPI Schema 导入, 对标 Dify Custom Tool)
// 完整功能: 工具 CRUD + 解析预览 + 启用禁用 + HTTP 端点测试
export const customToolAdminApi = {
  // 列表 (支持 search + tenant_id 过滤)
  list: (params) => api.get('/admin/custom-tools', { params }),
  // 创建: 解析 OpenAPI → 存储 → 返回 ToolSpec 预览
  create: (payload) => api.post('/admin/custom-tools', payload),
  // 详情
  get: (toolId) => api.get(`/admin/custom-tools/${encodeURIComponent(toolId)}`),
  // 更新 (重新解析 OpenAPI)
  update: (toolId, payload) =>
    api.put(`/admin/custom-tools/${encodeURIComponent(toolId)}`, payload),
  // 删除
  delete: (toolId) => api.delete(`/admin/custom-tools/${encodeURIComponent(toolId)}`),
  // 启用/禁用
  toggle: (toolId, enabled) =>
    api.post(`/admin/custom-tools/${encodeURIComponent(toolId)}/toggle`, {
      enabled,
    }),
  // 测试: 实际调 HTTP endpoint 返回响应
  test: (toolId, payload) =>
    api.post(`/admin/custom-tools/${encodeURIComponent(toolId)}/test`, payload),
  // 仅解析 OpenAPI (不入库,返回 ToolSpec 预览)
  parse: (payload) => api.post('/admin/custom-tools/parse', payload),
}

// Feature Flag Admin API (P3-2: 应用级功能开关, 对标 Langfuse Feature Flag)
// 完整功能: CRUD + 启用/禁用切换 + 状态检查 (含命中原因)
export const featureFlagAdminApi = {
  // 列表 (支持 category 过滤: general/model/agent/feature)
  list: (params) => api.get('/admin/feature-flags', { params }),
  // 创建
  create: (payload) => api.post('/admin/feature-flags', payload),
  // 详情
  get: (key) => api.get(`/admin/feature-flags/${encodeURIComponent(key)}`),
  // 更新 (任意字段, key 不可改)
  update: (key, payload) => api.put(`/admin/feature-flags/${encodeURIComponent(key)}`, payload),
  // 删除
  delete: (key) => api.delete(`/admin/feature-flags/${encodeURIComponent(key)}`),
  // 启用/禁用切换
  toggle: (key, enabled) =>
    api.post(`/admin/feature-flags/${encodeURIComponent(key)}/toggle`, {
      enabled,
    }),
  // 检查状态 (query: tenant_id? / user_id?, 返回 {enabled, reason})
  check: (key, params) =>
    api.get(`/admin/feature-flags/${encodeURIComponent(key)}/check`, { params }),
}

// 多 Agent 协作 Admin API (P4-1: supervisor 模式多 Agent, 对标 Coze Multi-Agent)
// 完整功能: 异步运行 / 状态查询 / 暂停恢复 / artifacts 查询 / 同步测试
export const multiAgentAdminApi = {
  // 异步运行多 Agent 任务, 返回 thread_id (后台执行)
  run: (payload) => api.post('/admin/multi-agent/run', payload),
  // 查询任务状态 (含 next / values / interrupt 信息)
  getState: (threadId) =>
    api.get(`/admin/multi-agent/threads/${encodeURIComponent(threadId)}/state`),
  // 恢复暂停的任务 (body: {decision?, comment?})
  resume: (threadId, payload) =>
    api.post(`/admin/multi-agent/threads/${encodeURIComponent(threadId)}/resume`, payload),
  // 查询各 Agent 产出 (artifacts + final_report)
  getArtifacts: (threadId) =>
    api.get(`/admin/multi-agent/threads/${encodeURIComponent(threadId)}/artifacts`),
  // 同步测试 (不进队列, 直接执行返回结果, 供前端"测试"按钮)
  test: (payload) => api.post('/admin/multi-agent/test', payload),
  // 列出所有任务 (供左侧任务列表, 可选 status 过滤)
  listThreads: (params) => api.get('/admin/multi-agent/threads', { params }),
}

// 工作流编排 Admin API (P4-2: 对标 Dify Workflow / Coze Bot 编排)
// 完整功能: 工作流 CRUD + 启用禁用 + 执行 + 运行状态 + 节点级执行状态 + 验证
export const workflowAdminApi = {
  // 1. 工作流列表 (支持 search + tenant_id 过滤)
  list: (params) => api.get('/admin/workflows', { params }),
  // 2. 创建工作流 (name + description + graph + input_schema)
  create: (payload) => api.post('/admin/workflows', payload),
  // 3. 工作流详情
  get: (workflowId) => api.get(`/admin/workflows/${encodeURIComponent(workflowId)}`),
  // 4. 更新工作流 (graph 变化时 version +1)
  update: (workflowId, payload) =>
    api.put(`/admin/workflows/${encodeURIComponent(workflowId)}`, payload),
  // 5. 删除工作流
  delete: (workflowId) => api.delete(`/admin/workflows/${encodeURIComponent(workflowId)}`),
  // 6. 启用/禁用切换
  toggle: (workflowId, enabled) =>
    api.post(`/admin/workflows/${encodeURIComponent(workflowId)}/toggle`, {
      enabled,
    }),
  // 7. 执行工作流 (body: {inputs: dict, thread_id?}, 返回 {run_id, thread_id, status, ...})
  run: (workflowId, payload) =>
    api.post(`/admin/workflows/${encodeURIComponent(workflowId)}/run`, payload),
  // 8. 查询运行状态
  getRun: (runId) => api.get(`/admin/workflows/runs/${encodeURIComponent(runId)}`),
  // 9. 节点级执行状态 (供前端时间线展示)
  getRunNodeStates: (runId) =>
    api.get(`/admin/workflows/runs/${encodeURIComponent(runId)}/node-states`),
  // 10. 工作流的运行历史 (按创建时间倒序)
  listRuns: (workflowId, params) =>
    api.get(`/admin/workflows/${encodeURIComponent(workflowId)}/runs`, { params }),
  // 11. 验证 graph 合法性 (检查环 / 必填字段 / 节点类型)
  validate: (workflowId, payload) =>
    api.post(`/admin/workflows/${encodeURIComponent(workflowId)}/validate`, payload),
}

// ============================================================
// Chat 流式对话 API（移植 opencode session/prompt + SSE）
// ============================================================
export const chatApi = {
  // 创建会话
  createSession: (payload) => api.post('/chat/sessions', payload),
  // 列出当前用户的会话
  listSessions: () => api.get('/chat/sessions'),
  // 获取会话详情
  getSession: (sessionId) => api.get(`/chat/sessions/${encodeURIComponent(sessionId)}`),
  // 更新会话标题
  updateSession: (sessionId, payload) =>
    api.patch(`/chat/sessions/${encodeURIComponent(sessionId)}`, payload),
  // 删除会话
  deleteSession: (sessionId) => api.delete(`/chat/sessions/${encodeURIComponent(sessionId)}`),
  // 列出会话消息（含 parts，供前端回显历史）
  listMessages: (sessionId) => api.get(`/chat/sessions/${encodeURIComponent(sessionId)}/messages`),
  // 注意：发送消息走 SSE 流式，用 utils/sse.js 的 streamSSE，不在此处

  // ---- P0 新增 ----
  // P0-2: 重新生成最后一条 assistant 消息（走 SSE，URL 供 streamSSE 使用）
  regenerateUrl: (sessionId) => `/chat/sessions/${encodeURIComponent(sessionId)}/regenerate`,
  // P0-3: 删除单条消息（含 parts 级联）
  deleteMessage: (sessionId, messageId) =>
    api.delete(
      `/chat/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}`,
    ),
  // P0-8: 消息反馈（点赞/点踩）
  sendFeedback: (sessionId, messageId, payload) =>
    api.post(
      `/chat/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/feedback`,
      payload,
    ),
  // P0-5: 自动生成会话标题
  autoTitle: (sessionId) => api.post(`/chat/sessions/${encodeURIComponent(sessionId)}/auto-title`),
  // P0-10: 搜索会话
  searchSessions: (query) => api.get('/chat/sessions/search', { params: { q: query } }),

  // ---- 增强：分享 / Fork ----
  // 生成会话分享链接：返回 {share_url, share_id}
  shareSession: (sessionId) => api.post(`/chat/sessions/${encodeURIComponent(sessionId)}/share`),
  // 通过 share_id 只读访问会话（公开端点，无需认证）
  getSharedSession: (shareId) => api.get(`/chat/sessions/shared/${encodeURIComponent(shareId)}`),
  // 从指定消息分叉出新会话：返回新会话 dict
  forkSession: (sessionId, fromMessageId, title) =>
    api.post(`/chat/sessions/${encodeURIComponent(sessionId)}/fork`, {
      from_message_id: fromMessageId,
      title: title || undefined,
    }),
  // 停止当前会话的 SSE 流式生成
  stop: (sessionId) => api.post(`/chat/sessions/${encodeURIComponent(sessionId)}/stop`),
}

// ============================================================
// Evidence 引用 API（暴露 EvidenceRef 查询）
// ============================================================
export const evidenceApi = {
  // 查询某评估的所有证据引用（按 dimension 分组）
  list: (evaluationId) => api.get(`/evaluations/${encodeURIComponent(evaluationId)}/evidence`),
}

// ============================================================
// 语音 API (STT/TTS)
// - tts: 文本转语音，返回音频 URL 或 base64
// - stt: 语音转文本，上传音频文件返回识别文本（降级方案）
// ============================================================
export const voiceApi = {
  tts: (text) => api.post('/voice/tts', { text }),
  stt: (audioFile) => {
    const formData = new FormData()
    formData.append('file', audioFile)
    return api.post('/voice/stt', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
}

// ============================================================
// 提示词模板 API
// 完整功能: 模板 CRUD + 变量实例化（供 ChatInput 模板选择器使用）
// ============================================================
export const templateApi = {
  list: (category) => api.get('/presets/templates', { params: { category } }),
  create: (data) => api.post('/presets/templates', data),
  update: (id, data) => api.put(`/presets/templates/${encodeURIComponent(id)}`, data),
  delete: (id) => api.delete(`/presets/templates/${encodeURIComponent(id)}`),
  instantiate: (id, variables) =>
    api.post(`/presets/templates/${encodeURIComponent(id)}/instantiate`, {
      variables,
    }),
}

// ============================================================
// Agent 预设 API
// 完整功能: Agent 预设 CRUD + 使用计数
// ============================================================
export const presetApi = {
  list: (category) => api.get('/presets/agents', { params: { category } }),
  get: (id) => api.get(`/presets/agents/${encodeURIComponent(id)}`),
  create: (data) => api.post('/presets/agents', data),
  update: (id, data) => api.put(`/presets/agents/${encodeURIComponent(id)}`, data),
  delete: (id) => api.delete(`/presets/agents/${encodeURIComponent(id)}`),
  use: (id) => api.post(`/presets/agents/${encodeURIComponent(id)}/use`),
}

// ============================================================
// 会话工具配置 API
// 管理某会话启用的工具列表
// ============================================================
export const sessionToolsApi = {
  get: (sessionId) => api.get(`/chat/sessions/${encodeURIComponent(sessionId)}/tools`),
  update: (sessionId, tools) =>
    api.put(`/chat/sessions/${encodeURIComponent(sessionId)}/tools`, {
      tools,
    }),
}

// ============================================================
// Artifact API (对标 Claude Artifacts / ChatGPT Canvas)
// 完整功能: 创建 / 列表 / 详情 / 更新(版本+1) / 删除 / fork / 文本提取
// ============================================================
export const artifactApi = {
  create: (data) => api.post('/artifacts', data),
  listBySession: (sessionId) => api.get(`/artifacts/session/${sessionId}`),
  get: (id) => api.get(`/artifacts/${id}`),
  update: (id, data) => api.put(`/artifacts/${id}`, data),
  delete: (id) => api.delete(`/artifacts/${id}`),
  fork: (id) => api.post(`/artifacts/${id}/fork`),
  extract: (text) => api.post('/artifacts/extract', { text }),
}

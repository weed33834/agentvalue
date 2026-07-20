# AgentValue-AI 前端

基于 Vue 3 + Vite + Element Plus + ECharts 的员工价值评估系统前端。

## 页面说明

| 路由 | 角色 | 功能 |
|---|---|---|
| `/login` | 全部 | 账号登录 / 演示模式角色选择，可初始化演示账号 |
| `/employee` | 员工 | 个人成长看板 + 能力雷达图 |
| `/employee/input` | 员工 | 录入日报/任务进度 + 附件上传（图片/PDF/音频），触发 AI 评估 |
| `/employee/history` | 员工 | 历史评估列表与详情 |
| `/employee/feedback` | 员工 | 反馈与申诉记录及处理进度 |
| `/employee/growth-path` | 员工 | 成长路径推荐（能力对比、成长趋势、建议行动） |
| `/manager` | 主管/HR/管理员 | 团队价值排行榜、风险分布、待审批队列 |
| `/manager/approval/:id` | 主管/HR/管理员 | 评估审批详情页（批准/驳回/转 HR） |
| `/manager/team` | 主管/HR/管理员 | 团队分析（成员价值趋势、维度对比） |
| `/manager/roi` | 主管/HR/管理员 | 团队 ROI 九宫格与周度趋势 |
| `/manager/attrition-risk` | 主管/HR/管理员 | 离职风险预测（风险分布与员工明细） |
| `/hr` | HR/管理员 | HR 复核队列（高风险评估汇总） |
| `/hr/audit/:id` | HR/管理员 | HR 复核详情页（评估全量、审批历史、申诉记录） |
| `/admin` | 管理员 | 模型档位管理（运行档位/推荐档位/硬件信息/切换） |
| `/admin/llm-config` | 管理员 | LLM 配置中心（聊天/兜底/本地/Embedding/Vision/ASR/推理参数） |
| `/admin/audit-logs` | 管理员 | 审计日志查询（按操作人/动作筛选） |
| `/admin/metrics` | 管理员 | 系统指标（解析 /metrics，评估/审批/反馈/LLM 调用计数卡片） |
| `/admin/chat` | 全部 | AI 对话界面（v1.5.0）：会话管理 + SSE 流式 + 工具调用展示 + 模型切换 |

## 本地开发

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://localhost:5173`，并通过 Vite proxy 将 `/api` 转发到 `http://localhost:8000`。

## AI 对话组件（v1.5.0）

| 组件 | 路径 | 功能 |
|---|---|---|
| `ChatView.vue` | `views/admin/ChatView.vue` | 对话主视图：会话列表 + 消息流 + 模型切换 + 搜索导出 |
| `MessageList.vue` | `components/chat/MessageList.vue` | 消息列表容器：自动滚动 + 事件分发 |
| `MessageBubble.vue` | `components/chat/MessageBubble.vue` | 单条消息：Markdown 渲染 + 代码复制 + 编辑 + 点赞/点踩 + reasoning 折叠 |
| `ToolCallCard.vue` | `components/chat/ToolCallCard.vue` | 工具调用卡片：可折叠输入/输出 + JSON 美化 + 状态图标 |
| `ChatInput.vue` | `components/chat/ChatInput.vue` | 输入框：多行文本 + 文件上传 + 附件预览 + 发送/停止 |
| `chat.js` | `stores/chat.js` | 对话状态管理：SSE 事件分发 + 流式渲染 + 工具调用 + 会话 CRUD |
| `markdown.js` | `utils/markdown.js` | Markdown 渲染：KaTeX 数学公式 + Mermaid 图表 + 代码高亮 |
| `sse.js` | `utils/sse.js` | 通用 SSE 流式客户端：JWT 鉴权 + AbortController + 心跳过滤 |

## 生产构建

```bash
npm run build
```

构建产物位于 `frontend/dist/`。

## 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `VITE_API_BASE_URL` | 后端 API 基础路径 | `/api/v1` |

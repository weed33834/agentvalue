# AgentValue-AI 竞品分析与功能补全计划

> 调研时间:2026-07-20 | 覆盖 20+ 竞品 | 目标:识别功能差距,制定补全优先级

## 一、产品经理评估:当前完成度

### 整体完成度:62%

框架完整、核心闭环跑通,但"让普通人开箱即用"还差最后一公里。

| 维度 | 完成度 | 状态 |
|---|---|---|
| 对话体验 | 75% | 10项P0已完成,缺语音/多模态/分享 |
| Agent 工具层 | 55% | 9个工具,缺 grep/glob/patch/LSP/websearch |
| 评估业务闭环 | 80% | 三视图+审批+申诉完整,缺360°环评 |
| 运营管理平台 | 85% | 9个管理页面,对标Dify/Coze基本到位 |
| 多模态能力 | 25% | 仅文件上传UI,缺OCR/语音/图片理解 |
| 用户体验打磨 | 45% | 缺引导/快捷键/移动端/暗色主题/分享 |

### 落地距离

| 阶段 | 距离 | 关键差距 |
|---|---|---|
| 技术爱好者可用 | 已到达 | Docker部署+API调试可行 |
| 企业IT团队部署 | 约2周 | 缺安装向导/环境检测/数据迁移工具 |
| HR团队日常使用 | 约4-6周 | 缺360°环评/校准会/人才九宫格/移动端 |
| 普通员工自助使用 | 约8-12周 | 缺引导/移动端/通知/IM推送/语音输入 |

---

## 二、竞品调研总览

### AI 对话产品

| 产品 | 核心优势 | 我们能学的 |
|---|---|---|
| ChatGPT | Canvas写作/代码块、GPTs市场、语音对话、Code Interpreter、Vision | 多模态、分享链接、代码沙箱 |
| Claude.ai | Artifacts可视化产物、Projects隔离、Computer Use、MCP生态 | Artifacts预览、Projects工作区、MCP支持 |
| Dify | 可视化工作流+错误分支、混合检索+元数据过滤、Agent Strategy可插拔、Plugin Marketplace | 元数据过滤检索、Agent Strategy |
| Coze | 自然语言构建Bot、8+渠道发布、Bot商店 | 多渠道发布、自然语言建Bot |
| LobeChat | Agent市场+插件市场+主题市场三联动、TTS/STT全链路、PWA | 提示词模板库、Agent预设、主题 |
| Open WebUI | 完全离线运行、Pipelines框架、多OCR引擎 | Pipeline链式处理 |

### 代码智能体

| 产品 | 工具数 | 我们能学的 |
|---|---|---|
| Claude Code | 40+ | Sub Agents、Cron调度、Monitor监控、Hooks生命周期 |
| GitHub Copilot | 30+ | 跨仓库搜索、Plan mode handoffs、#工具集分组 |
| opencode | 13 | grep/glob搜索、LSP代码智能、Skill系统 |
| Cline | 7 | 逐步审批、Agent Teams、Kanban任务板 |
| Aider | slash命令式 | Repo Map图排序、architect/editor双模型 |
| Cursor | 自研模型 | 语义搜索、Browser工具、Cloud Agent |

### HR评估产品

| 产品 | AI能力定位 | 我们能学的 |
|---|---|---|
| Workday | Illuminate AI引擎(70%+客户采用) | Skills Cloud技能推理、Succession Agent |
| Culture Amp | 15亿数据点+ISO42001认证 | AI Coach角色扮演、偏差检测 |
| Lattice | 对话式人才洞察+AI Meeting Agent | 对话查询人才数据、1:1自动笔记 |
| 15Five | Spark AI辅助Review(削减偏差) | Review起草、近因偏差检测 |
| Leapsome | 跨模块AI助手 | Analytics AI Wizard对话式分析 |
| 飞书绩效 | 360°评估人"手松手紧"提示 | 偏差智能提示 |
| 钉钉智能绩效 | AI评分(87%匹配度) | AI量化产出评估 |

---

## 三、功能差距矩阵

### P0 紧急补全(竞品标配,我们没有)

| 功能 | 谁有 | 说明 | 工时估 |
|---|---|---|---|
| grep/glob 工具 | opencode/Cline/Claude Code | 代码搜索是Agent基础能力 | 1d |
| patch 工具(精确编辑) | opencode/Cline/Claude Code | 现在write_file全量覆盖,需diff patch | 2d |
| web_search 工具 | opencode/Cursor/Claude Code | 现在只有web_fetch抓单页,缺搜索 | 1d |
| 图片理解(Vision) | ChatGPT/Claude/LobeChat | 上传图片到对话,模型多模态理解 | 2d |
| 分享对话链接 | ChatGPT/Claude/LobeChat | 生成只读分享页,社交传播 | 2d |
| 对话分支(Fork) | ChatGPT/LobeChat | 从某条消息分叉出新对话路径 | 2d |

### P1 高优先(头部竞品都有)

| 功能 | 谁有 | 说明 | 工时估 |
|---|---|---|---|
| 语音输入(STT) | ChatGPT/LobeChat | 麦克风录音转文字 | 2d |
| 语音输出(TTS) | ChatGPT/LobeChat | 回复朗读,支持语音对话 | 2d |
| 提示词模板库 | LobeChat/Open WebUI/Coze | 内置常用Prompt,一键使用 | 1d |
| Agent预设/市场 | LobeChat/ChatGPT/Coze | 预配置角色(翻译/写作/代码审查) | 2d |
| MCP协议支持 | Claude/Cline/Cursor/Copilot | 接入MCP生态的400+工具 | 3d |
| Code Interpreter | ChatGPT/Claude/Dify | 沙箱内执行Python,生成图表 | 3d |
| 暗色/亮色主题切换 | 全部竞品 | 仅暗色,缺亮色切换 | 1d |
| 对话中断恢复 | ChatGPT/Claude | 流式中断后可恢复 | 1d |
| 快捷键支持 | LobeChat/Open WebUI | Cmd+K搜索、Cmd+Enter发送 | 1d |

### P2 差异化(竞品头部才有,我们也要有)

| 功能 | 谁有 | 说明 | 工时估 |
|---|---|---|---|
| Artifacts可视化产物 | Claude | 代码/SVG/HTML生成后可直接预览交互 | 3d |
| Computer Use | Claude | 截屏+鼠标键盘控制GUI | 5d |
| 多渠道发布 | Coze | 一次构建发到飞书/钉钉/微信/Web | 5d |
| 360°环评 | Lattice/飞书/Workday | 多评估人+校准会+偏差检测 | 5d |
| 人才九宫格 | Workday/飞书 | 绩效×潜力矩阵,人才盘点可视化 | 2d |
| 校准会 | Lattice/飞书 | 评估校准流程+可视化 | 3d |
| AI评估偏差检测 | Culture Amp/15Five | 检测性别/近因/评分者偏差 | 3d |
| 对话式人才洞察 | Lattice | "谁进步最大?"对话查询 | 3d |
| Repo Map | Aider | 图排序算法按token预算裁剪上下文 | 3d |
| Sub Agents | Claude Code | 子智能体独立上下文窗口 | 3d |
| Skills系统 | opencode/Claude Code | 可复用SKILL.md | 2d |

### P3 蓝海创新(竞品没有,我们独有)

| 功能 | 说明 | 状态 |
|---|---|---|
| 三视图同源分离 | 一次推理三视角输出 | 已有 |
| AI评估+对话一体化 | 评估数据可直接在对话中查询 | 已有 |
| 对话式人才洞察 | "这个季度谁进步最大?"对话查询 | 待开发 |
| AI评估偏差检测 | 结合HR数据检测评估偏差 | 待开发 |
| 员工成长路径AI推荐 | 基于评估数据推荐发展路径 | 待开发 |

---

## 四、补全优先级排序

### 第一批(1-2周):补齐Agent工具层基础能力

1. **grep/glob工具** — 代码搜索(1d)
2. **patch工具** — 精确文件编辑(2d)
3. **web_search工具** — 联网搜索(1d)
4. **图片理解(Vision)** — 上传图片对话(2d)
5. **分享对话链接** — 社交传播(2d)
6. **对话分支(Fork)** — 多路径探索(2d)
7. **暗色/亮色主题** — 视觉偏好(1d)
8. **快捷键** — 效率提升(1d)

### 第二批(3-4周):补齐多模态与个性化

1. **语音输入STT** — 麦克风录音(2d)
2. **语音输出TTS** — 回复朗读(2d)
3. **提示词模板库** — 内置常用Prompt(1d)
4. **Agent预设** — 预配置角色(2d)
5. **MCP协议支持** — 接入400+工具生态(3d)
6. **Code Interpreter** — Python沙箱(3d)
7. **对话中断恢复** — 流式恢复(1d)

### 第三批(5-8周):补齐HR评估深度

1. **360°环评** — 多评估人+校准(5d)
2. **人才九宫格** — 绩效×潜力矩阵(2d)
3. **校准会** — 评估校准流程(3d)
4. **AI偏差检测** — 评估偏差检测(3d)
5. **对话式人才洞察** — 对话查询人才数据(3d)

### 第四批(9-12周):蓝海创新

1. **Artifacts可视化产物** — 代码预览(3d)
2. **Sub Agents** — 子智能体(3d)
3. **Skills系统** — 可复用技能(2d)
4. **多渠道发布** — IM/Web分发(5d)

---

## 五、竞品定位图

```
                    功能深度
                      ↑
                      │
    Workday ●        │        ● Claude Code
    Culture Amp ●    │        ● ChatGPT
    Lattice ●        │
                      │  ● Dify
    15Five ●         │  ● Coze
                      │
    钉钉 ●            │  ● LobeChat
    BambooHR ●       │  ● Open WebUI
                      │
    ─────────────────┼──────────────────→ 易用性
                      │
                      │    ● AgentValue-AI (当前位置)
                      │      目标:向右上移动
```

**我们的差异化定位**:不是纯对话产品(对标ChatGPT),也不是纯HR工具(对标Lattice),
而是"对话+工具调用+员工评估"三位一体的智能体平台——这是目前市场上的空白。

---

## 六、Sources

### AI 对话产品
1. OpenAI Help Center - Model Release Notes
2. Anthropic - Claude 4 Release / Computer Use / Artifacts
3. Dify Blog - Agent Node / Workflows / v1.1.0 Metadata
4. Coze Docs - Natural Language Build / Multi-channel
5. LobeHub Docs - Agent Marketplace / TTS-STT
6. Open WebUI Docs - Pipelines / RAG System

### 代码智能体
7. opencode 官方文档 - Tools / Permissions
8. Cursor 官方文档 - Agent Overview / Composer
9. Cline 官方文档 - Plan & Act / Tools Reference
10. Aider 官方文档 - Repo Map / Commands
11. Claude Code 官方文档 - Tools / Sub Agents
12. VS Code - GitHub Copilot Features

### HR 评估产品
13. Lattice - Fall/Winter 2025 Release (AI Meeting Agent, Succession Planning)
14. 15Five - AI Assisted Reviews / Manager Copilot
15. Culture Amp - AI Coach Expansion / ISO42001
16. Leapsome - Product Updates Oct 2025
17. 飞书绩效官网 - 360°环评 / 校准
18. 钉钉智能绩效 - AI评分 / 量化产出
19. BambooHR - Performance Summary with AI
20. Workday - AI Agents / Illuminate / Skills Cloud

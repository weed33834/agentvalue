# AgentValue-AI Prompt 工程规范 v1.0

> **版本：** v1.0  
> **编写日期：** 2026-06-20  
> **适用范围：** 所有产生员工评估输出的 LLM Prompt

---

## 一、目标

这份规范管的是 Prompt 怎么设计、怎么改、怎么上线。四条硬要求：

1. LLM 输出必须能过 [Pydantic Schema](../backend/schemas/evaluation.py) 校验；
2. 员工视图和管理视图的语气严格分离，不能串；
3. 每条结论都要带原始证据，能追溯；
4. Prompt 改动必须可回归、可审计，禁止偷偷改完直接上。

---

## 二、Prompt 文件组织

```
backend/prompts/
├── daily_evaluation.md      # 员工日报周期评估主 Prompt
├── <feature>_prompt.md      # 后续扩展的 Prompt
└── versions/                # 历史版本归档
    ├── daily_evaluation_v0.1.md
    ├── daily_evaluation_v0.2.md
    ├── daily_evaluation_v1.0.md
    └── daily_evaluation_v1.1.md
```

**命名规则：**
- 文件名使用小写 + 下划线；
- 版本号格式 `v{major}.{minor}`，重大语气/结构变更升 major；
- Prompt 文件必须包含：版本、适用模型档位、输出格式、核心原则、Schema、示例。

---

## 三、Prompt 设计原则

### 3.1 Schema 优先

- Prompt 中必须显式给出输出 JSON Schema；
- Schema 字段的描述必须与 [evaluation.py](../backend/schemas/evaluation.py) 完全一致；
- 必须强调“只输出 JSON，不要 Markdown 代码块，不要解释”。

### 3.2 双视角分离

| 维度 | 员工视图（employee_view） | 管理视图（manager_view） |
|---|---|---|
| 目标读者 | 员工本人 | 主管、HR、高管 |
| 语气 | 建设性、发展性、尊重 | 直接、尖锐、基于商业判断 |
| 禁用词 | 差、懒、慢、拖沓、消极、不合格、无能、没用、糟糕、失败 | 无（但不可人身攻击） |
| 问题表述 | “成长空间”“可优化方向” | “风险”“ROI 下降”“成长斜率放缓” |
| 决策建议 | 仅限个人下一步行动 | 可建议任务/项目/团队调配 |

### 3.3 证据优先

- 每个 `DimensionScore.evidence` 必须引用原始输入中的具体片段；
- `ManagerView.hidden_issues` 必须有证据，禁止纯主观臆测；
- 证据不足时，必须降低 `confidence_score` 或明确说明“信息不足”。

### 3.4 安全护栏

- 禁止输出“建议辞退”“建议降薪”等直接人事处置结论；
- 禁止在 `employee_view` 中泄露 `manager_view` 内容；
- 禁止对员工进行人格评价，只能评价行为和产出。

---

## 四、变量替换规范

Prompt 中使用以下占位符，由调用方在运行时替换：

| 占位符 | 含义 | 是否必填 |
|---|---|---|
| `{raw_inputs}` | 员工本周原始输入（日报、任务、代码等）JSON | 是 |
| `{employee_history}` | 员工历史记忆摘要 JSON | 否 |
| `{company_kb}` | 公司评分标准、价值观等 JSON | 否 |
| `{employee_id}` | 员工 ID | 是 |
| `{period}` | 评估周期 | 是 |

**替换要求：**
- 使用 JSON 序列化后的字符串替换；
- 对特殊字符做转义，避免破坏 Prompt 结构；
- 空值使用 `[]` 或 `{}`，不要删除占位符。

---

## 五、版本管理

1. Prompt 版本号与代码版本解耦，独立管理；
2. 每次变更需复制到 `versions/` 目录并标注变更原因；
3. 变更后必须运行回归测试集，通过率 ≥ 90% 方可合并；
4. `AuditInfo.prompt_version` 必须记录实际使用的 Prompt 版本。

### 版本变更记录格式

```markdown
## daily_evaluation_v0.1 → v0.2
- 变更人：@prompt-engineer
- 变更日期：2026-06-27
- 变更原因：提升 evidence 引用准确率
- 主要修改：
  - 在输出质量控制规则中增加 evidence 长度校验；
  - 新增“禁止推断情绪”规则。
- 回归测试结果：3/3 通过
```

---

## 六、回归测试流程

### 6.1 测试集

- 存放位置：[backend/eval/dataset.json](../backend/eval/dataset.json)；
- 每个用例包含：case_id、employee_id、raw_inputs、expected_checks；
- 目标规模：Phase 1 完成 50 条，覆盖劳模、摸鱼、明星、新人、瓶颈期 5 类画像。

### 6.2 检查项

1. **Schema 合规：** 输出能否通过 `EmployeeEvaluation.model_validate`；
2. **无负面词：** 员工视图是否包含禁用负面词；
3. **证据引用：** growth_areas 中每个维度是否有具体证据；
4. **风险标记匹配：** 高风险输入是否被识别，低风险输入是否不误报；
5. **分数区间：** overall_score 是否在期望范围内；
6. **双视角隔离：** employee_view 中是否未泄露 manager_view 内容。

### 6.3 运行方式

```bash
cd backend
python -m pytest tests/test_schema.py -v
python -m eval.evaluate
```

---

## 七、模型档位适配

| 档位 | Prompt 策略 |
|---|---|
| L0-云端 | 使用完整 Prompt，启用全模态与深度推理 |
| L1-边缘 | 仅输出 employee_view 摘要版，减少字段与维度 |
| L2-标准 | 使用完整 Prompt，但可限制维度数量至 3-4 个 |
| L3-本地旗舰 | 使用完整 Prompt，启用全模态 |

---

## 八、常见问题与调试

### 8.1 LLM 不输出合法 JSON

- 检查 Prompt 中是否明确“只输出 JSON”；
- 使用 Pydantic `model_validate` 捕获错误并反馈给模型重试；
- 考虑降低模型温度（temperature=0）并设置 top_p=1。

### 8.2 员工视图出现负面词

- 在 Prompt 中增加反面示例；
- 在回归测试集中增加负面词检查用例；
- 对频繁违规的模型降低档位或增加输出后处理。

### 8.3 evidence 引用 hallucination

- 强制要求 evidence 必须来自原始输入；
- 在回归测试集中加入“无证据不得分”检查；
- 使用 LLM-as-a-Judge 验证 evidence 与原文的相似度。

---

## 九、相关文档

- [架构实现说明](architecture-notes.md)
- [Schema 定义](../backend/schemas/evaluation.py)
- [当前 Prompt](../backend/prompts/daily_evaluation.md)
- [回归测试数据集](../backend/eval/dataset.json)


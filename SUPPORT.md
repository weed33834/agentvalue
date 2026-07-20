# 获取帮助

遇到问题先按下面的渠道对号入座,能更快拿到回应。

## 渠道一览

| 场景 | 去哪里 | 说明 |
|---|---|---|
| Bug / 异常行为 | [GitHub Issue](https://gitcode.com/badhope/agentvalue/issues/new?template=bug_report.md) | 附复现步骤、commit SHA / 版本、模型档位 |
| 功能建议 | [GitHub Issue](https://gitcode.com/badhope/agentvalue/issues/new?template=feature_request.md) | 描述场景与预期收益 |
| 部署 / 配置 / 使用疑问 | [GitHub Discussions](https://gitcode.com/badhope/agentvalue/discussions) | 不确定是不是 Bug 的问题先来这里讨论 |
| 安全漏洞 | [SECURITY.md](SECURITY.md) | **私密报告,不要开公开 Issue** |

国内访问 GitHub 慢的,可以先用 [GitCode 镜像](https://gitcode.com/badhope/AgentValue-AI) 浏览代码;
但 Issue / Discussions / 安全报告统一走 GitHub 主仓库,镜像只做代码同步,不接受单独 issue。

## 提问前先自查

1. 翻一遍 [README 的 FAQ](README.md#faq),常见的不配 Key 能不能跑、演示模式能不能进生产、
   多租户隔离怎么做都有答案。
2. 跑一遍 `cd backend && python scripts/check_prod_readiness.py`,生产就绪检查会直接指出
   缺失的配置项。
3. 评估结果异常(误判 / 偏见 / 幻觉)开 Issue 时,请附:case 输入、模型档位、Prompt 版本
   (见 `backend/prompts/versions/`),方便定位是 Prompt 还是模型问题。

## 企业落地支持

AgentValue-AI 是开源公益项目,不提供商业 SLA。企业落地(私有化部署、定制 Prompt、合规审计
对接)的深度支持,请在 Discussions 开帖说明背景,维护者会评估能否在社区范围内给到指引。

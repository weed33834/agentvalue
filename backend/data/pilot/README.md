# 试点数据集

本目录下的所有 JSON 文件均为**合成测试数据**,由 `backend/scripts/pilot_data_generator.py` 程序化生成,
**不包含任何真实员工信息**。文件中出现的姓名(如"李强"、"王薇")、工号、邮箱、部门、地区、Jira 编号等
全部为虚构样本,用于:

- 试点巡检与回归演示
- 公平性月报、SLA 监控、规模化部署 Runbook 的样例输出
- E2E 测试 fixture

## 生成方式

```bash
cd backend
python scripts/pilot_data_generator.py
```

可调整脚本参数控制规模(startup / growth / medium / large / huge 五档)与种子。

## 子目录

| 目录 | 规模 | 员工数 | 说明 |
|---|---|---|---|
| `startup/` | 初创团队 | 15 | 全员生成 |
| `growth/` | 成长期 | 80 | 全员生成 |
| `medium/` | 中型企业 | 100 | 从 500 人中抽样 |
| `large/` | 大型企业 | 150 | 从 5000 人中抽样 |
| `huge/` | 超大型 | 200 | 从 50000 人中抽样 |

> 员工数为实际生成的 `employees.json` 记录数。medium/large/huge 三档因全员数量过大
> (500/5000/50000),按 `pilot_data_generator.py` 的 `sampled_employees` 配置抽样生成。

每档目录下包含 `employees.json` 与 `weekly_reports_week1~4.json`(4 周周报)。

## 根目录文件

- `_summary.json` — 试点汇总
- `fairness-monthly-report.json` — 公平性月报样例输出
- `sla-report.json` — 申诉处理 SLA 监控样例输出

## 重新生成

如需重置数据:

```bash
cd backend
rm -rf data/pilot/*.json data/pilot/*/*.json
python scripts/pilot_data_generator.py
```

注意:重新生成会改变员工姓名/工号等占位内容,相关测试 fixture 也会随之变化。
